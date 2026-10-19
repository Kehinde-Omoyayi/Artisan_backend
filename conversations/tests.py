from datetime import date, timedelta
from decimal import Decimal
from time import perf_counter
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from booking.models import Booking
from home.models import ArtisanProfile, ArtisanService, CustomUser, CustomerProfile, Locations, Service, ServiceCategory
from payments.models import EscrowPayment
from services.models import ArtisanVerification, ServiceRequest
from support.models import SupportCase

from .automation import _location_is_negated, _parse_preferred_date
from .models import Conversation, ConversationAttachment, Message, Offer
from .services import apply_request_patch, create_offer, open_conversation_dispute, send_message


class ConversationTestMixin:
    def make_user(self, email, *, active=True):
        user = CustomUser.objects.create_user(email=email, first_name="Test", last_name="User", password="Passw0rd!", is_active=active)
        user.phone_number = f"+234803{user.pk:07d}"
        user.save(update_fields=["phone_number"])
        return user

    def make_artisan(self, email="artisan@example.com"):
        user = self.make_user(email)
        artisan = ArtisanProfile.objects.create(user=user, business_name="Test Artisan", profile_status="ACTIVE", country="Nigeria", state="Lagos", area="Ikeja")
        ArtisanVerification.objects.create(artisan=artisan, verification_status="VERIFIED", verification_method="NIN")
        return user, artisan

    def make_conversation(self):
        return Conversation.objects.create(customer=self.customer, artisan=self.artisan)

    def complete_request(self, conversation=None, *, budget=None):
        conversation = conversation or self.conversation
        payload = {"description": "Fix the wiring in my apartment.", "service_id": self.service.id, "location_id": str(self.location.id), "preferred_date": (timezone.localdate() + timedelta(days=2)).isoformat()}
        if budget is not None:
            payload["budget"] = budget
        apply_request_patch(conversation=conversation, user=self.customer, payload=payload)
        conversation.refresh_from_db()
        return ServiceRequest.objects.get(pk=conversation.service_request_id)

    def offer_payload(self, total=Decimal("25000.00")):
        start = timezone.now() + timedelta(days=1)
        end = start + timedelta(hours=2)
        expiry = start - timedelta(hours=1)
        labour = total - Decimal("5000.00")
        return {"labour_amount": labour, "materials_amount": Decimal("5000.00"), "total_amount": total, "materials_included": True, "scope": "Repair and test affected sockets.", "notes": "Bring replacement parts.", "scheduled_start": start, "scheduled_end": end, "expires_at": expiry}


class PreferredDateParserTests(SimpleTestCase):
    def test_location_negation_is_recognized(self):
        self.assertTrue(_location_is_negated("not Ikeja", "Ikeja"))
        self.assertTrue(_location_is_negated("not in Ikeja", "Ikeja"))
        self.assertTrue(_location_is_negated("instead of Ikeja", "Ikeja"))
        self.assertFalse(_location_is_negated("the job is in Ikeja", "Ikeja"))

    def test_negated_date_is_ignored_and_explicit_correction_is_used(self):
        today = date(2026, 10, 14)
        self.assertEqual(
            _parse_preferred_date("not 2026-10-15, use 16/10/2026", today),
            date(2026, 10, 16),
        )
        self.assertEqual(
            _parse_preferred_date("not October 15, use October 16", today),
            date(2026, 10, 16),
        )
        self.assertEqual(
            _parse_preferred_date("next Friday instead of next Thursday", today),
            date(2026, 10, 16),
        )
        self.assertIsNone(_parse_preferred_date("not 15/10/2026", today))
        self.assertIsNone(_parse_preferred_date("not next Friday", today))

    def test_conflicting_positive_date_mentions_are_left_for_clarification(self):
        today = date(2026, 10, 14)
        self.assertIsNone(_parse_preferred_date("Friday or Monday", today))
        self.assertIsNone(_parse_preferred_date("tomorrow, 20/10/2026", today))
        self.assertEqual(_parse_preferred_date("not tomorrow; today works", today), today)

    def test_measurements_and_clock_ranges_are_not_mistaken_for_dates(self):
        today = date(2026, 10, 14)
        self.assertEqual(
            _parse_preferred_date("Replace the 3/4 inch pipe tomorrow at 9-11 am", today),
            date(2026, 10, 15),
        )
        self.assertEqual(
            _parse_preferred_date("Schedule between 9-11 in the morning tomorrow", today),
            date(2026, 10, 15),
        )


