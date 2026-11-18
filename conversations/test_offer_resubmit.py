"""Re-offering after a customer rejection — the agreement stays open history.

Pins the release behaviour the chat relies on:

* a declined offer is terminal for that offer only — the request/conversation
  stays open and the artisan may send a brand-new offer;
* at most one pending (PROPOSED / REVISION_REQUESTED) offer exists at a time;
* the rejected offer is kept, immutable and untouched, with its own version;
* versions keep incrementing, and the new offer is a fresh row (no clobbering).
"""

from datetime import timedelta
from decimal import Decimal
import uuid

from django.test import TestCase
from django.utils import timezone

from rest_framework.exceptions import ValidationError

from conversations.models import Conversation, Offer
from conversations.services import (
    create_offer,
    decline_offer,
    get_current_offer,
)
from home.models import (
    ArtisanProfile,
    CustomerProfile,
    CustomUser,
    Locations,
    ServiceCategory,
)
from services.models import ServiceRequest


def _user(email, first="A"):
    return CustomUser.objects.create_user(
        email=email,
        first_name=first,
        last_name="Tester",
        password="StrongPassword123!",
        is_active=True,
    )


def _setup_service_conversation():
    customer_user = _user(f"customer-{uuid.uuid4().hex[:10]}@example.com", "Ada")
    artisan_user = _user(f"artisan-{uuid.uuid4().hex[:10]}@example.com", "Tunde")
    CustomerProfile.objects.create(user=customer_user)
    artisan = ArtisanProfile.objects.create(user=artisan_user, profile_status="ACTIVE")
    category = ServiceCategory.objects.create(name=f"Cat {uuid.uuid4().hex[:6]}")
    location = Locations.objects.create(country="Nigeria", state="Lagos", area="Yaba")
    service_request = ServiceRequest.objects.create(
        requester=customer_user,
        category=category,
        location=location,
        preferred_date=timezone.localdate(),
        description="Fix the tap.",
    )
    conversation = Conversation.objects.create(
        customer=customer_user,
        artisan=artisan,
        service_request=service_request,
    )
    return conversation, customer_user, artisan_user


def _offer_payload(**overrides):
    payload = {
        "labour_amount": "25000.00",
        "materials_amount": "0",
        "materials": [],
        "transport_amount": "0",
        "additional_amount": "0",
        "scheduled_start": timezone.now() + timedelta(days=3),
        "duration_value": 2,
        "duration_unit": "DAYS",
    }
    payload.update(overrides)
    return payload


class OfferResubmissionTests(TestCase):
    def setUp(self):
        self.conversation, self.customer_user, self.artisan_user = (
            _setup_service_conversation()
        )

    def test_artisan_can_reoffer_after_the_customer_declines(self):
        first = create_offer(
            conversation=self.conversation,
            user=self.artisan_user,
            payload=_offer_payload(),
            idempotency_key=f"offer-{uuid.uuid4().hex}",
        )
        declined = decline_offer(
            conversation=self.conversation,
            user=self.customer_user,
            offer_id=first.id,
        )
        self.assertEqual(declined.status, Offer.Status.DECLINED)
        # A rejected offer is not "the current offer" any more.
        self.assertIsNone(get_current_offer(self.conversation))
        second = create_offer(
            conversation=self.conversation,
            user=self.artisan_user,
            payload=_offer_payload(labour_amount="32000.00"),
            idempotency_key=f"offer-{uuid.uuid4().hex}",
        )
        self.assertGreater(second.version, first.version)
        self.assertEqual(second.status, Offer.Status.PROPOSED)
        self.assertNotEqual(second.id, first.id)
        # History is preserved: the rejected offer keeps its old version and price.
        first.refresh_from_db()
        self.assertEqual(first.status, Offer.Status.DECLINED)
        self.assertEqual(first.labour_amount, Decimal("25000.00"))
        self.assertEqual(
            Offer.objects.filter(conversation=self.conversation).count(), 2
        )

    def test_one_pending_offer_at_a_time_is_enforced(self):
        create_offer(
            conversation=self.conversation,
            user=self.artisan_user,
            payload=_offer_payload(),
            idempotency_key=f"offer-{uuid.uuid4().hex}",
        )
        with self.assertRaises(ValidationError):
            create_offer(
                conversation=self.conversation,
                user=self.artisan_user,
                payload=_offer_payload(labour_amount="40000.00"),
                idempotency_key=f"offer-{uuid.uuid4().hex}",
            )
        self.assertEqual(
            Offer.objects.filter(
                conversation=self.conversation, status=Offer.Status.PROPOSED
            ).count(),
            1,
        )

    def test_declined_offer_remains_immutable(self):
        first = create_offer(
            conversation=self.conversation,
            user=self.artisan_user,
            payload=_offer_payload(labour_amount="25000.00"),
            idempotency_key=f"offer-{uuid.uuid4().hex}",
        )
        decline_offer(
            conversation=self.conversation,
            user=self.customer_user,
            offer_id=first.id,
        )
        before = Offer.objects.get(pk=first.pk)
        snapshot = (
            before.status,
            before.labour_amount,
            before.materials_amount,
            before.total_amount,
            before.version,
            before.created_by_id,
        )
        create_offer(
            conversation=self.conversation,
            user=self.artisan_user,
            payload=_offer_payload(labour_amount="51000.00"),
            idempotency_key=f"offer-{uuid.uuid4().hex}",
        )
        after = Offer.objects.get(pk=first.pk)
        self.assertEqual(
            snapshot,
            (
                after.status,
                after.labour_amount,
                after.materials_amount,
                after.total_amount,
                after.version,
                after.created_by_id,
            ),
        )
