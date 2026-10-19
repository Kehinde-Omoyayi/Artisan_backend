from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from home.models import (
    ArtisanProfile,
    ArtisanService,
    Institution,
    Locations,
    Service,
    ServiceCategory,
)
from services.models import ArtisanVerification
from support.permissions import SUPPORT_AGENT, SUPPORT_FINANCE

from .models import (
    PromotionApplication,
    PromotionApplicationDecision,
    PromotionApplicationEvidence,
    PromotionApplicationRevision,
    PromotionCampaign,
    PromotionCampaignAction,
    PromotionInventory,
    PromotionPayment,
    PromotionPlan,
)
from support.models import SupportCase
from support.permissions import SUPPORT_SUPERVISOR


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "promotion-tests",
    }
}


@override_settings(CACHES=TEST_CACHES)
class PromotionApplicationApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user_model = get_user_model()
        self.ikeja = Locations.objects.create(country="Nigeria", state="Lagos", area="Ikeja")
        self.yaba = Locations.objects.create(country="Nigeria", state="Lagos", area="Yaba")
        self.ikeja_east = Locations.objects.create(country="Nigeria", state="Lagos", area="Ikeja East")
        self.other_state = Locations.objects.create(country="Nigeria", state="Ogun", area="Abeokuta")
        category = ServiceCategory.objects.create(name="Home Repair")
        self.service = Service.objects.create(
            category=category,
            name="Pipe Repair",
            description="Repair and maintenance of water pipes.",
            is_active=True,
        )
        self.artisan_user, self.artisan = self.make_artisan(
            "maker@example.test",
            business_name="Ayo Pipe Works",
            location=self.ikeja,
        )
        self.ikeja_offer = ArtisanService.objects.create(
            artisan=self.artisan,
            service=self.service,
            location=self.ikeja,
            starting_price="15000.00",
        )
        self.yaba_offer = ArtisanService.objects.create(
            artisan=self.artisan,
            service=self.service,
            location=self.yaba,
            starting_price="14000.00",
        )
        self.anywhere_offer = ArtisanService.objects.create(
            artisan=self.artisan,
            service=self.service,
            location=None,
            starting_price="16000.00",
        )
        self.institution = Institution.objects.create(
            name="Supported Technical College",
            campus="Ikeja Campus",
            abbreviation="STC",
            location=self.ikeja,
            is_active=True,
        )

    def make_artisan(self, email, *, business_name="Professional Artisan", location=None, profile_status="ACTIVE", verification_status="VERIFIED", bio="Experienced professional offering careful, reliable repair work.", phone_number="+2348012345678"):
        user = self.user_model.objects.create_user(
            email=email,
            first_name="Ayo",
            last_name="Artisan",
            phone_number=phone_number,
            password="StrongPassword123!",
            is_active=True,
        )
        artisan = ArtisanProfile.objects.create(
            user=user,
            business_name=business_name,
            bio=bio,
            years_experience=6,
            country=location.country if location else "Nigeria",
            state=location.state if location else "Lagos",
            area=location.area if location else "Ikeja",
            profile_status=profile_status,
        )
        if verification_status:
            ArtisanVerification.objects.create(
                artisan=artisan,
                verification_status=verification_status,
                verification_method="NIN",
            )
        return user, artisan

    def payload(self, promotion_type="AREA", *, target=None, service_ids=None):
        data = {
            "promotion_type": promotion_type,
            "service_ids": service_ids if service_ids is not None else [self.ikeja_offer.pk],
            "promotional_description": (
                "Reliable local pipe repair with clear communication and careful workmanship for homes and small businesses."
            ),
        }
        if promotion_type == PromotionApplication.PromotionType.INSTITUTION:
            data["institution_id"] = str((target or self.institution).pk)
        elif promotion_type == PromotionApplication.PromotionType.AREA:
            data["area_location_id"] = str((target or self.ikeja).pk)
        elif promotion_type == PromotionApplication.PromotionType.STATE:
            data["state_country"] = "Nigeria"
            data["state_name"] = "Lagos"
        return data

    def create_application(self, promotion_type="AREA", *, target=None, service_ids=None):
        self.client.force_authenticate(self.artisan_user)
        with patch("promotions.services.notify_promotion_application_submitted") as notify:
            response = self.client.post(
                "/api/v1/promotions/applications/",
                self.payload(promotion_type, target=target, service_ids=service_ids),
                format="json",
            )
        self.assertEqual(response.status_code, 201, response.data)
        notify.assert_called_once()
        return response.data

    def support_user(self, *, role=SUPPORT_AGENT, email="support@example.test"):
        user = self.user_model.objects.create_user(
            email=email,
            first_name="Support",
            last_name="Reviewer",
            password="StrongPassword123!",
            is_active=True,
            is_staff=True,
        )
        group, _ = Group.objects.get_or_create(name=role)
        user.groups.add(group)
        return user

    def test_eligibility_uses_server_profile_verification_and_public_active_service_rules(self):
        self.client.force_authenticate(self.artisan_user)
        response = self.client.get("/api/v1/promotions/eligibility/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["eligible"], response.data)
        self.assertEqual(response.data["active_service_count"], 3)
        self.assertEqual(response.data["profile_status"], "ACTIVE")
        self.assertEqual(response.data["verification_status"], "VERIFIED")

        # Optional social/portfolio fields deliberately remain empty and do not block eligibility.
        self.assertFalse(self.artisan.portfolio_url)
        self.assertFalse(self.artisan.social_media_1_url)

    def test_incomplete_profile_or_unverified_identity_blocks_submission(self):
        self.artisan.bio = ""
        self.artisan.save(update_fields=["bio"])
        self.client.force_authenticate(self.artisan_user)

        response = self.client.post(
            "/api/v1/promotions/applications/",
            self.payload(),
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("eligibility", response.data["details"])
        self.assertTrue(any("professional description" in str(item).lower() for item in response.data["details"]["eligibility"]))
        self.assertEqual(PromotionApplication.objects.count(), 0)

        self.artisan.bio = "Experienced professional offering careful, reliable repair work."
        self.artisan.profile_status = "INACTIVE"
        self.artisan.save(update_fields=["bio", "profile_status"])
        response = self.client.post(
            "/api/v1/promotions/applications/",
            self.payload(),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(any("Active status" in str(item) for item in response.data["details"]["eligibility"]))
        self.assertEqual(PromotionApplication.objects.count(), 0)

    def test_target_and_service_scope_must_match_exact_catalogue_location(self):
        self.client.force_authenticate(self.artisan_user)
        wrong_area = self.client.post(
            "/api/v1/promotions/applications/",
            self.payload("AREA", target=self.yaba, service_ids=[self.ikeja_offer.pk]),
            format="json",
        )
        self.assertEqual(wrong_area.status_code, 400)
        self.assertIn("service_ids", wrong_area.data["details"])
        self.assertEqual(PromotionApplication.objects.count(), 0)

        wrong_institution = Institution.objects.create(
            name="Another supported college",
            campus="Yaba Campus",
            location=self.yaba,
        )
        wrong_institution_response = self.client.post(
            "/api/v1/promotions/applications/",
            self.payload("INSTITUTION", target=wrong_institution, service_ids=[self.ikeja_offer.pk]),
            format="json",
        )
        self.assertEqual(wrong_institution_response.status_code, 400)
        self.assertIn("exact selected target", str(wrong_institution_response.data))

        accepted = self.client.post(
            "/api/v1/promotions/applications/",
            self.payload("AREA", target=self.ikeja, service_ids=[self.ikeja_offer.pk]),
            format="json",
        )
        self.assertEqual(accepted.status_code, 201, accepted.data)
        self.assertEqual(accepted.data["area_location_id"], str(self.ikeja.pk))

    def test_general_anywhere_service_is_not_mistaken_for_registered_location(self):
        self.client.force_authenticate(self.artisan_user)
        response = self.client.post(
            "/api/v1/promotions/applications/",
            self.payload("AREA", target=self.other_state, service_ids=[self.anywhere_offer.pk]),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["services"][0]["location"])
        self.assertEqual(response.data["target"]["area"], "Abeokuta")

    def test_each_product_is_target_specific_and_exact_duplicate_is_blocked(self):
        self.client.force_authenticate(self.artisan_user)
        first = self.client.post(
            "/api/v1/promotions/applications/",
            self.payload("AREA", target=self.ikeja),
            format="json",
        )
        self.assertEqual(first.status_code, 201, first.data)

        duplicate = self.client.post(
            "/api/v1/promotions/applications/",
            self.payload("AREA", target=self.ikeja),
            format="json",
        )
        self.assertEqual(duplicate.status_code, 400)

        distinct_products = [
            self.payload("INSTITUTION"),
            self.payload("STATE"),
            self.payload("HOMEPAGE"),
            self.payload("AREA", target=self.ikeja_east, service_ids=[self.anywhere_offer.pk]),
        ]
        for data in distinct_products:
            with patch("promotions.services.notify_promotion_application_submitted"):
                response = self.client.post("/api/v1/promotions/applications/", data, format="json")
            self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(PromotionApplication.objects.filter(artisan=self.artisan).count(), 5)

    def test_artisan_can_withdraw_approved_application_before_checkout(self):
        application = self.create_application("AREA", target=self.ikeja)
        agent = self.support_user()
        self.client.force_authenticate(agent)
        with patch("promotions.services.notify_promotion_application_decision"):
            approved = self.client.post(
                f"/api/v1/promotions/review/applications/{application['id']}/decision/",
                {"decision": "APPROVED", "reason": "The target and selected services match the submitted work."},
                format="json",
            )
        self.assertEqual(approved.status_code, 200, approved.data)

        self.client.force_authenticate(self.artisan_user)
        withdrawn = self.client.post(
            f"/api/v1/promotions/applications/{application['id']}/withdraw/",
            {},
            format="json",
        )

        self.assertEqual(withdrawn.status_code, 200, withdrawn.data)
        self.assertEqual(withdrawn.data["status"], "WITHDRAWN")
        self.assertEqual(withdrawn.data["latest_decision"]["decision"], "WITHDRAWN")
        decisions = PromotionApplicationDecision.objects.filter(application_id=application["id"]).order_by("created_at")
        self.assertEqual(list(decisions.values_list("decision_type", flat=True)), ["APPROVED", "WITHDRAWN"])

    def test_applicant_cannot_set_lifecycle_status_directly(self):
        application = self.create_application()
        self.client.force_authenticate(self.artisan_user)

        response = self.client.patch(
            f"/api/v1/promotions/applications/{application['id']}/",
            {"status": "APPROVED"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        record = PromotionApplication.objects.get(pk=application["id"])
        self.assertEqual(record.status, PromotionApplication.Status.PENDING_REVIEW)

    def test_review_queue_is_restricted_and_approval_records_exact_decision(self):
        application_data = self.create_application("AREA", target=self.ikeja)
        application_id = application_data["id"]
        agent = self.support_user()
        self.client.force_authenticate(self.artisan_user)
        denied = self.client.get("/api/v1/promotions/review/applications/")
        self.assertEqual(denied.status_code, 403)

        finance = self.support_user(role=SUPPORT_FINANCE, email="finance@example.test")
        self.client.force_authenticate(finance)
        finance_denied = self.client.get("/api/v1/promotions/review/applications/")
        self.assertEqual(finance_denied.status_code, 403)

        self.client.force_authenticate(agent)
        queue = self.client.get("/api/v1/promotions/review/applications/")
        self.assertEqual(queue.status_code, 200, queue.data)
        self.assertEqual(queue.data["count"], 1)
        detail = self.client.get(f"/api/v1/promotions/review/applications/{application_id}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        snapshot = detail.data["current_revision_snapshot"]
        self.assertEqual(snapshot["promotion_type"], "AREA")
        self.assertEqual(snapshot["target"]["area"], "Ikeja")
        self.assertEqual(snapshot["services"][0]["service_name"], "Pipe Repair")
        self.assertEqual(snapshot["profile"]["verification_status"], "VERIFIED")

        with patch("promotions.services.notify_promotion_application_decision") as notify:
            approved = self.client.post(
                f"/api/v1/promotions/review/applications/{application_id}/decision/",
                {"decision": "APPROVED", "reason": "The target and selected services match the submitted work."},
                format="json",
            )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(approved.data["status"], "APPROVED")
        notify.assert_called_once()
        record = PromotionApplication.objects.get(pk=application_id)
        self.assertEqual(record.last_reviewed_by, agent)
        self.assertIsNotNone(record.reviewed_at)
        decision = PromotionApplicationDecision.objects.get(application=record)
        self.assertEqual(decision.revision.revision_number, 1)
        self.assertEqual(decision.actor, agent)
        self.assertEqual(decision.reason, "The target and selected services match the submitted work.")

    def test_approval_rechecks_live_eligibility_and_leaves_application_pending_on_failure(self):
        application = self.create_application()
        agent = self.support_user()
        self.artisan.profile_status = "INACTIVE"
        self.artisan.save(update_fields=["profile_status"])
        self.client.force_authenticate(agent)

        response = self.client.post(
            f"/api/v1/promotions/review/applications/{application['id']}/decision/",
            {"decision": "APPROVED", "reason": "The application is suitable for the selected target."},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("eligibility", response.data["details"])
        record = PromotionApplication.objects.get(pk=application["id"])
        self.assertEqual(record.status, PromotionApplication.Status.PENDING_REVIEW)
        self.assertFalse(record.decisions.exists())

    def test_approval_is_bound_to_the_exact_reviewed_description_and_service_snapshot(self):
        application = self.create_application("AREA", target=self.ikeja)
        record = PromotionApplication.objects.get(pk=application["id"])
        record.promotional_description = "Unreviewed description changed after the application was submitted."
        record.save(update_fields=["promotional_description"])
        record.services.set([self.yaba_offer])
        agent = self.support_user()
        self.client.force_authenticate(agent)

        response = self.client.post(
            f"/api/v1/promotions/review/applications/{application['id']}/decision/",
            {"decision": "APPROVED", "reason": "The application is suitable for the selected target."},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        details = str(response.data["details"]).lower()
        self.assertIn("description changed after submission", details)
        self.assertIn("selected services changed after submission", details)
        record.refresh_from_db()
        self.assertEqual(record.status, PromotionApplication.Status.PENDING_REVIEW)
        self.assertFalse(record.decisions.exists())

    def test_changes_required_then_resubmit_creates_a_new_immutable_revision(self):
        application = self.create_application("AREA", target=self.ikeja)
        agent = self.support_user()
        self.client.force_authenticate(agent)
        feedback = "Please clarify the work examples and how this locality is served."
        with patch("promotions.services.notify_promotion_application_decision"):
            changed = self.client.post(
                f"/api/v1/promotions/review/applications/{application['id']}/decision/",
                {"decision": "CHANGES_REQUIRED", "reason": feedback},
                format="json",
            )
        self.assertEqual(changed.status_code, 200, changed.data)
        self.assertEqual(changed.data["status"], "CHANGES_REQUIRED")

        self.client.force_authenticate(self.artisan_user)
        current = self.client.get(f"/api/v1/promotions/applications/{application['id']}/")
        self.assertEqual(current.status_code, 200, current.data)
        self.assertEqual(current.data["latest_decision"]["reason"], feedback)
        new_payload = self.payload("AREA", target=self.ikeja)
        new_payload["promotional_description"] = "Updated description with clear examples of careful pipe repair for households and small businesses in Ikeja."
        with patch("promotions.services.notify_promotion_application_submitted"):
            resubmitted = self.client.patch(
                f"/api/v1/promotions/applications/{application['id']}/",
                new_payload,
                format="json",
            )
        self.assertEqual(resubmitted.status_code, 200, resubmitted.data)
        self.assertEqual(resubmitted.data["status"], "PENDING_REVIEW")
        self.assertEqual(resubmitted.data["latest_revision_number"], 2)
        self.assertEqual(PromotionApplicationRevision.objects.filter(application_id=application["id"]).count(), 2)
        self.assertEqual(PromotionApplicationDecision.objects.filter(application_id=application["id"]).count(), 1)
        revisions = PromotionApplicationRevision.objects.filter(application_id=application["id"]).order_by("revision_number")
        self.assertNotEqual(revisions[0].snapshot["promotional_description"], revisions[1].snapshot["promotional_description"])

    def test_support_capability_exposes_promotion_review_separately_from_finance(self):
        agent = self.support_user()
        self.client.force_authenticate(agent)
        response = self.client.get("/api/v1/support/cases/capabilities/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["can_review_promotions"])

        finance = self.support_user(role=SUPPORT_FINANCE, email="capability-finance@example.test")
        self.client.force_authenticate(finance)
        response = self.client.get("/api/v1/support/cases/capabilities/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["can_review_promotions"])

    def test_evidence_is_private_to_applicant_and_authorized_reviewers(self):
        # Evidence persistence is covered using a small valid PNG rather than a filename-only upload.
        from io import BytesIO
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        import tempfile

        buffer = BytesIO()
        Image.new("RGB", (8, 8), color="navy").save(buffer, format="PNG")
        buffer.seek(0)
        upload = SimpleUploadedFile("sample.png", buffer.read(), content_type="image/png")
        payload = self.payload("AREA", target=self.ikeja)
        payload.pop("service_ids")
        payload["service_ids"] = [self.ikeja_offer.pk]
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            self.client.force_authenticate(self.artisan_user)
            with patch("promotions.services.notify_promotion_application_submitted"):
                response = self.client.post(
                    "/api/v1/promotions/applications/",
                    {**payload, "evidence_images": [upload]},
                    format="multipart",
                )
            self.assertEqual(response.status_code, 201, response.data)
            evidence = PromotionApplicationEvidence.objects.get()
            self.assertTrue(evidence.image.name.endswith(".webp"))
            private_url = f"/api/v1/promotions/applications/{response.data['id']}/evidence/{evidence.id}/"
            own = self.client.get(private_url)
            self.assertEqual(own.status_code, 200)
            self.assertEqual(own["Content-Type"], "image/webp")

            other_user, _other_artisan = self.make_artisan(
                "other-maker@example.test",
                business_name="Another Artisan",
                location=self.ikeja,
                phone_number="+2348012345679",
            )
            self.client.force_authenticate(other_user)
            denied = self.client.get(private_url)
            self.assertEqual(denied.status_code, 403)

            agent = self.support_user()
            self.client.force_authenticate(agent)
            reviewer = self.client.get(private_url)
            self.assertEqual(reviewer.status_code, 200)

    def configure_promotion_plan(self, promotion_type="AREA", *, enabled=True, price_kobo=200000, duration_days=7, terms_text="Promotion terms: schedule is assigned by trusted payment verification."):
        plan, _ = PromotionPlan.objects.update_or_create(
            promotion_type=promotion_type,
            defaults={
                "name": f"{promotion_type.title()} Promotion",
                "is_enabled": enabled,
                "price_kobo": price_kobo,
                "currency": "NGN",
                "duration_days": duration_days,
                "terms_text": terms_text,
                "version": 1,
                "refund_policy": PromotionPlan.RefundPolicy.PRO_RATA,
            },
        )
        return plan

    def approve_promotion_application(self, *, promotion_type="AREA", artisan_user=None, target=None, service_ids=None):
        artisan_user = artisan_user or self.artisan_user
        if artisan_user == self.artisan_user:
            application_data = self.create_application(promotion_type, target=target, service_ids=service_ids)
        else:
            self.client.force_authenticate(artisan_user)
            payload = {
                "promotion_type": promotion_type,
                "service_ids": service_ids or [],
                "promotional_description": "Reliable local repair work with clear communication and careful workmanship for homes and small businesses.",
            }
            if promotion_type == "AREA":
                payload["area_location_id"] = str(target.pk)
            elif promotion_type == "INSTITUTION":
                payload["institution_id"] = str(target.pk)
            elif promotion_type == "STATE":
                payload.update({"state_country": "Nigeria", "state_name": "Lagos"})
            with patch("promotions.services.notify_promotion_application_submitted"):
                submitted = self.client.post("/api/v1/promotions/applications/", payload, format="json")
            self.assertEqual(submitted.status_code, 201, submitted.data)
            application_data = submitted.data

        reviewer = self.support_user(email=f"promotion-review-{PromotionApplication.objects.count()}@example.test")
        self.client.force_authenticate(reviewer)
        with patch("promotions.services.notify_promotion_application_decision"):
            approved = self.client.post(
                f"/api/v1/promotions/review/applications/{application_data['id']}/decision/",
                {"decision": "APPROVED", "reason": "The exact target and selected services match the reviewed work."},
                format="json",
            )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.client.force_authenticate(artisan_user)
        return PromotionApplication.objects.select_related("artisan__user", "area_location", "institution__location").get(pk=application_data["id"])

    def start_promotion_checkout(self, application, *, idempotency_key=None):
        self.client.force_authenticate(application.artisan.user)
        offer = self.client.get(f"/api/v1/promotions/applications/{application.id}/offer/")
        self.assertEqual(offer.status_code, 200, offer.data)
        checkout = self.client.post(
            f"/api/v1/promotions/applications/{application.id}/checkout/",
            {
                "quote_token": offer.data["quote_token"],
                "accepted_terms": True,
                "accepted_terms_version": offer.data["terms_version"],
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=idempotency_key or f"promotion-{application.id}",
        )
        return offer, checkout

    def verify_promotion_payment(self, payment, transaction_id, *, expect_provider_lookup=True):
        from promotions.tasks import verify_promotion_payment

        verified_result = {
            "status": "success",
            "data": {
                "id": str(transaction_id),
                "tx_ref": payment.reference,
                "currency": payment.currency,
                "amount": str(payment.amount_kobo / 100),
                "status": "successful",
                "flw_ref": f"FLW-{transaction_id}",
            },
        }
        with patch("promotions.tasks.FlutterwaveClient.verify_transaction", return_value=verified_result) as verify:
            result = verify_promotion_payment.run(str(payment.id), str(transaction_id))
        if expect_provider_lookup:
            verify.assert_called_once_with(str(transaction_id))
        else:
            verify.assert_not_called()
        return result

    def test_offer_reads_manager_configuration_and_homepage_stays_unavailable_until_enabled(self):
        self.configure_promotion_plan(price_kobo=275000, duration_days=9, terms_text="Manager-authored local placement terms.")
        application = self.approve_promotion_application(promotion_type="AREA", target=self.ikeja)
        offer = self.client.get(f"/api/v1/promotions/applications/{application.id}/offer/")

        self.assertEqual(offer.status_code, 200, offer.data)
        self.assertEqual(offer.data["price"], "2750")
        self.assertEqual(offer.data["duration_days"], 9)
        self.assertEqual(offer.data["terms_text"], "Manager-authored local placement terms.")
        self.assertEqual(offer.data["target"]["target_key"], f"AREA:{self.ikeja.pk}")
        self.assertEqual(offer.data["availability"], "AVAILABLE")
        self.assertIsNotNone(offer.data["earliest_start_at"])

        self.configure_promotion_plan("HOMEPAGE", enabled=False, price_kobo=0, terms_text="")
        homepage_application = self.approve_promotion_application(promotion_type="HOMEPAGE", target=None)
        homepage_offer = self.client.get(f"/api/v1/promotions/applications/{homepage_application.id}/offer/")
        self.assertEqual(homepage_offer.status_code, 200, homepage_offer.data)
        self.assertEqual(homepage_offer.data["availability"], "UNAVAILABLE")
        self.assertIn("Homepage Feature remains unavailable", homepage_offer.data["availability_reason"])

    def test_checkout_is_exact_offer_idempotent_and_not_marked_paid_by_browser_submission(self):
        plan = self.configure_promotion_plan()
        application = self.approve_promotion_application(promotion_type="AREA", target=self.ikeja)
        offer, first = self.start_promotion_checkout(application, idempotency_key="promotion-checkout-replay")

        self.assertEqual(first.status_code, 202, first.data)
        self.assertEqual(first.data["payment_status"], PromotionPayment.Status.INITIALIZATION_PENDING)
        self.assertEqual(first.data["campaign_status"], PromotionCampaign.Status.PENDING_PAYMENT)
        self.assertTrue(first.data["initialization_pending"])
        self.assertEqual(first.data["amount"], "2000")
        self.assertEqual(first.data["currency"], "NGN")
        self.assertEqual(PromotionPlan.objects.get(pk=plan.pk).price_kobo, 200000)

        campaign = PromotionCampaign.objects.get(pk=first.data["campaign_id"])
        payment = PromotionPayment.objects.get(pk=first.data["payment_id"])
        self.assertEqual(campaign.status, PromotionCampaign.Status.PENDING_PAYMENT)
        self.assertIsNone(campaign.payment_verified_at)
        self.assertIsNone(campaign.verified_payment_id)
        self.assertEqual(campaign.plan_name_snapshot, plan.name)
        self.assertEqual(campaign.duration_days, 7)
        self.assertEqual(campaign.accepted_terms_version, offer.data["terms_version"])

        replay = self.client.post(
            f"/api/v1/promotions/applications/{application.id}/checkout/",
            {
                "quote_token": offer.data["quote_token"],
                "accepted_terms": True,
                "accepted_terms_version": offer.data["terms_version"],
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="promotion-checkout-replay",
        )
        self.assertEqual(replay.status_code, 202, replay.data)
        self.assertEqual(replay["Idempotent-Replayed"], "true")
        self.assertEqual(PromotionPayment.objects.filter(campaign=campaign).count(), 1)
        self.assertEqual(PromotionPayment.objects.get(pk=payment.pk).status, PromotionPayment.Status.INITIALIZATION_PENDING)

        # A quote is bound to the current plan revision, price, duration and terms.
        plan.price_kobo = 300000
        plan.version += 1
        plan.save(update_fields=["price_kobo", "version", "updated_at"])
        self.client.force_authenticate(self.artisan_user)
        stale = self.client.post(
            f"/api/v1/promotions/applications/{application.id}/checkout/",
            {
                "quote_token": offer.data["quote_token"],
                "accepted_terms": True,
                "accepted_terms_version": offer.data["terms_version"],
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="promotion-checkout-stale-quote",
        )
        self.assertEqual(stale.status_code, 400, stale.data)
        self.assertEqual(PromotionPayment.objects.filter(campaign=campaign).count(), 1)

    def test_trusted_flutterwave_verification_controls_paid_queue_and_is_idempotent(self):
        from promotions.campaign_services import recompute_inventory_schedule

        self.configure_promotion_plan(duration_days=7)
        inventory, _ = PromotionInventory.objects.get_or_create(
            target_key=f"AREA:{self.ikeja.pk}",
            defaults={"promotion_type": "AREA", "area_location": self.ikeja, "target_label": str(self.ikeja), "capacity": 1},
        )
        inventory.capacity = 1
        inventory.save(update_fields=["capacity", "updated_at"])

        first_application = self.approve_promotion_application(promotion_type="AREA", target=self.ikeja)
        _, first_checkout = self.start_promotion_checkout(first_application, idempotency_key="promotion-payment-first")
        self.assertEqual(first_checkout.status_code, 202, first_checkout.data)
        first_payment = PromotionPayment.objects.get(pk=first_checkout.data["payment_id"])
        first_result = self.verify_promotion_payment(first_payment, "930001")
        self.assertEqual(first_result["status"], "verified")
        first_campaign = PromotionCampaign.objects.get(pk=first_checkout.data["campaign_id"])
        self.assertEqual(first_campaign.status, PromotionCampaign.Status.QUEUED)
        self.assertIsNotNone(first_campaign.payment_verified_at)
        self.assertEqual(first_campaign.verified_payment_id, first_payment.pk)
        recompute_inventory_schedule(inventory.id)
        first_campaign.refresh_from_db()
        self.assertEqual(first_campaign.status, PromotionCampaign.Status.ACTIVE)

        second_user, second_artisan = self.make_artisan(
            "promotion-second-maker@example.test",
            business_name="Second Pipe Works",
            location=self.ikeja,
            phone_number="+2348012345680",
        )
        second_offer = ArtisanService.objects.create(
            artisan=second_artisan,
            service=self.service,
            location=self.ikeja,
            starting_price="14500.00",
        )
        second_application = self.approve_promotion_application(
            promotion_type="AREA",
            artisan_user=second_user,
            target=self.ikeja,
            service_ids=[second_offer.pk],
        )
        _, second_checkout = self.start_promotion_checkout(second_application, idempotency_key="promotion-payment-second")
        self.assertEqual(second_checkout.status_code, 202, second_checkout.data)
        second_payment = PromotionPayment.objects.get(pk=second_checkout.data["payment_id"])
        second_result = self.verify_promotion_payment(second_payment, "930002")
        self.assertEqual(second_result["status"], "verified")
        recompute_inventory_schedule(inventory.id)
        second_campaign = PromotionCampaign.objects.get(pk=second_checkout.data["campaign_id"])
        first_campaign.refresh_from_db()
        self.assertEqual(first_campaign.status, PromotionCampaign.Status.ACTIVE)
        self.assertEqual(second_campaign.status, PromotionCampaign.Status.SCHEDULED)
        self.assertGreaterEqual(second_campaign.scheduled_start_at, first_campaign.expires_at)
        self.assertGreaterEqual(second_campaign.payment_verified_at, first_campaign.payment_verified_at)

        duplicate = self.verify_promotion_payment(first_payment, "930001", expect_provider_lookup=False)
        self.assertEqual(duplicate["status"], PromotionPayment.Status.VERIFIED)
        self.assertEqual(PromotionCampaignAction.objects.filter(campaign=first_campaign, action=PromotionCampaignAction.ActionType.PAYMENT_VERIFIED).count(), 1)

    def test_pause_and_restore_preserve_remaining_time_without_jumping_paid_queue(self):
        from promotions.campaign_services import pause_or_restore_or_revoke_campaign, recompute_inventory_schedule

        self.configure_promotion_plan(duration_days=7)
        inventory, _ = PromotionInventory.objects.get_or_create(
            target_key=f"AREA:{self.ikeja.pk}",
            defaults={"promotion_type": "AREA", "area_location": self.ikeja, "target_label": str(self.ikeja), "capacity": 1},
        )
        inventory.capacity = 1
        inventory.save(update_fields=["capacity", "updated_at"])

        first_application = self.approve_promotion_application(promotion_type="AREA", target=self.ikeja)
        _, first_checkout = self.start_promotion_checkout(first_application, idempotency_key="promotion-lifecycle-first")
        first_payment = PromotionPayment.objects.get(pk=first_checkout.data["payment_id"])
        self.verify_promotion_payment(first_payment, "940001")
        first_campaign = PromotionCampaign.objects.get(pk=first_checkout.data["campaign_id"])
        recompute_inventory_schedule(inventory.id)
        first_campaign.refresh_from_db()
        self.assertEqual(first_campaign.status, PromotionCampaign.Status.ACTIVE)

        second_user, second_artisan = self.make_artisan(
            "promotion-lifecycle-second@example.test",
            business_name="Second Local Pipe Works",
            location=self.ikeja,
            phone_number="+2348012345681",
        )
        second_offer = ArtisanService.objects.create(artisan=second_artisan, service=self.service, location=self.ikeja, starting_price="14500.00")
        second_application = self.approve_promotion_application(promotion_type="AREA", artisan_user=second_user, target=self.ikeja, service_ids=[second_offer.pk])
        _, second_checkout = self.start_promotion_checkout(second_application, idempotency_key="promotion-lifecycle-second")
        second_payment = PromotionPayment.objects.get(pk=second_checkout.data["payment_id"])
        self.verify_promotion_payment(second_payment, "940002")
        recompute_inventory_schedule(inventory.id)
        second_campaign = PromotionCampaign.objects.get(pk=second_checkout.data["campaign_id"])
        self.assertEqual(second_campaign.status, PromotionCampaign.Status.SCHEDULED)

        case = SupportCase.objects.create(
            opened_by=self.artisan_user,
            promotion_campaign=first_campaign,
            case_type=SupportCase.CaseType.REPORT,
            subject="Please review this placement",
            description="A support case linked to the campaign for a fair review.",
        )
        agent = self.support_user(role=SUPPORT_AGENT, email="promotion-case-agent@example.test")
        with patch("promotions.campaign_services.notify_promotion_campaign_update"):
            paused, pause_action, _ = pause_or_restore_or_revoke_campaign(
                first_campaign.id,
                actor=agent,
                action="SUSPEND",
                reason="Temporarily suspend while the support team reviews the evidence.",
                evidence_summary="The case is under neutral review; no violation is presumed.",
                support_case_id=case.id,
            )
        self.assertEqual(paused.status, PromotionCampaign.Status.SUSPENDED)
        self.assertEqual(pause_action.action, PromotionCampaignAction.ActionType.SUSPENDED)
        self.assertGreater(paused.remaining_duration_seconds, 0)
        self.assertLessEqual(paused.remaining_duration_seconds, 7 * 86400)
        self.assertFalse(paused.is_publicly_active)
        remaining = paused.remaining_duration_seconds
        recompute_inventory_schedule(inventory.id)
        second_campaign.refresh_from_db()
        self.assertEqual(second_campaign.status, PromotionCampaign.Status.ACTIVE)

        supervisor = self.support_user(role=SUPPORT_SUPERVISOR, email="promotion-case-supervisor@example.test")
        with patch("promotions.campaign_services.notify_promotion_campaign_update"):
            restored, restore_action, _ = pause_or_restore_or_revoke_campaign(
                first_campaign.id,
                actor=supervisor,
                action="RESTORE",
                reason="Review is complete; restore the campaign in normal paid order.",
                evidence_summary="The review found no confirmed violation; paid service time remains.",
                support_case_id=case.id,
            )
        self.assertEqual(restored.status, PromotionCampaign.Status.QUEUED)
        self.assertEqual(restore_action.action, PromotionCampaignAction.ActionType.RESTORED)
        self.assertEqual(restored.remaining_duration_seconds, remaining)
        self.assertIsNotNone(restored.queue_reentry_at)
        recompute_inventory_schedule(inventory.id)
        restored.refresh_from_db()
        second_campaign.refresh_from_db()
        self.assertEqual(second_campaign.status, PromotionCampaign.Status.ACTIVE)
        self.assertEqual(restored.status, PromotionCampaign.Status.SCHEDULED)
        self.assertGreaterEqual(restored.scheduled_start_at, second_campaign.expires_at)
        self.assertEqual(restored.remaining_duration_seconds, remaining)

    def test_redirect_callback_and_signed_webhook_only_enqueue_trusted_verification(self):
        import base64
        import hashlib
        import hmac
        import json

        from payments.models import PaymentWebhookEvent
        from django.test import override_settings

        self.configure_promotion_plan()
        application = self.approve_promotion_application(promotion_type="AREA", target=self.ikeja)
        _, checkout = self.start_promotion_checkout(application, idempotency_key="promotion-callback-webhook")
        self.assertEqual(checkout.status_code, 202, checkout.data)
        payment = PromotionPayment.objects.get(pk=checkout.data["payment_id"])
        campaign = PromotionCampaign.objects.get(pk=checkout.data["campaign_id"])

        with patch("promotions.tasks.verify_promotion_payment.delay") as verify_callback:
            callback = self.client.get(
                "/api/v1/payments/flutterwave/callback/",
                {"transaction_id": "950001", "tx_ref": payment.reference, "status": "successful"},
            )
        self.assertEqual(callback.status_code, 202)
        verify_callback.assert_called_once_with(str(payment.id), "950001")
        payment.refresh_from_db()
        campaign.refresh_from_db()
        self.assertEqual(payment.status, PromotionPayment.Status.INITIALIZATION_PENDING)
        self.assertEqual(campaign.status, PromotionCampaign.Status.PENDING_PAYMENT)
        self.assertIsNone(campaign.payment_verified_at)

        secret = "promotion-webhook-signing-secret"
        payload = {
            "event": "charge.completed",
            "data": {"id": "950001", "tx_ref": payment.reference, "status": "successful"},
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = base64.b64encode(hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()).decode("ascii")
        with (
            override_settings(FLW_SECRET_HASH=secret),
            patch("promotions.tasks.verify_promotion_payment.delay") as verify_webhook,
            self.captureOnCommitCallbacks(execute=True),
        ):
            webhook = self.client.post(
                "/api/v1/payments/flutterwave/webhook/",
                data=body,
                content_type="application/json",
                HTTP_FLUTTERWAVE_SIGNATURE=signature,
            )
        self.assertEqual(webhook.status_code, 200)
        event = PaymentWebhookEvent.objects.get(provider="flutterwave", event_key="charge.completed:950001")
        verify_webhook.assert_called_once_with(str(payment.id), webhook_event_id=event.id)
        payment.refresh_from_db()
        campaign.refresh_from_db()
        event.refresh_from_db()
        self.assertEqual(payment.status, PromotionPayment.Status.INITIALIZATION_PENDING)
        self.assertEqual(campaign.status, PromotionCampaign.Status.PENDING_PAYMENT)
        self.assertIsNone(campaign.payment_verified_at)
        self.assertIsNone(event.processed_at)

        invalid_signature = self.client.post(
            "/api/v1/payments/flutterwave/webhook/",
            data=body,
            content_type="application/json",
            HTTP_FLUTTERWAVE_SIGNATURE="invalid-signature",
        )
        self.assertEqual(invalid_signature.status_code, 401)
        self.assertEqual(PaymentWebhookEvent.objects.count(), 1)

    def test_anonymous_homepage_serves_only_active_paid_promotions_with_separate_capacity(self):
        from promotions.campaign_services import recompute_inventory_schedule

        self.configure_promotion_plan(
            "HOMEPAGE",
            enabled=True,
            price_kobo=125000,
            terms_text="Homepage paid placement terms stored in the configured plan.",
        )
        application = self.approve_promotion_application(promotion_type="HOMEPAGE", target=None)
        offer, checkout = self.start_promotion_checkout(application, idempotency_key="promotion-public-homepage")
        self.assertEqual(offer.data["availability"], "AVAILABLE")
        self.assertEqual(checkout.status_code, 202, checkout.data)
        payment = PromotionPayment.objects.get(pk=checkout.data["payment_id"])
        self.verify_promotion_payment(payment, "960001")
        campaign = PromotionCampaign.objects.select_related("inventory").get(pk=checkout.data["campaign_id"])
        campaign.inventory.capacity = 1
        campaign.inventory.save(update_fields=["capacity", "updated_at"])
        recompute_inventory_schedule(campaign.inventory_id)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, PromotionCampaign.Status.ACTIVE)

        second_user, second_artisan = self.make_artisan(
            "promotion-second-homepage-maker@example.test",
            business_name="Second Homepage Artisan",
            location=self.ikeja,
            phone_number="+2348012345683",
        )
        second_offer = ArtisanService.objects.create(
            artisan=second_artisan,
            service=self.service,
            location=self.ikeja,
            starting_price="15500.00",
        )
        second_application = self.approve_promotion_application(
            promotion_type="HOMEPAGE",
            artisan_user=second_user,
            target=None,
            service_ids=[second_offer.pk],
        )
        _, second_checkout = self.start_promotion_checkout(second_application, idempotency_key="promotion-public-homepage-second")
        self.assertEqual(second_checkout.status_code, 202, second_checkout.data)
        second_payment = PromotionPayment.objects.get(pk=second_checkout.data["payment_id"])
        self.verify_promotion_payment(second_payment, "960002")
        recompute_inventory_schedule(campaign.inventory_id)
        second_campaign = PromotionCampaign.objects.get(pk=second_checkout.data["campaign_id"])
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, PromotionCampaign.Status.ACTIVE)
        self.assertEqual(second_campaign.status, PromotionCampaign.Status.SCHEDULED)

        anonymous_client = APIClient()
        public_response = anonymous_client.get("/api/v1/promotions/homepage/")
        self.assertEqual(public_response.status_code, 200, public_response.data)
        self.assertEqual(len(public_response.data), 1)
        self.assertEqual(public_response.data[0]["campaign_id"], str(campaign.id))
        self.assertEqual(public_response.data[0]["business_name"], self.artisan.business_name)
        self.assertIn("artisan_url", public_response.data[0])
        campaign.refresh_from_db()
        # A response containing a card is not evidence that a visitor saw it.
        self.assertEqual(campaign.impressions, 0)

        self.assertIsNotNone(campaign.payment_verified_at)
        self.assertTrue(campaign.is_publicly_active)

    def test_manager_plan_and_capacity_changes_are_authorized_audited_and_idempotent(self):
        from home.models import CustomUser

        plan = self.configure_promotion_plan()
        inventory = PromotionInventory.objects.create(
            promotion_type="AREA",
            target_key=f"AREA:{self.ikeja.pk}",
            area_location=self.ikeja,
            target_label=str(self.ikeja),
            capacity=20,
        )
        manager = CustomUser.objects.create_user(
            email="promotion-platform-manager@example.test",
            first_name="Platform",
            last_name="Manager",
            password="StrongPassword123!",
            is_active=True,
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_authenticate(manager)

        plan_list = self.client.get("/api/v1/promotions/management/plans/")
        self.assertEqual(plan_list.status_code, 200, plan_list.data)
        self.assertIn(str(plan.id), {item["id"] for item in plan_list.data})
        plan_detail = self.client.get(f"/api/v1/promotions/management/plans/{plan.id}/")
        self.assertEqual(plan_detail.status_code, 200, plan_detail.data)
        self.assertEqual(plan_detail.data["id"], str(plan.id))

        plan_update = {
            "name": "Manager-set Area Plan",
            "price": "3250.00",
            "duration_days": 11,
            "is_enabled": True,
            "refund_policy": "PRO_RATA",
            "terms_text": "Manager-set terms; payment verification and paid queue order apply.",
        }
        changed_plan = self.client.patch(
            f"/api/v1/promotions/management/plans/{plan.id}/",
            plan_update,
            format="json",
            HTTP_IDEMPOTENCY_KEY="promotion-manager-plan-change",
        )
        self.assertEqual(changed_plan.status_code, 200, changed_plan.data)
        self.assertIn("no-store", changed_plan["Cache-Control"])
        self.assertEqual(changed_plan.data["price"], "3250")
        self.assertEqual(changed_plan.data["duration_days"], 11)
        self.assertEqual(changed_plan.data["version"], 2)
        replay_plan = self.client.patch(
            f"/api/v1/promotions/management/plans/{plan.id}/",
            plan_update,
            format="json",
            HTTP_IDEMPOTENCY_KEY="promotion-manager-plan-change",
        )
        self.assertEqual(replay_plan.status_code, 200)
        self.assertEqual(replay_plan["Idempotent-Replayed"], "true")
        self.assertEqual(PromotionPlan.objects.get(pk=plan.id).version, 2)

        changed_capacity = self.client.patch(
            f"/api/v1/promotions/management/inventories/{inventory.id}/capacity/",
            {"capacity": 5},
            format="json",
            HTTP_IDEMPOTENCY_KEY="promotion-manager-capacity-change",
        )
        self.assertEqual(changed_capacity.status_code, 200, changed_capacity.data)
        self.assertEqual(changed_capacity.data["capacity"], 5)
        replay_capacity = self.client.patch(
            f"/api/v1/promotions/management/inventories/{inventory.id}/capacity/",
            {"capacity": 5},
            format="json",
            HTTP_IDEMPOTENCY_KEY="promotion-manager-capacity-change",
        )
        self.assertEqual(replay_capacity.status_code, 200)
        self.assertEqual(replay_capacity["Idempotent-Replayed"], "true")
        inventory.refresh_from_db()
        self.assertEqual(inventory.capacity, 5)

        self.client.force_authenticate(self.artisan_user)
        denied = self.client.patch(
            f"/api/v1/promotions/management/plans/{plan.id}/",
            {"price": "1000.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="promotion-artisan-plan-change",
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(PromotionPlan.objects.get(pk=plan.id).price_kobo, 325000)

    def test_promotion_reports_use_existing_support_case_flow_with_owner_and_public_visibility_checks(self):
        from promotions.campaign_services import recompute_inventory_schedule

        self.configure_promotion_plan(
            "HOMEPAGE",
            enabled=True,
            price_kobo=125000,
            terms_text="Homepage placement support and reporting terms for the current campaign.",
        )
        application = self.approve_promotion_application(promotion_type="HOMEPAGE", target=None)
        _, checkout = self.start_promotion_checkout(application, idempotency_key="promotion-support-case-campaign")
        self.assertEqual(checkout.status_code, 202, checkout.data)
        payment = PromotionPayment.objects.get(pk=checkout.data["payment_id"])
        self.verify_promotion_payment(payment, "970001")
        campaign = PromotionCampaign.objects.get(pk=checkout.data["campaign_id"])
        recompute_inventory_schedule(campaign.inventory_id)
        campaign.refresh_from_db()
        self.assertTrue(campaign.is_publicly_active)

        case_payload = {
            "promotion_campaign": str(campaign.id),
            "case_type": "REPORT",
            "priority": "MEDIUM",
            "subject": "Please review this sponsored placement",
            "description": "I am asking customer support to review this paid placement and the relevant evidence.",
        }
        with patch("support.services.notify_support_case_created"):
            self.client.force_authenticate(self.artisan_user)
            owner_case = self.client.post("/api/v1/support/cases/", case_payload, format="json")
        self.assertEqual(owner_case.status_code, 201, owner_case.data)
        self.assertEqual(str(owner_case.data["promotion_campaign"]), str(campaign.id))

        public_user, _public_artisan = self.make_artisan(
            "promotion-public-reporter@example.test",
            business_name="Public Reporter Artisan",
            location=self.ikeja,
            phone_number="+2348012345682",
        )
        with patch("support.services.notify_support_case_created"):
            self.client.force_authenticate(public_user)
            public_case = self.client.post("/api/v1/support/cases/", case_payload, format="json")
        self.assertEqual(public_case.status_code, 201, public_case.data)
        self.assertEqual(str(public_case.data["promotion_campaign"]), str(campaign.id))

    def test_campaign_management_action_endpoint_replays_without_duplicate_pause(self):
        self.configure_promotion_plan()
        application = self.approve_promotion_application(promotion_type="AREA", target=self.ikeja)
        _, checkout = self.start_promotion_checkout(application, idempotency_key="promotion-action-endpoint-checkout")
        self.assertEqual(checkout.status_code, 202, checkout.data)
        payment = PromotionPayment.objects.get(pk=checkout.data["payment_id"])
        self.verify_promotion_payment(payment, "980001")
        campaign = PromotionCampaign.objects.get(pk=checkout.data["campaign_id"])
        from promotions.campaign_services import recompute_inventory_schedule
        recompute_inventory_schedule(campaign.inventory_id)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, PromotionCampaign.Status.ACTIVE)

        case = SupportCase.objects.create(
            opened_by=self.artisan_user,
            promotion_campaign=campaign,
            case_type=SupportCase.CaseType.REPORT,
            subject="Review paid campaign",
            description="Support review case for the campaign action endpoint.",
        )
        agent = self.support_user(email="promotion-action-endpoint-agent@example.test")
        action_payload = {
            "action": "SUSPEND",
            "support_case_id": case.pk,
            "reason": "Suspend temporarily while support reviews the submitted evidence.",
            "evidence_summary": "Review is ongoing. This report is not a confirmed violation.",
            "confirmed_violation": False,
        }
        self.client.force_authenticate(agent)
        with patch("promotions.campaign_services.notify_promotion_campaign_update"):
            first = self.client.post(
                f"/api/v1/promotions/review/campaigns/{campaign.id}/actions/",
                action_payload,
                format="json",
                HTTP_IDEMPOTENCY_KEY="promotion-campaign-suspend-action",
            )
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["status"], PromotionCampaign.Status.SUSPENDED)

        replay = self.client.post(
            f"/api/v1/promotions/review/campaigns/{campaign.id}/actions/",
            action_payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="promotion-campaign-suspend-action",
        )
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay["Idempotent-Replayed"], "true")
        self.assertEqual(
            PromotionCampaignAction.objects.filter(
                campaign=campaign,
                action=PromotionCampaignAction.ActionType.SUSPENDED,
            ).count(),
            1,
        )

    def test_platform_termination_creates_separately_approved_promotion_refund_request(self):
        from promotions.campaign_services import pause_or_restore_or_revoke_campaign, recompute_inventory_schedule

        self.configure_promotion_plan(
            duration_days=7,
            terms_text="Pro-rata refund for unused paid time after platform termination, with independent approval.",
        )
        application = self.approve_promotion_application(promotion_type="AREA", target=self.ikeja)
        _, checkout = self.start_promotion_checkout(application, idempotency_key="promotion-refund-checkout")
        self.assertEqual(checkout.status_code, 202, checkout.data)
        payment = PromotionPayment.objects.get(pk=checkout.data["payment_id"])
        self.verify_promotion_payment(payment, "990001")
        campaign = PromotionCampaign.objects.get(pk=checkout.data["campaign_id"])
        recompute_inventory_schedule(campaign.inventory_id)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, PromotionCampaign.Status.ACTIVE)

        complaint_case = SupportCase.objects.create(
            opened_by=self.artisan_user,
            promotion_campaign=campaign,
            case_type=SupportCase.CaseType.REPORT,
            subject="Review campaign conduct",
            description="A report to investigate before deciding whether the campaign should continue.",
        )
        supervisor = self.support_user(role=SUPPORT_SUPERVISOR, email="promotion-refund-supervisor@example.test")
        with (
            patch("promotions.campaign_services.notify_promotion_campaign_update"),
            patch("support.services.notify_support_case_created"),
            patch("support.services.notify_support_refund_requested"),
        ):
            revoked, action, refund_action = pause_or_restore_or_revoke_campaign(
                campaign.id,
                actor=supervisor,
                action="REVOKE",
                reason="End this placement under the configured pro-rata termination terms.",
                evidence_summary="Review completed; this refund follows platform termination terms.",
                support_case_id=complaint_case.id,
                confirmed_violation=False,
            )

        self.assertEqual(revoked.status, PromotionCampaign.Status.REVOKED)
        self.assertEqual(action.action, PromotionCampaignAction.ActionType.REVOKED)
        self.assertGreater(revoked.remaining_duration_seconds, 0)
        self.assertIsNotNone(refund_action)
        self.assertEqual(refund_action.status, "REQUESTED")
        self.assertEqual(refund_action.promotion_payment_id, payment.id)
        self.assertIsNone(refund_action.approved_by_id)
        self.assertGreater(refund_action.amount_kobo, 0)
        self.assertEqual(refund_action.case.promotion_campaign_id, campaign.id)
        self.assertEqual(refund_action.case.case_type, SupportCase.CaseType.PAYMENT)
        self.assertEqual(PromotionCampaign.objects.filter(pk=campaign.id).count(), 1)
        self.assertEqual(SupportCase.objects.filter(promotion_campaign=campaign).count(), 2)

        from support.services import approve_refund, execute_refund, reconcile_refund

        approver = self.support_user(role=SUPPORT_SUPERVISOR, email="promotion-refund-approver@example.test")
        finance_user = self.support_user(role=SUPPORT_FINANCE, email="promotion-refund-finance@example.test")
        refund_action = approve_refund(
            action=refund_action,
            staff_user=approver,
            approval_note="Approved under the configured unused-time refund terms.",
        )
        self.assertEqual(refund_action.status, "APPROVED")
        self.assertEqual(refund_action.approved_by_id, approver.id)

        with patch(
            "support.services.FlutterwaveClient.refund_transaction",
            return_value={"status": "success", "data": {"id": "provider-refund-990001", "flw_ref": "FLW-RFD-990001", "status": "pending"}},
        ) as submit_refund:
            refund_action = execute_refund(action=refund_action, staff_user=finance_user)
        submit_refund.assert_called_once_with(
            transaction_id="990001",
            amount_kobo=refund_action.amount_kobo,
            comments=refund_action.reason,
            callback_url="",
        )
        self.assertEqual(refund_action.status, "PROCESSING")
        payment.refresh_from_db()
        self.assertEqual(payment.status, PromotionPayment.Status.REFUND_PENDING)

        successful_provider_result = {
            "status": "success",
            "data": [{
                "id": "provider-refund-990001",
                "flw_ref": "FLW-RFD-990001",
                "status": "successful",
                "tx_id": "990001",
                "amount_refunded": refund_action.amount_kobo // 100,
            }],
        }
        with (
            patch("support.services.FlutterwaveClient.find_refund", return_value=successful_provider_result),
            patch("support.services.notify_support_refund_succeeded"),
        ):
            refund_action = reconcile_refund(action=refund_action)

        self.assertEqual(refund_action.status, "SUCCEEDED")
        payment.refresh_from_db()
        revoked.refresh_from_db()
        refund_action.case.refresh_from_db()
        self.assertEqual(payment.status, PromotionPayment.Status.PARTIALLY_REFUNDED)
        self.assertEqual(refund_action.case.resolution_code, SupportCase.ResolutionCode.PARTIAL_REFUND)
        self.assertEqual(revoked.status, PromotionCampaign.Status.REVOKED)

    def _promotion_active_campaign(self, promotion_type="AREA", *, target=None, reference_id="700001", artisan_user=None, service_ids=None):
        from promotions.campaign_services import recompute_inventory_schedule

        self.configure_promotion_plan(
            promotion_type,
            enabled=True,
            price_kobo=225000,
            duration_days=9,
            terms_text="Configured paid placement terms with verified payment and active service eligibility.",
        )
        application = self.approve_promotion_application(
            promotion_type=promotion_type,
            artisan_user=artisan_user,
            target=target,
            service_ids=service_ids,
        )
        _offer, checkout = self.start_promotion_checkout(
            application,
            idempotency_key=f"promotion-checkout-{reference_id}",
        )
        self.assertEqual(checkout.status_code, 202, checkout.data)
        payment = PromotionPayment.objects.get(pk=checkout.data["payment_id"])
        self.verify_promotion_payment(payment, reference_id)
        campaign = PromotionCampaign.objects.select_related("inventory").get(pk=checkout.data["campaign_id"])
        campaign.inventory.capacity = max(1, campaign.inventory.capacity)
        campaign.inventory.save(update_fields=["capacity", "updated_at"])
        recompute_inventory_schedule(campaign.inventory_id)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, PromotionCampaign.Status.ACTIVE)
        return campaign

    def test_promotion_impressions_and_profile_visits_require_real_placement_events(self):
        from promotions.models import PromotionCampaignPerformanceDaily, PromotionCampaignPerformanceEvent

        campaign = self._promotion_active_campaign("HOMEPAGE", reference_id="970001")
        anonymous = APIClient()
        homepage = anonymous.get("/api/v1/promotions/homepage/")
        self.assertEqual(homepage.status_code, 200, homepage.data)
        self.assertEqual(homepage.data[0]["campaign_id"], str(campaign.pk))
        self.assertIn("impression_token", homepage.data[0])
        campaign.refresh_from_db()
        self.assertEqual(campaign.impressions, 0, "An API response is not an impression.")
        self.assertEqual(PromotionCampaignPerformanceEvent.objects.filter(campaign=campaign).count(), 0)

        visitor_id = "visitor-session-promotion-000000000001"
        impression_payload = {
            "token": homepage.data[0]["impression_token"],
            "visitor_id": visitor_id,
            "event_type": PromotionCampaignPerformanceEvent.EventType.IMPRESSION,
            "artisan_id": str(campaign.artisan_id),
        }
        shown = anonymous.post("/api/v1/promotions/performance-events/", impression_payload, format="json")
        self.assertEqual(shown.status_code, 201, shown.data)
        self.assertTrue(shown.data["recorded"])
        repeated = anonymous.post("/api/v1/promotions/performance-events/", impression_payload, format="json")
        self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertFalse(repeated.data["recorded"])

        wrong_event = anonymous.post(
            "/api/v1/promotions/performance-events/",
            {**impression_payload, "event_type": PromotionCampaignPerformanceEvent.EventType.PROFILE_VISIT},
            format="json",
        )
        self.assertEqual(wrong_event.status_code, 400, wrong_event.data)
        profile_payload = {
            "token": homepage.data[0]["profile_visit_token"],
            "visitor_id": visitor_id,
            "event_type": PromotionCampaignPerformanceEvent.EventType.PROFILE_VISIT,
            "artisan_id": str(campaign.artisan_id),
        }
        visited = anonymous.post("/api/v1/promotions/performance-events/", profile_payload, format="json")
        self.assertEqual(visited.status_code, 201, visited.data)
        self.assertTrue(visited.data["recorded"])
        campaign.refresh_from_db()
        self.assertEqual(campaign.impressions, 0, "Promotion performance events must not contaminate legacy impression counters.")
        self.assertEqual(campaign.profile_clicks, 0)
        self.assertEqual(campaign.performance_impressions, 1)
        self.assertEqual(campaign.performance_profile_visits, 1)
        self.assertEqual(PromotionCampaignPerformanceEvent.objects.filter(campaign=campaign).count(), 2)
        daily = PromotionCampaignPerformanceDaily.objects.get(campaign=campaign, event_day=timezone.localdate())
        self.assertEqual(daily.impressions, 1)
        self.assertEqual(daily.profile_visits, 1)

        self.client.force_authenticate(self.artisan_user)
        owner_response = self.client.get("/api/v1/promotions/campaigns/")
        self.assertEqual(owner_response.status_code, 200, owner_response.data)
        owner_campaign = next(row for row in owner_response.data if row["id"] == str(campaign.pk))
        self.assertTrue(owner_campaign["performance_data_available"])
        self.assertEqual(owner_campaign["impressions"], 1)
        self.assertEqual(owner_campaign["profile_visits"], 1)
        self.assertEqual(owner_campaign["profile_visit_rate_percent"], 100.0)

        other_user, _other_artisan = self.make_artisan(
            "promotion-private-metrics-other@example.test",
            business_name="Another Private Artisan",
            location=self.ikeja,
            phone_number="+2348012345689",
        )
        self.client.force_authenticate(other_user)
        outsider_response = self.client.get("/api/v1/promotions/campaigns/")
        self.assertEqual(outsider_response.status_code, 200, outsider_response.data)
        self.assertFalse(any(row["id"] == str(campaign.pk) for row in outsider_response.data))

        from django.urls import reverse
        manager = get_user_model().objects.create_superuser(
            email="promotion-manager-metrics@example.test",
            first_name="Test",
            last_name="Manager",
            password="StrongPassword123!",
        )
        self.client.force_authenticate(manager)
        overview = self.client.get(reverse("manager-overview"))
        self.assertEqual(overview.status_code, 200, overview.data)
        promotion_summary = overview.data["promotions"]
        self.assertEqual(promotion_summary["active_homepage_placements"], 1)
        self.assertEqual(promotion_summary["performance"]["last_30_days"]["impressions"], 1)
        self.assertEqual(promotion_summary["performance"]["last_30_days"]["profile_visits"], 1)
        self.assertEqual(promotion_summary["performance"]["last_30_days"]["useful_rate_percent"], 100.0)

    def test_promotion_homepage_capacity_is_separate_from_public_card_display_limit(self):
        first_campaign = self._promotion_active_campaign(
            "HOMEPAGE",
            reference_id="970006",
        )
        second_user, second_artisan = self.make_artisan(
            "promotion-homepage-capacity-second@example.test",
            business_name="Second Homepage Works",
            location=self.ikeja,
            phone_number="+2348012345691",
        )
        second_offer = ArtisanService.objects.create(
            artisan=second_artisan,
            service=self.service,
            location=self.ikeja,
            starting_price="15500.00",
        )
        second_campaign = self._promotion_active_campaign(
            "HOMEPAGE",
            reference_id="970007",
            artisan_user=second_user,
            service_ids=[second_offer.pk],
        )
        inventory = second_campaign.inventory
        first_campaign.refresh_from_db()
        second_campaign.refresh_from_db()
        self.assertEqual(first_campaign.status, PromotionCampaign.Status.ACTIVE)
        self.assertEqual(second_campaign.status, PromotionCampaign.Status.ACTIVE)
        self.assertGreaterEqual(inventory.capacity, 2)

        with override_settings(PROMOTION_HOMEPAGE_DISPLAY_LIMIT=1):
            response = APIClient().get("/api/v1/promotions/homepage/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertIn(response.data[0]["campaign_id"], {str(first_campaign.pk), str(second_campaign.pk)})

    def test_promotion_legacy_counters_are_not_reported_as_event_performance(self):
        campaign = self._promotion_active_campaign("AREA", target=self.ikeja, reference_id="970010")
        campaign.impressions = 57
        campaign.profile_clicks = 13
        campaign.save(update_fields=["impressions", "profile_clicks", "updated_at"])
        self.client.force_authenticate(self.artisan_user)

        response = self.client.get("/api/v1/promotions/campaigns/")
        self.assertEqual(response.status_code, 200, response.data)
        serialized = next(row for row in response.data if row["id"] == str(campaign.pk))
        self.assertIsNone(serialized["impressions"])
        self.assertIsNone(serialized["profile_visits"])
        self.assertIsNone(serialized["profile_clicks"])
        self.assertFalse(serialized["performance_data_available"])
        self.assertIsNone(serialized["profile_visit_rate_percent"])

    def test_promotion_geographic_filters_are_canonical_and_specific_targets_win(self):
        anonymous = APIClient()
        filters = {
            "category_name": "Home Repair",
            "service_name": "Pipe Repair",
            "country": "Nigeria",
            "state": "Lagos",
            "area": "Ikeja",
        }
        state_campaign = self._promotion_active_campaign("STATE", reference_id="970011")
        state_fallback = anonymous.get("/api/v1/promotions/discovery/", filters)
        self.assertEqual(state_fallback.status_code, 200, state_fallback.data)
        self.assertEqual(len(state_fallback.data["results"]), 1)
        self.assertEqual(state_fallback.data["results"][0]["campaign_id"], str(state_campaign.pk))
        self.assertEqual(state_fallback.data["results"][0]["placement_type"], "STATE_PROMOTION")
        self.assertIn("impression_token", state_fallback.data["results"][0])
        state_campaign.refresh_from_db()
        self.assertEqual(state_campaign.impressions, 0, "A discovery API response is not an impression.")

        # The same locality string in another state is a different canonical target.
        other_state_ikeja = Locations.objects.create(country="Nigeria", state="Ogun", area="Ikeja")
        wrong_state = anonymous.get(
            "/api/v1/promotions/discovery/",
            {**filters, "state": "Ogun", "area_location_id": str(other_state_ikeja.pk)},
        )
        self.assertEqual(wrong_state.status_code, 200, wrong_state.data)
        self.assertEqual(wrong_state.data["results"], [])
        country_only = anonymous.get(
            "/api/v1/promotions/discovery/",
            {"category_name": "Home Repair", "country": "Nigeria"},
        )
        self.assertEqual(country_only.data["results"], [])

        area_campaign = self._promotion_active_campaign("AREA", target=self.ikeja, reference_id="970012")
        area_result = anonymous.get("/api/v1/promotions/discovery/", filters)
        self.assertEqual(len(area_result.data["results"]), 1)
        self.assertEqual(area_result.data["results"][0]["campaign_id"], str(area_campaign.pk))
        self.assertEqual(area_result.data["results"][0]["placement_type"], "AREA_PROMOTION")

        institution_campaign = self._promotion_active_campaign(
            "INSTITUTION",
            target=self.institution,
            reference_id="970013",
        )
        institution_result = anonymous.get(
            "/api/v1/promotions/discovery/",
            {
                "category_name": "Home Repair",
                "service_name": "Pipe Repair",
                "institution_id": str(self.institution.pk),
            },
        )
        self.assertEqual(institution_result.status_code, 200, institution_result.data)
        self.assertEqual(len(institution_result.data["results"]), 1, "One artisan must not appear twice in a promoted section.")
        self.assertEqual(institution_result.data["results"][0]["campaign_id"], str(institution_campaign.pk))
        self.assertEqual(institution_result.data["results"][0]["placement_type"], "INSTITUTION_PROMOTION")

        organic = anonymous.get(
            "/api/v1/profile/artisan/details/",
            {"category_name": "Home Repair", "service_name": "Pipe Repair", "country": "Nigeria", "state": "Lagos", "area": "Ikeja"},
        )
        self.assertEqual(organic.status_code, 200, organic.data)
        self.assertFalse(organic.data["results"][0]["is_promoted"])

    def test_promotion_discovery_rotation_prefers_less_exposed_eligible_inventory(self):

        first_campaign = self._promotion_active_campaign(
            "AREA",
            target=self.ikeja,
            reference_id="970016",
        )
        second_user, second_artisan = self.make_artisan(
            "promotion-rotation-second@example.test",
            business_name="Another Pipe Works",
            location=self.ikeja,
            phone_number="+2348012345690",
        )
        second_offer = ArtisanService.objects.create(
            artisan=second_artisan,
            service=self.service,
            location=self.ikeja,
            starting_price="14500.00",
        )
        second_campaign = self._promotion_active_campaign(
            "AREA",
            target=self.ikeja,
            reference_id="970017",
            artisan_user=second_user,
            service_ids=[second_offer.pk],
        )
        from promotions.placement_services import make_performance_token
        impression_token = make_performance_token(
            first_campaign,
            event_type="IMPRESSION",
        )
        for index in range(2):
            tracked = APIClient().post(
                "/api/v1/promotions/performance-events/",
                {
                    "token": impression_token,
                    "visitor_id": f"rotation-visitor-session-00000000000{index + 1}",
                    "event_type": "IMPRESSION",
                    "artisan_id": str(first_campaign.artisan_id),
                },
                format="json",
            )
            self.assertEqual(tracked.status_code, 201, tracked.data)
        first_campaign.refresh_from_db()
        self.assertEqual(first_campaign.performance_impressions, 2)

        with override_settings(PROMOTION_DISCOVERY_DISPLAY_LIMIT=1):
            response = APIClient().get(
                "/api/v1/promotions/discovery/",
                {"category_name": "Home Repair", "service_name": "Pipe Repair", "country": "Nigeria", "state": "Lagos", "area": "Ikeja"},
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["display_limit"], 1)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["campaign_id"], str(second_campaign.pk))
        self.assertNotEqual(response.data["results"][0]["campaign_id"], str(first_campaign.pk))

    def test_promotion_market_picks_are_editorial_and_management_is_superuser_only(self):
        from core.models import AuditLog
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from promotions.models import ArtisanMarketPick

        anonymous = APIClient()
        self.assertEqual(anonymous.get("/api/v1/promotions/market-picks/").data, [])
        ordinary_manager = self.support_user(email="promotion-not-a-superuser@example.test")
        self.client.force_authenticate(ordinary_manager)
        self.assertEqual(self.client.get("/api/v1/promotions/management/market-picks/").status_code, 403)
        denied = self.client.post(
            "/api/v1/promotions/management/market-picks/",
            {"action": "ADD", "artisan_id": str(self.artisan.pk), "position": 1, "selection_note": "Complete profile and verified active service work."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="promotion-pick-denied",
        )
        self.assertEqual(denied.status_code, 403)

        manager = get_user_model().objects.create_user(
            email="promotion-editorial-manager@example.test",
            first_name="Platform",
            last_name="Editor",
            password="StrongPassword123!",
            is_active=True,
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_authenticate(manager)
        candidates = self.client.get("/api/v1/promotions/management/market-picks/")
        self.assertEqual(candidates.status_code, 200, candidates.data)
        self.assertIn(str(self.artisan.pk), {row["artisan_id"] for row in candidates.data["eligible_artisans"]})
        payload = {
            "action": "ADD",
            "artisan_id": str(self.artisan.pk),
            "position": 1,
            "selection_note": "Verified complete profile with an active service offering and clear work evidence.",
        }
        created = self.client.post(
            "/api/v1/promotions/management/market-picks/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="promotion-market-pick-add",
        )
        self.assertEqual(created.status_code, 200, created.data)
        self.assertTrue(created.data["is_active"])
        self.assertTrue(created.data["currently_eligible"])
        self.assertEqual(ArtisanMarketPick.objects.filter(artisan=self.artisan, is_active=True).count(), 1)
        add_audit = AuditLog.objects.get(
            actor=manager,
            action="ARTISAN_MARKET_PICK_ADD",
            resource_type="ArtisanMarketPick",
            resource_id=created.data["id"],
        )
        self.assertEqual(add_audit.metadata["selection_note"], payload["selection_note"])
        replay = self.client.post(
            "/api/v1/promotions/management/market-picks/",
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="promotion-market-pick-add",
        )
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay["Idempotent-Replayed"], "true")
        self.assertEqual(AuditLog.objects.filter(resource_id=created.data["id"], resource_type="ArtisanMarketPick").count(), 1)

        cache.clear()
        public = anonymous.get("/api/v1/promotions/market-picks/")
        self.assertEqual(public.status_code, 200, public.data)
        self.assertEqual(len(public.data), 1)
        self.assertEqual(public.data[0]["placement_type"], "ARTISAN_MARKET_PICK")
        self.assertEqual(public.data[0]["artisan_id"], str(self.artisan.pk))
        self.assertNotIn("selection_note", public.data[0])

        removal_note = "Pause this editorial selection while the team reevaluates its current portfolio evidence."
        removed = self.client.post(
            "/api/v1/promotions/management/market-picks/",
            {"action": "REMOVE", "artisan_id": str(self.artisan.pk), "position": 1, "selection_note": removal_note},
            format="json",
            HTTP_IDEMPOTENCY_KEY="promotion-market-pick-remove",
        )
        self.assertEqual(removed.status_code, 200, removed.data)
        self.assertFalse(removed.data["is_active"])
        remove_audit = AuditLog.objects.get(
            actor=manager,
            action="ARTISAN_MARKET_PICK_REMOVE",
            resource_type="ArtisanMarketPick",
            resource_id=created.data["id"],
        )
        self.assertEqual(remove_audit.metadata["selection_note"], removal_note)
        self.assertEqual(remove_audit.metadata["previous_selection_note"], payload["selection_note"])
        cache.clear()
        self.assertEqual(anonymous.get("/api/v1/promotions/market-picks/").data, [])

        inactive_service = self.service
        inactive_service.is_active = False
        inactive_service.save(update_fields=["is_active"])
        cache.clear()
        hidden = anonymous.get("/api/v1/promotions/market-picks/")
        self.assertEqual(hidden.data, [])

    def test_promotion_performance_dedupe_cleanup_keeps_aggregate_counters(self):
        from datetime import timedelta
        from promotions.models import PromotionCampaignPerformanceDaily, PromotionCampaignPerformanceEvent
        from promotions.placement_services import delete_expired_performance_dedupe_rows

        campaign = self._promotion_active_campaign("HOMEPAGE", reference_id="970021")
        stale = PromotionCampaignPerformanceEvent.objects.create(
            campaign=campaign,
            event_type=PromotionCampaignPerformanceEvent.EventType.IMPRESSION,
            visitor_digest="a" * 64,
            event_day=timezone.localdate() - timedelta(days=10),
        )
        daily_aggregate = PromotionCampaignPerformanceDaily.objects.create(
            campaign=campaign,
            event_day=stale.event_day,
            impressions=4,
            profile_visits=2,
        )
        retained = PromotionCampaignPerformanceEvent.objects.create(
            campaign=campaign,
            event_type=PromotionCampaignPerformanceEvent.EventType.PROFILE_VISIT,
            visitor_digest="b" * 64,
            event_day=timezone.localdate(),
        )
        campaign.performance_impressions = 8
        campaign.save(update_fields=["performance_impressions", "updated_at"])
        with override_settings(PROMOTION_PERFORMANCE_DEDUPE_DAYS=2):
            deleted = delete_expired_performance_dedupe_rows()
        self.assertEqual(deleted, 1)
        self.assertFalse(PromotionCampaignPerformanceEvent.objects.filter(pk=stale.pk).exists())
        self.assertTrue(PromotionCampaignPerformanceEvent.objects.filter(pk=retained.pk).exists())
        self.assertTrue(PromotionCampaignPerformanceDaily.objects.filter(pk=daily_aggregate.pk).exists())
        campaign.refresh_from_db()
        self.assertEqual(campaign.performance_impressions, 8)

    def test_support_agent_cannot_restore_or_revoke_campaign(self):
        self.configure_promotion_plan(
            duration_days=7,
            terms_text="Paid campaign terms specify a verified payment and an audited review flow.",
        )
        application = self.approve_promotion_application(promotion_type="AREA", target=self.ikeja)
        _offer, checkout = self.start_promotion_checkout(application, idempotency_key="promotion-agent-action-checkout")
        self.assertEqual(checkout.status_code, 202, checkout.data)
        campaign = PromotionCampaign.objects.get(pk=checkout.data["campaign_id"])
        agent = self.support_user(role=SUPPORT_AGENT, email="promotion-ordinary-agent@example.test")
        self.client.force_authenticate(agent)

        for action in ("RESTORE", "REVOKE"):
            with self.subTest(action=action):
                response = self.client.post(
                    f"/api/v1/promotions/review/campaigns/{campaign.id}/actions/",
                    {
                        "action": action,
                        "support_case_id": 1,
                        "reason": "A reviewed campaign action with a recorded operational reason.",
                        "evidence_summary": "A support agent cannot use this supervisor-only lifecycle action.",
                    },
                    format="json",
                    HTTP_IDEMPOTENCY_KEY=f"promotion-agent-{action.lower()}-denied",
                )
                self.assertEqual(response.status_code, 403, response.data)

        self.assertFalse(PromotionCampaignAction.objects.filter(campaign=campaign).exists())

    def test_manager_inventory_active_counts_match_authoritative_public_eligibility(self):
        from home.manager_analytics import build_manager_overview

        first = self._promotion_active_campaign(
            "AREA",
            target=self.ikeja,
            reference_id="990001",
        )
        first.inventory.capacity = 2
        first.inventory.save(update_fields=["capacity", "updated_at"])

        second_user, second_artisan = self.make_artisan(
            "promotion-inventory-second@example.test",
            business_name="Second Ikeja Artisan",
            location=self.ikeja,
            phone_number="+2348012345680",
        )
        second_offer = ArtisanService.objects.create(
            artisan=second_artisan,
            service=self.service,
            location=self.ikeja,
            starting_price="17500.00",
        )
        second = self._promotion_active_campaign(
            "AREA",
            target=self.ikeja,
            reference_id="990002",
            artisan_user=second_user,
            service_ids=[second_offer.pk],
        )
        self.assertEqual(first.inventory_id, second.inventory_id)
        self.assertEqual(first.status, PromotionCampaign.Status.ACTIVE)
        self.assertEqual(second.status, PromotionCampaign.Status.ACTIVE)

        second_artisan.verification.verification_status = "PENDING"
        second_artisan.verification.save(update_fields=["verification_status", "updated_at"])
        self.assertEqual(
            PromotionCampaign.objects.filter(
                inventory_id=first.inventory_id,
                status=PromotionCampaign.Status.ACTIVE,
                payment_verified_at__isnull=False,
            ).count(),
            2,
        )

        overview = build_manager_overview(now=timezone.now())
        row = next(
            item for item in overview["promotions"]["inventory"]
            if item["id"] == str(first.inventory_id)
        )
        self.assertEqual(row["active_count"], 1)
        self.assertEqual(row["remaining_capacity"], 1)
        self.assertEqual(overview["promotions"]["active_geographic_campaigns"], 1)

        manager = self.user_model.objects.create_superuser(
            email="promotion-inventory-manager@example.test",
            first_name="Test",
            last_name="Manager",
            password="SafePass!483",
        )
        self.client.force_authenticate(manager)
        response = self.client.get("/api/v1/promotions/management/inventories/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("no-store", response["Cache-Control"])
        inventory_rows = response.data.get("results", response.data)
        api_row = next(item for item in inventory_rows if item["id"] == str(first.inventory_id))
        self.assertEqual(api_row["active_count"], 1)
        self.assertEqual(api_row["remaining_capacity"], 1)