class ConversationWorkflowTests(TestCase, ConversationTestMixin):
    def setUp(self):
        self.client = APIClient()
        self.customer = self.make_user("customer@example.com")
        CustomerProfile.objects.create(user=self.customer, country="Nigeria", state="Lagos", area="Ikeja")
        self.other_customer = self.make_user("other@example.com")
        CustomerProfile.objects.create(user=self.other_customer, country="Nigeria", state="Lagos", area="Lekki")
        self.artisan_user, self.artisan = self.make_artisan()
        self.other_artisan_user, self.other_artisan = self.make_artisan("other-artisan@example.com")
        self.location = Locations.objects.create(country="Nigeria", state="Lagos", area="Ikeja")
        self.category = ServiceCategory.objects.create(name="Electrical")
        self.service = Service.objects.create(category=self.category, name="Electrical Repairs", description="Residential electrical repairs")
        ArtisanService.objects.create(artisan=self.artisan, service=self.service, location=self.location, starting_price=Decimal("10000.00"))
        self.other_location = Locations.objects.create(country="Nigeria", state="Lagos", area="Lekki")
        self.other_service = Service.objects.create(category=self.category, name="Plumbing Repairs", description="Residential plumbing repairs")
        ArtisanService.objects.create(artisan=self.artisan, service=self.other_service, location=self.location, starting_price=Decimal("12000.00"))
        self.conversation = self.make_conversation()

    def auth(self, user):
        self.client.force_authenticate(user=user)

    def test_all_conversation_routes_are_wired(self):
        from django.urls import reverse
        self.assertTrue(reverse("conversation-start"))
        cid = self.conversation.id
        oid = "00000000-0000-0000-0000-000000000001"
        routes = [
            reverse("conversation-list"), reverse("conversation-detail", args=[cid]), reverse("conversation-messages", args=[cid]),
            reverse("conversation-request", args=[cid]), reverse("conversation-offers", args=[cid]), reverse("conversation-offer-counter", args=[cid]),
            reverse("conversation-offer-accept", args=[cid, oid]), reverse("conversation-offer-decline", args=[cid, oid]), reverse("conversation-read", args=[cid]),
            reverse("conversation-payment-initialize", args=[cid]), reverse("conversation-dispute", args=[cid]),
            reverse("conversation-direct-artisan", args=[self.other_artisan.id]), reverse("conversation-inbox"),
        ]
        self.assertEqual(len(routes), 13)

    def test_conversation_root_get_returns_inbox_payload(self):
        self.auth(self.customer)
        response = self.client.get("/api/v1/conversations/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("results", response.data)

    def test_inbox_preview_is_personalized_for_customer_and_artisan(self):
        send_message(
            conversation=self.conversation,
            user=self.customer,
            body="The bedroom socket is damaged and needs a repair.",
            idempotency_key="inbox-preview-1",
        )

        self.auth(self.customer)
        customer_response = self.client.get("/api/v1/conversations/")
        customer_item = next(item for item in customer_response.data["results"] if item["id"] == str(self.conversation.id))
        self.assertEqual(customer_item["last_message_preview"], "You: The bedroom socket is damaged and needs a repair.")

        self.auth(self.artisan_user)
        artisan_response = self.client.get("/api/v1/conversations/")
        artisan_item = next(item for item in artisan_response.data["results"] if item["id"] == str(self.conversation.id))
        self.assertEqual(artisan_item["last_message_preview"], "Test User: The bedroom socket is damaged and needs a repair.")

    def test_inbox_unread_count_uses_read_watermark_without_changing_legacy_boolean(self):
        for index in range(2):
            send_message(
                conversation=self.conversation,
                user=self.artisan_user,
                body=f"Incoming update {index + 1}.",
                idempotency_key=f"inbox-unread-initial-{index}",
            )

        self.auth(self.customer)
        first = self.client.get("/api/v1/conversations/")
        first_item = next(item for item in first.data["results"] if item["id"] == str(self.conversation.id))
        self.assertTrue(first_item["unread"])
        self.assertEqual(first_item["unread_count"], 2)

        marked_read = self.client.post(f"/api/v1/conversations/{self.conversation.id}/read/", {}, format="json")
        self.assertEqual(marked_read.status_code, status.HTTP_200_OK)

        send_message(
            conversation=self.conversation,
            user=self.artisan_user,
            body="One new update after the read marker.",
            idempotency_key="inbox-unread-after-read",
        )
        send_message(
            conversation=self.conversation,
            user=self.customer,
            body="My reply does not erase the incoming unread count.",
            idempotency_key="inbox-own-reply-after-read",
        )

        final = self.client.get("/api/v1/conversations/")
        final_item = next(item for item in final.data["results"] if item["id"] == str(self.conversation.id))
        self.assertFalse(final_item["unread"])
        self.assertEqual(final_item["unread_count"], 1)
        self.assertEqual(Message.objects.filter(conversation=self.conversation, sender=self.artisan_user).count(), 3)

    def test_offer_decline_requires_and_respects_idempotency(self):
        conversation = self.conversation
        self.complete_request(conversation, budget=Decimal("30000.00"))
        self.auth(self.artisan_user)
        offer = create_offer(conversation=conversation, user=self.artisan_user, payload=self.offer_payload(), idempotency_key="offer-1")
        self.auth(self.customer)
        first = self.client.post(
            f"/api/v1/conversations/{conversation.id}/offers/{offer.id}/decline/",
            {}, format="json", HTTP_IDEMPOTENCY_KEY="decline-1"
        )
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        second = self.client.post(
            f"/api/v1/conversations/{conversation.id}/offers/{offer.id}/decline/",
            {}, format="json", HTTP_IDEMPOTENCY_KEY="decline-1"
        )
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data["id"], second.data["id"])

    def test_artisan_to_artisan_direct_chat_is_supported(self):
        self.auth(self.artisan_user)
        response = self.client.post(
            "/api/v1/conversations/",
            {"participants": [self.other_artisan_user.id], "initial_message": "Let’s collaborate on a job."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="direct-create-1",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        conversation_id = response.data["id"]
        conversation = Conversation.objects.get(pk=conversation_id)
        self.assertEqual(conversation.conversation_type, Conversation.ConversationType.DIRECT)
        self.assertIsNone(conversation.customer_id)
        self.assertIsNone(conversation.artisan_id)
        self.assertEqual(set(conversation.participants.values_list("id", flat=True)), {self.artisan_user.id, self.other_artisan_user.id})
        self.assertEqual(Message.objects.filter(conversation=conversation, sender=self.artisan_user).count(), 1)

        self.auth(self.other_artisan_user)
        reply = self.client.post(
            f"/api/v1/conversations/{conversation_id}/messages/",
            {"body": "Absolutely."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="direct-msg-1",
        )
        self.assertEqual(reply.status_code, status.HTTP_201_CREATED)
        self.assertEqual(reply.data["sender_id"], self.other_artisan_user.id)

    def test_direct_conversation_cannot_be_created_with_self_or_idor_viewed(self):
        self.auth(self.artisan_user)
        self_response = self.client.post(
            "/api/v1/conversations/",
            {"participants": [self.artisan_user.id]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="direct-self-1",
        )
        self.assertEqual(self_response.status_code, status.HTTP_400_BAD_REQUEST)
        create = self.client.post(
            "/api/v1/conversations/",
            {"participants": [self.other_artisan_user.id]},
            format="json",
            HTTP_IDEMPOTENCY_KEY="direct-idor-1",
        )
        self.assertEqual(create.status_code, status.HTTP_201_CREATED)
        conversation_id = create.data["id"]
        self.auth(self.other_customer)
        self.assertEqual(self.client.get(f"/api/v1/conversations/{conversation_id}/").status_code, status.HTTP_404_NOT_FOUND)

    def test_start_and_resume_conversation(self):
        self.auth(self.other_customer)
        response = self.client.post("/api/v1/conversations/start/", {"artisan_id": str(self.artisan.id), "service_id": self.service.id}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        conversation_id = response.data["id"]
        resume = self.client.post("/api/v1/conversations/start/", {"artisan_id": str(self.artisan.id), "service_id": self.service.id}, format="json")
        self.assertEqual(resume.status_code, status.HTTP_200_OK)
        self.assertEqual(resume.data["id"], conversation_id)
        self.assertEqual(Conversation.objects.filter(customer=self.other_customer, artisan=self.artisan).count(), 1)
        # The fixture customer's existing draft conversation is resumed, never duplicated.
        self.auth(self.customer)
        again = self.client.post("/api/v1/conversations/start/", {"artisan_id": str(self.artisan.id), "service_id": self.service.id}, format="json")
        self.assertEqual(again.status_code, status.HTTP_200_OK)
        self.assertEqual(again.data["id"], str(self.conversation.id))
        self.assertEqual(Conversation.objects.filter(customer=self.customer, artisan=self.artisan).count(), 1)

    def test_dual_role_artisan_uses_customer_side_service_context_without_losing_artisan_access(self):
        CustomerProfile.objects.create(
            user=self.artisan_user,
            country="Nigeria",
            state="Lagos",
            area="Ikeja",
        )
        ArtisanService.objects.create(
            artisan=self.other_artisan,
            service=self.other_service,
            location=self.location,
            starting_price=Decimal("12000.00"),
        )
        self.auth(self.artisan_user)

        started = self.client.post(
            "/api/v1/conversations/start/",
            {"artisan_id": str(self.other_artisan.id), "service_id": self.other_service.id},
            format="json",
        )

        self.assertEqual(started.status_code, status.HTTP_201_CREATED, started.data)
        customer_side = Conversation.objects.get(pk=started.data["id"])
        self.assertEqual(customer_side.customer_id, self.artisan_user.id)
        self.assertEqual(customer_side.artisan_id, self.other_artisan.id)
        self.assertEqual(started.data["viewer_role"], "customer")

        list_response = self.client.get("/api/v1/conversations/")
        customer_item = next(item for item in list_response.data["results"] if item["id"] == str(customer_side.id))
        artisan_item = next(item for item in list_response.data["results"] if item["id"] == str(self.conversation.id))
        self.assertEqual(customer_item["viewer_role"], "customer")
        self.assertEqual(customer_item["counterparty"]["role"], "artisan")
        self.assertEqual(artisan_item["viewer_role"], "artisan")
        self.assertEqual(artisan_item["counterparty"]["role"], "customer")

        message = self.client.post(
            f"/api/v1/conversations/{customer_side.id}/messages/",
            {"body": "I am asking about a plumbing repair."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="dual-role-customer-message",
        )
        self.assertEqual(message.status_code, status.HTTP_201_CREATED, message.data)
        self.assertEqual(message.data["sender_role"], "customer")

        existing_artisan_detail = self.client.get(f"/api/v1/conversations/{self.conversation.id}/")
        self.assertEqual(existing_artisan_detail.status_code, status.HTTP_200_OK)
        self.assertEqual(existing_artisan_detail.data["viewer_role"], "artisan")

    def test_idor_is_blocked_for_detail_messages_offers_and_dispute(self):
        victim = self.conversation
        self.auth(self.other_customer)
        self.assertEqual(self.client.get(f"/api/v1/conversations/{victim.id}/").status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(f"/api/v1/conversations/{victim.id}/messages/").status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(f"/api/v1/conversations/{victim.id}/offers/").status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.post(f"/api/v1/conversations/{victim.id}/dispute/", {"reason": "attack"}, format="json").status_code, status.HTTP_404_NOT_FOUND)

    def test_progressive_request_auto_selects_sole_service_and_allows_unknown_budget(self):
        self.artisan.artisan_services.filter(service=self.other_service).delete()
        self.auth(self.customer)
        response = self.client.patch(f"/api/v1/conversations/{self.conversation.id}/request/", {"description": "Fix my sockets.", "location_id": str(self.location.id), "preferred_date": (timezone.localdate() + timedelta(days=2)).isoformat(), "budget_unknown": True}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.conversation.refresh_from_db()
        request = ServiceRequest.objects.get(pk=self.conversation.service_request_id)
        self.assertEqual(request.budget, None)
        self.assertEqual(request.category_id, self.category.pk)
        self.assertEqual(response.data["service_request"]["service"], self.service.name)


    def test_explicit_draft_review_and_submit_creates_one_real_request(self):
        self.auth(self.customer)
        payload = {
            "service_id": self.service.id,
            "category_id": self.category.pk,
            "description": "Replace the damaged bedroom socket and test the circuit.",
            "location_id": str(self.location.id),
            "country": self.location.country,
            "state": self.location.state,
            "area": self.location.area,
            "preferred_date": (timezone.localdate() + timedelta(days=3)).isoformat(),
            "preferred_time_start": "09:30",
            "preferred_time_end": "11:00",
            "preferred_time_note": "Before work",
            "additional_details": "The building access code is shared on arrival.",
            "budget_unknown": True,
        }
        draft_response = self.client.patch(
            f"/api/v1/conversations/{self.conversation.id}/request/",
            {**payload, "submit_request": False},
            format="json",
        )
        self.assertEqual(draft_response.status_code, status.HTTP_200_OK, draft_response.data)
        self.conversation.refresh_from_db()
        self.assertIsNone(self.conversation.service_request_id)
        self.assertEqual(ServiceRequest.objects.count(), 0)
        self.assertEqual(self.conversation.draft["preferred_time_start"], "09:30")
        self.assertEqual(draft_response.data["assistant"]["mode"], "ready_for_review")

        submitted = self.client.patch(
            f"/api/v1/conversations/{self.conversation.id}/request/",
            {**payload, "submit_request": True},
            format="json",
        )
        self.assertEqual(submitted.status_code, status.HTTP_200_OK, submitted.data)
        self.conversation.refresh_from_db()
        request_id = self.conversation.service_request_id
        self.assertIsNotNone(request_id)
        request = ServiceRequest.objects.get(pk=request_id)
        self.assertEqual(request.preferred_time_start.strftime("%H:%M"), "09:30")
        self.assertEqual(request.preferred_time_end.strftime("%H:%M"), "11:00")
        self.assertEqual(request.preferred_time_note, "Before work")
        self.assertEqual(request.additional_details, "The building access code is shared on arrival.")

        retry = self.client.patch(
            f"/api/v1/conversations/{self.conversation.id}/request/",
            {**payload, "submit_request": True},
            format="json",
        )
        self.assertEqual(retry.status_code, status.HTTP_200_OK, retry.data)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.service_request_id, request_id)
        self.assertEqual(ServiceRequest.objects.count(), 1)

    def test_explicit_submit_validates_all_required_fields_without_creating_a_request(self):
        self.auth(self.customer)
        response = self.client.patch(
            f"/api/v1/conversations/{self.conversation.id}/request/",
            {
                "submit_request": True,
                "service_id": self.service.id,
                "description": "Repair the damaged socket safely.",
                "location_id": str(self.location.id),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("preferred_date", response.data["details"])
        self.assertEqual(ServiceRequest.objects.count(), 0)
        self.conversation.refresh_from_db()
        self.assertIsNone(self.conversation.service_request_id)

    def test_message_parsing_saves_catalogue_backed_draft_without_submitting(self):
        plumbing = ServiceCategory.objects.create(name="Plumbing")
        plumbing_service = Service.objects.create(category=plumbing, name="Household Pipe Repairs", description="Repair taps and pipes")
        ArtisanService.objects.create(artisan=self.artisan, service=plumbing_service, location=self.location, starting_price=Decimal("12000.00"))
        self.auth(self.customer)
        response = self.client.post(
            f"/api/v1/conversations/{self.conversation.id}/messages/",
            {"body": "The kitchen tap is leaking in Ikeja tomorrow at 2 pm."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="draft-natural-language-1",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.conversation.refresh_from_db()
        self.assertIsNone(self.conversation.service_request_id)
        self.assertEqual(ServiceRequest.objects.count(), 0)
        self.assertEqual(self.conversation.draft["service_id"], str(plumbing_service.pk))
        self.assertEqual(self.conversation.draft["location_id"], str(self.location.pk))
        self.assertEqual(self.conversation.draft["preferred_date"], (timezone.localdate() + timedelta(days=1)).isoformat())
        self.assertEqual(self.conversation.draft["preferred_time_start"], "14:00")
        self.assertIn("tap", self.conversation.draft["description"].lower())
        self.assertEqual(response.data["assistant"]["mode"], "collecting_optional_details")
        self.assertEqual(response.data["assistant"]["prompt"]["field"], "budget")
        self.assertTrue(response.data["assistant"]["prompt"]["optional"])

    def test_natural_chat_extracts_service_location_date_time_and_budget_without_submitting(self):
        self.auth(self.customer)
        preferred_date = timezone.localdate() + timedelta(days=5)
        message = (
            "I need an electrician to fix a damaged socket in Ikeja, Lagos "
            f"on {preferred_date.strftime('%d-%m-%Y')} at 2:37 pm. My budget is ₦10,000."
        )

        response = self.client.post(
            f"/api/v1/conversations/{self.conversation.id}/messages/",
            {"body": message},
            format="json",
            HTTP_IDEMPOTENCY_KEY="draft-natural-multi-detail-1",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.draft["service_id"], str(self.service.pk))
        self.assertEqual(self.conversation.draft["location_id"], str(self.location.pk))
        self.assertEqual(self.conversation.draft["preferred_date"], preferred_date.isoformat())
        self.assertEqual(self.conversation.draft["preferred_time_start"], "14:37")
        self.assertEqual(Decimal(str(self.conversation.draft["budget"])), Decimal("10000.00"))
        self.assertFalse(self.conversation.draft["budget_unknown"])
        self.assertEqual(response.data["assistant"]["mode"], "ready_for_review")
        self.assertEqual(response.data["assistant"]["request_state"]["values"]["preferred_time_start"], "14:37")
        self.assertIsNone(self.conversation.service_request_id)
        self.assertEqual(ServiceRequest.objects.count(), 0)

    def test_optional_budget_prompt_accepts_bare_amount_and_open_quote_reply(self):
        self.auth(self.customer)
        saved = self.client.patch(
            f"/api/v1/conversations/{self.conversation.id}/request/",
            {
                "submit_request": False,
                "service_id": self.service.pk,
                "description": "Replace a cracked bedroom socket safely.",
                "location_id": str(self.location.pk),
                "preferred_date": (timezone.localdate() + timedelta(days=4)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(saved.status_code, status.HTTP_200_OK, saved.data)
        self.assertEqual(saved.data["assistant"]["prompt"]["field"], "budget")
        self.assertTrue(saved.data["assistant"]["prompt"]["optional"])

        amount = self.client.post(
            f"/api/v1/conversations/{self.conversation.id}/messages/",
            {"body": "10,000"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="draft-budget-short-answer-1",
        )
        self.assertEqual(amount.status_code, status.HTTP_201_CREATED, amount.data)
        self.conversation.refresh_from_db()
        self.assertEqual(Decimal(str(self.conversation.draft["budget"])), Decimal("10000.00"))
        self.assertFalse(self.conversation.draft["budget_unknown"])
        self.assertEqual(amount.data["assistant"]["mode"], "ready_for_review")

        open_quote = self.client.post(
            f"/api/v1/conversations/{self.conversation.id}/messages/",
            {"body": "Open to a quote instead."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="draft-budget-open-quote-1",
        )
        self.assertEqual(open_quote.status_code, status.HTTP_201_CREATED, open_quote.data)
        self.conversation.refresh_from_db()
        self.assertTrue(self.conversation.draft["budget_unknown"])
        self.assertIsNone(self.conversation.draft["budget"])
        self.assertEqual(open_quote.data["assistant"]["mode"], "ready_for_review")
        self.assertIsNone(self.conversation.service_request_id)
        self.assertEqual(ServiceRequest.objects.count(), 0)

    def test_chat_corrections_update_date_time_and_budget_without_overwriting_description(self):
        self.auth(self.customer)
        original_description = "Repair the damaged bedroom socket and test the circuit."
        original_date = timezone.localdate() + timedelta(days=2)
        revised_date = timezone.localdate() + timedelta(days=7)
        saved = self.client.patch(
            f"/api/v1/conversations/{self.conversation.id}/request/",
            {
                "submit_request": False,
                "service_id": self.service.pk,
                "description": original_description,
                "location_id": str(self.location.pk),
                "preferred_date": original_date.isoformat(),
                "preferred_time_start": "09:00",
                "preferred_time_end": "11:00",
                "budget": "25000.00",
                "budget_unknown": False,
            },
            format="json",
        )
        self.assertEqual(saved.status_code, status.HTTP_200_OK, saved.data)

        response = self.client.post(
            f"/api/v1/conversations/{self.conversation.id}/messages/",
            {
                "body": (
                    f"Move the preferred visit to {revised_date.strftime('%d-%m-%Y')} at 3:15 pm "
                    "and change the budget to ₦12,500."
                )
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="draft-date-budget-correction-1",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.draft["preferred_date"], revised_date.isoformat())
        self.assertEqual(self.conversation.draft["preferred_time_start"], "15:15")
        self.assertIsNone(self.conversation.draft["preferred_time_end"])
        self.assertEqual(Decimal(str(self.conversation.draft["budget"])), Decimal("12500.00"))
        self.assertEqual(self.conversation.draft["description"], original_description)
        self.assertIsNone(self.conversation.service_request_id)
        self.assertEqual(ServiceRequest.objects.count(), 0)

    def test_ambiguous_service_message_retains_category_without_picking_a_service(self):
        plumbing = ServiceCategory.objects.create(name="Plumbing")
        repair = Service.objects.create(category=plumbing, name="Pipe Repairs", description="Repair household pipes")
        install = Service.objects.create(category=plumbing, name="Plumbing Installation", description="Install plumbing fixtures")
        ArtisanService.objects.create(artisan=self.artisan, service=repair, location=self.location, starting_price=Decimal("10000.00"))
        ArtisanService.objects.create(artisan=self.artisan, service=install, location=self.location, starting_price=Decimal("15000.00"))
        self.auth(self.customer)
        response = self.client.post(
            f"/api/v1/conversations/{self.conversation.id}/messages/",
            {"body": "I need plumbing."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="draft-ambiguous-service-1",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.draft.get("category_id"), plumbing.pk)
        self.assertNotIn("service_id", self.conversation.draft)
        self.assertIsNone(self.conversation.service_request_id)
        self.assertEqual(response.data["assistant"]["prompt"]["field"], "service")
        self.assertIn("Plumbing", response.data["assistant"]["prompt"]["text"])

    def test_ambiguous_date_correction_clears_only_the_preferred_date(self):
        self.auth(self.customer)
        saved = self.client.patch(
            f"/api/v1/conversations/{self.conversation.id}/request/",
            {
                "submit_request": False,
                "service_id": self.service.pk,
                "description": "Fix a kitchen socket that stopped working.",
                "location_id": str(self.location.pk),
                "preferred_date": (timezone.localdate() + timedelta(days=2)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(saved.status_code, status.HTTP_200_OK, saved.data)

        response = self.client.post(
            f"/api/v1/conversations/{self.conversation.id}/messages/",
            {"body": "Actually, Friday or Monday?"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="draft-ambiguous-date-correction-1",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.conversation.refresh_from_db()
        self.assertIsNone(self.conversation.draft["preferred_date"])
        self.assertEqual(self.conversation.draft["description"], "Fix a kitchen socket that stopped working.")
        self.assertEqual(self.conversation.draft["location_id"], str(self.location.pk))
        self.assertEqual(response.data["assistant"]["prompt"]["field"], "preferred_date")

    def test_message_corrections_replace_only_location_date_and_time(self):
        plumbing = ServiceCategory.objects.create(name="Plumbing")
        service = Service.objects.create(category=plumbing, name="Household Pipe Repairs", description="Repair taps and pipes")
        ArtisanService.objects.create(artisan=self.artisan, service=service, location=self.location, starting_price=Decimal("12000.00"))
        ArtisanService.objects.create(artisan=self.artisan, service=service, location=self.other_location, starting_price=Decimal("12000.00"))
        self.auth(self.customer)
        saved = self.client.patch(
            f"/api/v1/conversations/{self.conversation.id}/request/",
            {
                "submit_request": False,
                "service_id": service.pk,
                "description": "Fix a kitchen tap that is leaking.",
                "location_id": str(self.location.pk),
                "preferred_date": (timezone.localdate() + timedelta(days=1)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(saved.status_code, status.HTTP_200_OK, saved.data)
        corrected = self.client.post(
            f"/api/v1/conversations/{self.conversation.id}/messages/",
            {"body": "Actually, the job is in Lekki, not Ikeja, next Friday at 3 pm."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="draft-location-correction-1",
        )
        self.assertEqual(corrected.status_code, status.HTTP_201_CREATED, corrected.data)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.draft["description"], "Fix a kitchen tap that is leaking.")
        self.assertEqual(self.conversation.draft["location_id"], str(self.other_location.pk))
        self.assertEqual(self.conversation.draft["preferred_date"], (timezone.localdate() + timedelta(days=(4 - timezone.localdate().weekday()) % 7 or 7)).isoformat())
        self.assertEqual(self.conversation.draft["preferred_time_start"], "15:00")
        self.assertIsNone(self.conversation.service_request_id)

    def test_budget_can_be_supplied_after_budget_unknown(self):
        self.auth(self.customer)
        first = self.client.patch(f"/api/v1/conversations/{self.conversation.id}/request/", {"description": "Fix my sockets.", "service_id": self.service.id, "location_id": str(self.location.id), "preferred_date": (timezone.localdate() + timedelta(days=2)).isoformat(), "budget_unknown": True}, format="json")
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        second = self.client.patch(f"/api/v1/conversations/{self.conversation.id}/request/", {"budget": "25000.00", "budget_unknown": False}, format="json")
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.conversation.refresh_from_db()
        request = ServiceRequest.objects.get(pk=self.conversation.service_request_id) if self.conversation.service_request_id else None
        self.assertIsNotNone(request)
        self.assertEqual(request.budget, Decimal("25000.00"))

    def test_budget_unknown_choice_can_be_corrected_to_no_budget(self):
        self.auth(self.customer)
        first = self.client.patch(
            f"/api/v1/conversations/{self.conversation.id}/request/",
            {"submit_request": False, "budget_unknown": True},
            format="json",
        )
        self.assertEqual(first.status_code, status.HTTP_200_OK, first.data)
        corrected = self.client.patch(
            f"/api/v1/conversations/{self.conversation.id}/request/",
            {"submit_request": False, "budget_unknown": False, "budget": None},
            format="json",
        )
        self.assertEqual(corrected.status_code, status.HTTP_200_OK, corrected.data)
        self.conversation.refresh_from_db()
        self.assertFalse(self.conversation.draft["budget_unknown"])
        self.assertIsNone(self.conversation.draft["budget"])
        self.assertIsNone(self.conversation.service_request_id)

    def test_service_rejects_infinite_budget_and_offer_amounts(self):
        self.auth(self.customer)
        response = self.client.patch(f"/api/v1/conversations/{self.conversation.id}/request/", {"budget": "Infinity"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.complete_request()
        payload = self.offer_payload()
        payload["total_amount"] = Decimal("Infinity")
        payload["labour_amount"] = Decimal("Infinity")
        with self.assertRaises(Exception):
            create_offer(conversation=self.conversation, user=self.artisan_user, payload=payload)

    def test_offer_idempotency_key_cannot_be_reused_for_counter_operation(self):
        self.complete_request()
        offer = create_offer(conversation=self.conversation, user=self.artisan_user, payload=self.offer_payload(), idempotency_key="same-op")
        counter = self.offer_payload(Decimal("27000.00"))
        # Keys are scoped to the acting user: another user's identical key string is an independent operation
        # and never replays (or reveals) the artisan's offer.
        countered = create_offer(conversation=self.conversation, user=self.customer, payload=counter, parent_offer_id=offer.id, idempotency_key="same-op")
        self.assertNotEqual(countered.id, offer.id)
        self.assertEqual(countered.parent_offer_id, offer.id)
        # The same user replaying the key for the same operation is idempotent...
        replay = create_offer(conversation=self.conversation, user=self.customer, payload=counter, parent_offer_id=offer.id, idempotency_key="same-op")
        self.assertEqual(replay.id, countered.id)
        # ...but reusing it for a different operation (a new top-level offer) is rejected.
        with self.assertRaises(Exception):
            create_offer(conversation=self.conversation, user=self.customer, payload=counter, parent_offer_id=None, idempotency_key="same-op")

    def test_offer_cannot_be_created_before_request_is_complete(self):
        with self.assertRaises(Exception):
            create_offer(conversation=self.conversation, user=self.artisan_user, payload=self.offer_payload())

    def test_request_rejects_wrong_location_for_artisan_service(self):
        self.auth(self.customer)
        response = self.client.patch(f"/api/v1/conversations/{self.conversation.id}/request/", {"service_id": self.service.id, "description": "Fix my sockets.", "location_id": str(self.other_location.id), "preferred_date": (timezone.localdate() + timedelta(days=2)).isoformat()}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_message_idempotency_and_system_message_spoofing(self):
        self.auth(self.customer)
        headers = {"HTTP_IDEMPOTENCY_KEY": "msg-key-1"}
        first = self.client.post(f"/api/v1/conversations/{self.conversation.id}/messages/", {"body": "Hello"}, format="json", **headers)
        second = self.client.post(f"/api/v1/conversations/{self.conversation.id}/messages/", {"body": "Hello"}, format="json", **headers)
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(Message.objects.filter(conversation=self.conversation, sender=self.customer).count(), 1)
        spoof = self.client.post(f"/api/v1/conversations/{self.conversation.id}/messages/", {"body": "fake", "message_type": "SYSTEM", "event_code": "PAYMENT_HELD"}, format="json")
        self.assertEqual(spoof.status_code, status.HTTP_400_BAD_REQUEST)

    def test_message_idempotency_key_cannot_change_payload(self):
        self.auth(self.customer)
        headers = {"HTTP_IDEMPOTENCY_KEY": "msg-key-2"}
        self.client.post(f"/api/v1/conversations/{self.conversation.id}/messages/", {"body": "A"}, format="json", **headers)
        response = self.client.post(f"/api/v1/conversations/{self.conversation.id}/messages/", {"body": "B"}, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_attachment_security_rejects_double_extension_and_fake_image(self):
        self.auth(self.customer)
        fake_image = SimpleUploadedFile("image.php.jpg", b"<?php echo 1; ?>", content_type="image/jpeg")
        response = self.client.post(f"/api/v1/conversations/{self.conversation.id}/messages/", {"body": "bad", "attachments": [fake_image]}, format="multipart", HTTP_IDEMPOTENCY_KEY="attachment-attack-1")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(ConversationAttachment.objects.count(), 0)


    def test_customer_and_artisan_receive_different_request_assistant_context(self):
        self.auth(self.customer)
        customer_response = self.client.get(f"/api/v1/conversations/{self.conversation.id}/")
        self.assertEqual(customer_response.status_code, status.HTTP_200_OK)
        self.assertEqual(customer_response.data["assistant"]["mode"], "collecting_details")
        self.assertEqual(customer_response.data["assistant"]["prompt"]["field"], "service")
        self.assertEqual(
            customer_response.data["next_question"],
            "What service do you need? Choose from this artisan’s available services.",
        )

        self.auth(self.artisan_user)
        artisan_response = self.client.get(f"/api/v1/conversations/{self.conversation.id}/")
        self.assertEqual(artisan_response.status_code, status.HTTP_200_OK)
        self.assertEqual(artisan_response.data["assistant"]["mode"], "waiting_customer_details")
        self.assertIn("customer request is still a draft", artisan_response.data["assistant"]["reply"])
        self.assertIn("No offer can be made until the customer reviews and submits it", artisan_response.data["assistant"]["reply"])
        self.assertIsNone(artisan_response.data["assistant"]["prompt"]["field"])
        self.assertIsNone(artisan_response.data["next_question"])
        self.assertNotIn("SEND_OFFER", artisan_response.data["available_actions"])

    def test_message_response_includes_human_like_assistant_context(self):
        self.auth(self.customer)
        response = self.client.post(f"/api/v1/conversations/{self.conversation.id}/messages/", {"body": "I need the sockets checked."}, format="json", HTTP_IDEMPOTENCY_KEY="assistant-1")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("assistant", response.data)
        self.assertTrue(response.data["assistant"]["reply"])
        self.assertIn("prompt", response.data["assistant"])

    def test_terminal_offer_commercial_fields_are_immutable(self):
        self.complete_request()
        offer = create_offer(conversation=self.conversation, user=self.artisan_user, payload=self.offer_payload(), idempotency_key="immutable-1")
        offer.status = Offer.Status.ACCEPTED
        offer.save(update_fields=["status", "updated_at"])
        offer.total_amount = Decimal("27000.00")
        with self.assertRaises(ValueError): offer.save(update_fields=["total_amount", "updated_at"])

    def test_offer_financial_validation_and_negotiation(self):
        self.complete_request()
        artisan_offer = create_offer(conversation=self.conversation, user=self.artisan_user, payload=self.offer_payload(), idempotency_key="offer-1")
        self.assertEqual(artisan_offer.total_amount, Decimal("25000.00"))
        self.auth(self.customer)
        negative = dict(self.offer_payload()); negative["labour_amount"] = Decimal("-1.00"); negative["total_amount"] = Decimal("4999.00")
        response = self.client.post(f"/api/v1/conversations/{self.conversation.id}/offers/counter/", {**negative, "parent_offer_id": str(artisan_offer.id)}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        counter_payload = self.offer_payload(Decimal("27000.00")); counter_payload["parent_offer_id"] = str(artisan_offer.id)
        response = self.client.post(f"/api/v1/conversations/{self.conversation.id}/offers/counter/", counter_payload, format="json", HTTP_IDEMPOTENCY_KEY="counter-1")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        artisan_offer.refresh_from_db()
        self.assertEqual(artisan_offer.status, Offer.Status.COUNTERED)
        self.assertEqual(response.data["status"], Offer.Status.PROPOSED)

    @patch("payments.tasks.initialize_prepared_payment.delay")
    def test_accept_offer_atomically_creates_booking_and_payment_and_is_replay_safe(self, delay):
        self.complete_request()
        offer = create_offer(conversation=self.conversation, user=self.artisan_user, payload=self.offer_payload(), idempotency_key="accept-offer")
        self.auth(self.customer)
        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.post(f"/api/v1/conversations/{self.conversation.id}/offers/{offer.id}/accept/", format="json", HTTP_IDEMPOTENCY_KEY="accept-1")
        with self.captureOnCommitCallbacks(execute=True):
            second = self.client.post(f"/api/v1/conversations/{self.conversation.id}/offers/{offer.id}/accept/", format="json", HTTP_IDEMPOTENCY_KEY="accept-1")
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        offer.refresh_from_db()
        self.assertEqual(offer.status, Offer.Status.ACCEPTED)
        self.assertEqual(Booking.objects.filter(request_id=self.conversation.service_request_id).count(), 1)
        payment = EscrowPayment.objects.get(booking__request_id=self.conversation.service_request_id)
        self.assertEqual(payment.total_amount_kobo, 2_515_000)
        self.assertEqual(payment.platform_commission_kobo, 250_000)
        self.assertEqual(payment.artisan_payout_kobo, 2_250_000)
        delay.assert_called_once()

    @patch("payments.tasks.initialize_prepared_payment.delay")
    def test_payment_authorization_url_is_hidden_from_artisan_but_visible_to_customer(self, delay):
        self.complete_request()
        offer = create_offer(conversation=self.conversation, user=self.artisan_user, payload=self.offer_payload(), idempotency_key="payment-url-offer")
        self.auth(self.customer)
        with self.captureOnCommitCallbacks(execute=True):
            accepted = self.client.post(
                f"/api/v1/conversations/{self.conversation.id}/offers/{offer.id}/accept/",
                format="json",
                HTTP_IDEMPOTENCY_KEY="payment-url-accept",
            )
        self.assertEqual(accepted.status_code, status.HTTP_200_OK)
        payment = EscrowPayment.objects.get(booking__request_id=self.conversation.service_request_id)
        payment.provider_payload = {"data": {"link": "https://checkout.flutterwave.com/pay/sample"}}
        payment.save(update_fields=["provider_payload"])

        self.auth(self.artisan_user)
        artisan_detail = self.client.get(f"/api/v1/conversations/{self.conversation.id}/")
        self.assertEqual(artisan_detail.status_code, status.HTTP_200_OK)
        self.assertIsNone(artisan_detail.data["payment"]["authorization_url"])

        self.auth(self.customer)
        customer_detail = self.client.get(f"/api/v1/conversations/{self.conversation.id}/")
        self.assertEqual(customer_detail.status_code, status.HTTP_200_OK)
        self.assertEqual(customer_detail.data["payment"]["authorization_url"], "https://checkout.flutterwave.com/pay/sample")
        delay.assert_called_once()

    def test_expired_offer_cannot_be_accepted(self):
        self.complete_request()
        payload = self.offer_payload()
        payload["expires_at"] = timezone.now() + timedelta(seconds=1)
        offer = create_offer(conversation=self.conversation, user=self.artisan_user, payload=payload, idempotency_key="expiring")
        offer.expires_at = timezone.now() - timedelta(seconds=1)
        offer.save(update_fields=["expires_at"])
        self.auth(self.customer)
        response = self.client.post(f"/api/v1/conversations/{self.conversation.id}/offers/{offer.id}/accept/", format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Booking.objects.count(), 0)

    def test_dispute_creates_one_support_case_and_blocks_duplicate(self):
        self.complete_request()
        offer = create_offer(conversation=self.conversation, user=self.artisan_user, payload=self.offer_payload(), idempotency_key="dispute-offer")
        with patch("payments.tasks.initialize_prepared_payment.delay"):
            self.auth(self.customer)
            self.assertEqual(self.client.post(f"/api/v1/conversations/{self.conversation.id}/offers/{offer.id}/accept/", format="json", HTTP_IDEMPOTENCY_KEY="dispute-accept-1").status_code, status.HTTP_200_OK)
        booking = Booking.objects.get(request_id=self.conversation.service_request_id)
        self.assertEqual(booking.status, Booking.Status.ACCEPTED)
        response = self.client.post(f"/api/v1/conversations/{self.conversation.id}/dispute/", {"reason": "The agreed service has not been provided."}, format="json", HTTP_IDEMPOTENCY_KEY="dispute-1")
        self.assertEqual(response.status_code, status.HTTP_200_OK, msg=response.data)
        self.assertEqual(SupportCase.objects.filter(booking=booking, case_type=SupportCase.CaseType.DISPUTE).count(), 1)
        case = SupportCase.objects.get(booking=booking, case_type=SupportCase.CaseType.DISPUTE)
        self.assertEqual(response.data["dispute_case"]["id"], case.id)
        self.assertEqual(response.data["dispute_case"]["reference"], case.reference)
        self.assertEqual(response.data["dispute_case"]["status"], case.status)
        duplicate = self.client.post(f"/api/v1/conversations/{self.conversation.id}/dispute/", {"reason": "The customer has already opened this service dispute."}, format="json", HTTP_IDEMPOTENCY_KEY="dispute-2")
        self.assertEqual(duplicate.status_code, status.HTTP_400_BAD_REQUEST)
        booking.refresh_from_db()
        self.assertEqual(booking.status, Booking.Status.DISPUTED)

    def test_conversation_list_is_bounded_in_queries(self):
        self.auth(self.customer)
        for index in range(20):
            other = self.make_artisan(f"artisan-{index}@example.com")
            Conversation.objects.create(customer=self.customer, artisan=other[1])
        with CaptureQueriesContext(connection) as captured:
            started = perf_counter()
            response = self.client.get("/api/v1/conversations/")
            elapsed_ms = (perf_counter() - started) * 1000
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertLess(len(captured), 8, msg=f"Unexpected query count: {len(captured)}")
        self.assertLess(elapsed_ms, 2000, msg=f"Local test environment exceeded sanity ceiling: {elapsed_ms:.2f}ms")

    def test_incomplete_request_draft_has_human_readable_service_and_location_labels(self):
        self.conversation.draft = {
            "service_id": str(self.service.id),
            "category_id": self.category.pk,
            "location_id": str(self.location.id),
            "preferred_date": (timezone.localdate() + timedelta(days=2)).isoformat(),
            "description": "Replace a damaged socket.",
        }
        self.conversation.save(update_fields=["draft", "updated_at"])
        self.auth(self.customer)

        response = self.client.get(f"/api/v1/conversations/{self.conversation.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["request_draft"]["service_name"], self.service.name)
        self.assertEqual(response.data["request_draft"]["location_label"], "Ikeja, Lagos")

    def test_detail_endpoint_performance_budget(self):
        self.auth(self.customer)
        with CaptureQueriesContext(connection) as captured:
            started = perf_counter()
            response = self.client.get(f"/api/v1/conversations/{self.conversation.id}/")
            elapsed_ms = (perf_counter() - started) * 1000
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertLess(len(captured), 12, msg=f"Unexpected query count: {len(captured)}")
        self.assertLess(elapsed_ms, 200, msg=f"Conversation detail exceeded 200ms local API budget: {elapsed_ms:.2f}ms")
