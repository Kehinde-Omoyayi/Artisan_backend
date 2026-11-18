"""Platform-wide ₦100.00 minimum individual price — backend enforcement.

Every actual individual charge an artisan enters or submits must be at least
₦100.00: initial quotations, itemized materials, labour, transport, add-on
services and revised quotations. Optional charges that are simply not applied
(zero) remain valid; historical records below the minimum remain readable;
payout/escrow logic is untouched.
"""

from datetime import timedelta
from decimal import Decimal
import uuid

from django.test import TestCase
from django.utils import timezone

from rest_framework.test import APIClient

from booking.models import Booking, BookingAdditionalOffer, BookingAdditionalOrder
from conversations.models import Conversation, Offer
from conversations.services import create_offer
from core.policies import ARTISAN_COMMISSION_RATE, CUSTOMER_FEE_KOBO
from core.pricing import MIN_INDIVIDUAL_PRICE_MESSAGE
from home.models import (
    ArtisanProfile,
    CustomerProfile,
    CustomUser,
    Locations,
    ServiceCategory,
)
from payments.calculations import calculate_booking_money
from payments.models import EscrowPayment
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
        "transport_amount": "0",
        "additional_amount": "0",
        "scheduled_start": (timezone.now() + timedelta(days=3)).isoformat(),
        "duration_value": 2,
        "duration_unit": "DAYS",
    }
    payload.update(overrides)
    return payload


class OfferMinPriceApiTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.conversation, self.customer_user, self.artisan_user = (
            _setup_service_conversation()
        )
        self.url = f"/api/v1/conversations/{self.conversation.id}/offers/"
        self.api.force_authenticate(self.artisan_user)

    def _post_offer(self, payload, key=None):
        return self.api.post(
            self.url,
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY=key or f"offer-{uuid.uuid4().hex}",
        )

    def test_price_below_minimum_is_rejected(self):
        response = self._post_offer(_offer_payload(labour_amount="50.00"))
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(MIN_INDIVIDUAL_PRICE_MESSAGE, str(response.data))
        self.assertEqual(Offer.objects.count(), 0)

    def test_boundary_99_99_is_rejected(self):
        response = self._post_offer(_offer_payload(labour_amount="99.99"))
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(MIN_INDIVIDUAL_PRICE_MESSAGE, str(response.data))
        self.assertEqual(Offer.objects.count(), 0)

    def test_exactly_100_is_accepted(self):
        response = self._post_offer(_offer_payload(labour_amount="100.00"))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Offer.objects.count(), 1)

    def test_price_150_is_accepted(self):
        response = self._post_offer(_offer_payload(labour_amount="150.00"))
        self.assertEqual(response.status_code, 201, response.data)

    def test_itemized_quotation_with_one_invalid_material_is_rejected(self):
        response = self._post_offer(
            _offer_payload(
                labour_amount="5000.00",
                materials=[
                    {"name": "Copper pipe", "quantity": "1", "unit": "pcs", "unit_price": "150.00"},
                    {"name": "Thread seal tape", "quantity": "2", "unit": "pcs", "unit_price": "50.00"},
                    {"name": "Pipe wrench", "quantity": "1", "unit": "pcs", "unit_price": "200.00"},
                ],
            )
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(MIN_INDIVIDUAL_PRICE_MESSAGE, str(response.data))
        # The whole submission is rejected: no quotation, no card event.
        self.assertEqual(Offer.objects.count(), 0)

    def test_itemized_quotation_with_all_valid_materials_is_accepted(self):
        response = self._post_offer(
            _offer_payload(
                labour_amount="5000.00",
                materials_amount="350.00",
                materials=[
                    {"name": "Copper pipe", "quantity": "1", "unit": "pcs", "unit_price": "150.00"},
                    {"name": "Thread seal tape", "quantity": "2", "unit": "pcs", "unit_price": "100.00"},
                ],
            )
        )
        self.assertEqual(response.status_code, 201, response.data)
        materials = response.data["materials"]
        self.assertEqual(len(materials), 2)
        self.assertEqual(Decimal(response.data["materials_amount"]), Decimal("350.00"))

    def test_materials_subtotal_below_minimum_without_rows_is_rejected(self):
        response = self._post_offer(_offer_payload(materials_amount="50.00"))
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(MIN_INDIVIDUAL_PRICE_MESSAGE, str(response.data))

    def test_disabled_material_charging_zero_is_valid(self):
        response = self._post_offer(
            _offer_payload(materials_amount="0", materials_included=False)
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_optional_transportation_zero_is_valid(self):
        response = self._post_offer(_offer_payload(transport_amount="0"))
        self.assertEqual(response.status_code, 201, response.data)

    def test_optional_transportation_below_minimum_is_rejected(self):
        response = self._post_offer(_offer_payload(transport_amount="50.00"))
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(MIN_INDIVIDUAL_PRICE_MESSAGE, str(response.data))

    def test_optional_extra_charge_zero_is_valid(self):
        response = self._post_offer(_offer_payload(additional_amount="0"))
        self.assertEqual(response.status_code, 201, response.data)

    def test_optional_extra_charge_below_minimum_is_rejected(self):
        response = self._post_offer(
            _offer_payload(
                additional_amount="75.00",
                additional_charge_details="Back-box replacement",
            )
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(MIN_INDIVIDUAL_PRICE_MESSAGE, str(response.data))

    def test_revised_quotation_cannot_introduce_a_below_minimum_price(self):
        first = self._post_offer(_offer_payload(labour_amount="25000.00"))
        self.assertEqual(first.status_code, 201, first.data)
        response = self.api.post(
            f"/api/v1/conversations/{self.conversation.id}/offers/counter/",
            _offer_payload(
                labour_amount="80.00",
                parent_offer_id=first.data["id"],
            ),
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"counter-{uuid.uuid4().hex}",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(MIN_INDIVIDUAL_PRICE_MESSAGE, str(response.data))

    def test_direct_service_layer_call_cannot_bypass_the_minimum(self):
        with self.assertRaises(Exception) as ctx:
            create_offer(
                conversation=self.conversation,
                user=self.artisan_user,
                payload={
                    "labour_amount": "99.99",
                    "materials_amount": "0",
                    "transport_amount": "0",
                    "additional_amount": "0",
                    "scheduled_start": timezone.now() + timedelta(days=3),
                    "duration_value": 2,
                    "duration_unit": "DAYS",
                },
                idempotency_key=f"svc-{uuid.uuid4().hex}",
            )
        self.assertIn(MIN_INDIVIDUAL_PRICE_MESSAGE, str(ctx.exception))

    def test_historical_offer_below_minimum_remains_readable(self):
        start = timezone.now() + timedelta(days=3)
        legacy = Offer.objects.create(
            conversation=self.conversation,
            created_by=self.artisan_user,
            labour_amount=Decimal("50.00"),
            materials_amount=Decimal("0.00"),
            total_amount=Decimal("50.00"),
            scheduled_start=start,
            scheduled_end=start + timedelta(days=1),
            duration_value=1,
            duration_unit="DAYS",
            expires_at=start,
            status=Offer.Status.PROPOSED,
        )
        self.api.force_authenticate(self.customer_user)
        response = self.api.get(f"/api/v1/conversations/{self.conversation.id}/")
        self.assertEqual(response.status_code, 200, response.content)
        active = response.data.get("active_offer") or {}
        self.assertEqual(str(active.get("id")), str(legacy.id))
        self.assertEqual(Decimal(active["labour_amount"]), Decimal("50.00"))


class AddonMinPriceApiTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        (
            self.conversation,
            self.customer_user,
            self.artisan_user,
        ) = _setup_service_conversation()
        customer = self.conversation.customer
        artisan = self.conversation.artisan
        start = timezone.now() - timedelta(hours=2)
        self.booking = Booking.objects.create(
            request=self.conversation.service_request,
            artisan=artisan,
            customer=CustomerProfile.objects.get(user=customer),
            scheduled_time_start=start,
            scheduled_time_end=start + timedelta(hours=2),
            price_agreed="25000.00",
            status=Booking.Status.IN_PROGRESS,
        )
        EscrowPayment.objects.create(
            booking=self.booking,
            customer=customer,
            artisan=artisan,
            reference=f"PAY-MIN-{uuid.uuid4().hex[:12]}",
            service_amount_kobo=2_500_000,
            total_amount_kobo=2_515_000,
            platform_commission_kobo=250_000,
            artisan_payout_kobo=2_250_000,
            status=EscrowPayment.Status.HELD,
        )
        self.url = f"/api/v1/booking/{self.booking.id}/additional-orders/"
        self.api.force_authenticate(self.artisan_user)

    def test_addon_below_minimum_is_rejected(self):
        response = self.api.post(
            self.url,
            {"amount": "50.00", "description": "Replace the valve"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"addon-{uuid.uuid4().hex}",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(MIN_INDIVIDUAL_PRICE_MESSAGE, str(response.data))
        self.assertEqual(BookingAdditionalOrder.objects.count(), 0)

    def test_addon_boundary_99_99_is_rejected(self):
        response = self.api.post(
            self.url,
            {"amount": "99.99", "description": "Replace the valve"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"addon-{uuid.uuid4().hex}",
        )
        self.assertEqual(response.status_code, 400, response.data)

    def test_addon_exactly_100_is_accepted(self):
        response = self.api.post(
            self.url,
            {"amount": "100.00", "description": "Replace the valve"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"addon-{uuid.uuid4().hex}",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(BookingAdditionalOrder.objects.count(), 1)

    def test_addon_counter_below_minimum_is_rejected(self):
        response = self.api.post(
            self.url,
            {"amount": "12000.00", "description": "Replace the valve"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"addon-{uuid.uuid4().hex}",
        )
        self.assertEqual(response.status_code, 201, response.data)
        order = BookingAdditionalOrder.objects.get(pk=response.data["id"])
        offer = order.offers.get(revision=1)
        self.api.force_authenticate(self.customer_user)
        counter = self.api.post(
            f"/api/v1/booking/{self.booking.id}/additional-orders/{order.id}/offers/{offer.id}/respond/",
            {"action": "counter", "amount": "50.00", "description": "Meet me halfway"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"addon-counter-{uuid.uuid4().hex}",
        )
        self.assertEqual(counter.status_code, 400, counter.data)
        self.assertIn(MIN_INDIVIDUAL_PRICE_MESSAGE, str(counter.data))
        self.assertEqual(BookingAdditionalOffer.objects.filter(order=order).count(), 1)

    def test_addon_counter_exactly_100_is_accepted(self):
        response = self.api.post(
            self.url,
            {"amount": "12000.00", "description": "Replace the valve"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"addon-{uuid.uuid4().hex}",
        )
        order = BookingAdditionalOrder.objects.get(pk=response.data["id"])
        offer = order.offers.get(revision=1)
        self.api.force_authenticate(self.customer_user)
        counter = self.api.post(
            f"/api/v1/booking/{self.booking.id}/additional-orders/{order.id}/offers/{offer.id}/respond/",
            {"action": "counter", "amount": "100.00", "description": "Meet me at a hundred"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=f"addon-counter-{uuid.uuid4().hex}",
        )
        self.assertEqual(counter.status_code, 200, counter.data)


class FinancialIntegrityTests(TestCase):
    """The minimum individual price must not leak into payout/escrow logic."""

    def test_payout_commission_and_fee_constants_are_unchanged(self):
        self.assertEqual(ARTISAN_COMMISSION_RATE, Decimal("0.1000"))
        self.assertEqual(CUSTOMER_FEE_KOBO, 15_000)

    def test_booking_money_calculation_matches_legacy_constants(self):
        values = calculate_booking_money(2_000_000)
        self.assertEqual(values["service_amount_kobo"], 2_000_000)
        self.assertEqual(values["customer_fee_kobo"], 15_000)
        self.assertEqual(values["total_amount_kobo"], 2_015_000)
        self.assertEqual(values["platform_commission_kobo"], 200_000)
        self.assertEqual(values["artisan_payout_kobo"], 1_800_000)
