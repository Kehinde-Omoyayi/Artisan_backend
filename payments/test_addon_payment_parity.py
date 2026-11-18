"""Add-on ↔ booking payment parity regression tests.

Pins the integration defects that let a separate add-on payment diverge from the
original booking's payment: hosted-checkout reconciliation must never fail a
live bank-transfer account, the fallback verifier must reach add-on payments,
and the add-on negotiation/payment moments must surface as actionable popups
with the metadata the frontend needs to open the exact card.
"""

import base64
import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from booking.models import Booking, BookingAdditionalOffer, BookingAdditionalOrder
from conversations.models import Conversation
from home.models import ArtisanProfile, CustomerProfile, CustomUser, Locations, ServiceCategory
from notifications.models import InAppNotification
from payments.models import PaymentBankTransfer, PaymentLedgerEntry
from payments.services import auto_verify_pending_transfer, prepare_additional_order_payment
from payments.tasks import auto_verify_ready_transfers, reconcile_payment_initialization, reconcile_stale_payments
from services.models import ServiceRequest

from .models import EscrowPayment


def make_transfer_row(payment, *, status=PaymentBankTransfer.Status.READY, expires_in_hours=1,
                      account="9755152912", bank="Flutterwave MFB", transfer_reference="TRF-ADDON-1"):
    return PaymentBankTransfer.objects.create(
        payment=payment,
        status=status,
        reference=payment.reference,
        transfer_reference=transfer_reference,
        transfer_account=account,
        transfer_bank=bank,
        beneficiary_name="Artisan Market Escrow",
        payer_name="Kehinde Omoyayi",
        narration="Escrow payment",
        transfer_note="Add-on escrow transfer",
        transfer_amount_kobo=payment.total_amount_kobo,
        mode="banktransfer",
        account_expires_at=timezone.now() + timedelta(hours=expires_in_hours),
        minted_at=timezone.now(),
    )


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "addon-parity-tests"}})
class AddonPaymentFixture(TestCase):
    def setUp(self):
        self.customer_user = CustomUser.objects.create_user(
            email="parity-customer@example.com", first_name="Ada", last_name="Customer",
            password="StrongPassword123!", is_active=True,
        )
        self.artisan_user = CustomUser.objects.create_user(
            email="parity-artisan@example.com", first_name="Tunde", last_name="Artisan",
            password="StrongPassword123!", is_active=True,
        )
        self.customer = CustomerProfile.objects.create(user=self.customer_user)
        self.artisan = ArtisanProfile.objects.create(user=self.artisan_user, profile_status="ACTIVE")
        category = ServiceCategory.objects.create(name="Plumbing")
        location = Locations.objects.create(country="Nigeria", state="Lagos", area="Yaba")
        service_request = ServiceRequest.objects.create(
            requester=self.customer_user, category=category, location=location,
            preferred_date=timezone.localdate(), description="Repair a leaking tap.",
        )
        start = timezone.now() - timedelta(hours=2)
        self.booking = Booking.objects.create(
            request=service_request, artisan=self.artisan, customer=self.customer,
            scheduled_time_start=start, scheduled_time_end=start + timedelta(hours=2),
            price_agreed="25000.00", status=Booking.Status.IN_PROGRESS,
        )
        self.base_payment = EscrowPayment.objects.create(
            booking=self.booking, customer=self.customer_user, artisan=self.artisan,
            reference="PAY-PARITY-BASE", service_amount_kobo=2_500_000,
            total_amount_kobo=2_515_000, platform_commission_kobo=250_000,
            artisan_payout_kobo=2_250_000, status=EscrowPayment.Status.HELD,
        )
        self.order = BookingAdditionalOrder.objects.create(
            booking=self.booking, created_by=self.artisan_user,
            status=BookingAdditionalOrder.Status.APPROVED,
        )
        self.offer = BookingAdditionalOffer.objects.create(
            order=self.order, revision=1, created_by=self.artisan_user, amount="12000.00",
            description="Replace the worn valve", status=BookingAdditionalOffer.Status.ACCEPTED,
            accepted_by=self.customer_user,
        )
        self.order.accepted_offer = self.offer
        self.order.save(update_fields=["accepted_offer", "updated_at"])
        self.api = APIClient()

    def make_extra_booking(self, suffix="extra"):
        category = ServiceCategory.objects.create(name=f"Category {suffix}")
        location = Locations.objects.create(country="Nigeria", state="Ogun", area="Epe")
        service_request = ServiceRequest.objects.create(
            requester=self.customer_user, category=category, location=location,
            preferred_date=timezone.localdate(), description="Second job.",
        )
        start = timezone.now() - timedelta(hours=1)
        return Booking.objects.create(
            request=service_request, artisan=self.artisan, customer=self.customer,
            scheduled_time_start=start, scheduled_time_end=start + timedelta(hours=2),
            price_agreed="10000.00", status=Booking.Status.IN_PROGRESS,
        )


class StaleReconciliationGuardTests(AddonPaymentFixture):
    def test_a_live_ready_account_is_not_queued_for_hosted_checkout_reconciliation(self):
        addon_payment = prepare_additional_order_payment(order=self.order, user=self.customer_user)
        make_transfer_row(addon_payment, status=PaymentBankTransfer.Status.READY)

        with patch("payments.tasks.reconcile_payment_initialization.delay") as delay:
            result = reconcile_stale_payments(older_than_minutes=0)

        queued_ids = [call.args[0] for call in delay.call_args_list]
        self.assertNotIn(str(addon_payment.id), queued_ids)
        self.assertEqual(result["queued"], 0)

    def test_an_outcome_unknown_account_is_not_failed_by_stale_reconciliation(self):
        addon_payment = prepare_additional_order_payment(order=self.order, user=self.customer_user)
        make_transfer_row(addon_payment, status=PaymentBankTransfer.Status.OUTCOME_UNKNOWN)

        with patch("payments.tasks.reconcile_payment_initialization.delay") as delay:
            result = reconcile_stale_payments(older_than_minutes=0)

        self.assertNotIn(str(addon_payment.id), [call.args[0] for call in delay.call_args_list])
        addon_payment.refresh_from_db()
        self.assertEqual(addon_payment.status, EscrowPayment.Status.PAYMENT_PENDING)

    def test_a_creating_reservation_is_protected_from_hosted_checkout_reconciliation(self):
        addon_payment = prepare_additional_order_payment(order=self.order, user=self.customer_user)
        make_transfer_row(addon_payment, status=PaymentBankTransfer.Status.CREATING)

        with patch("payments.tasks.reconcile_payment_initialization.delay") as delay:
            result = reconcile_stale_payments(older_than_minutes=0)

        self.assertNotIn(str(addon_payment.id), [call.args[0] for call in delay.call_args_list])

    def test_a_plain_hosted_checkout_payment_is_still_reconciled(self):
        plain = EscrowPayment.objects.create(
            booking=self.make_extra_booking("plain"), customer=self.customer_user, artisan=self.artisan,
            reference="PAY-PARITY-PLAIN", service_amount_kobo=100000,
            total_amount_kobo=115000, customer_fee_kobo=15000, platform_commission_kobo=10000,
            artisan_payout_kobo=90000,
        )
        # created_at is auto_now_add; age the row explicitly for the cutoff filter.
        EscrowPayment.objects.filter(pk=plain.pk).update(
            created_at=timezone.now() - timedelta(minutes=30)
        )
        with patch("payments.tasks.reconcile_payment_initialization.delay") as delay:
            result = reconcile_stale_payments(older_than_minutes=10)

        queued_ids = [call.args[0] for call in delay.call_args_list]
        self.assertIn(str(plain.id), queued_ids)

    def test_auto_verify_ready_transfers_nudges_live_accounts_only(self):
        addon_payment = prepare_additional_order_payment(order=self.order, user=self.customer_user)
        make_transfer_row(addon_payment, status=PaymentBankTransfer.Status.READY)
        # An expired account cannot receive money; it must not be nudged.
        expired_payment = EscrowPayment.objects.create(
            booking=self.make_extra_booking("expired"), customer=self.customer_user, artisan=self.artisan,
            reference="PAY-PARITY-EXPIRED", service_amount_kobo=50000,
            total_amount_kobo=65000, customer_fee_kobo=15000, platform_commission_kobo=5000,
            artisan_payout_kobo=45000,
        )
        make_transfer_row(expired_payment, status=PaymentBankTransfer.Status.READY,
                          expires_in_hours=-1, transfer_reference="TRF-EXPIRED", account="0000000000")

        with patch("payments.services.auto_verify_pending_transfer") as auto:
            result = auto_verify_ready_transfers()

        self.assertEqual(result["status"], "ok")
        nudged = [call.args[0].id for call in auto.call_args_list]
        self.assertIn(addon_payment.id, nudged)
        self.assertNotIn(expired_payment.id, nudged)


class AddonNegotiationNotificationTests(TestCase):
    """The negotiation moments must be actionable popups, not plain toasts."""

    def setUp(self):
        self.customer_user = CustomUser.objects.create_user(
            email="notif-customer@example.com", first_name="Ada", last_name="Customer",
            password="StrongPassword123!", is_active=True,
        )
        self.artisan_user = CustomUser.objects.create_user(
            email="notif-artisan@example.com", first_name="Tunde", last_name="Artisan",
            password="StrongPassword123!", is_active=True,
        )
        self.customer = CustomerProfile.objects.create(user=self.customer_user)
        self.artisan = ArtisanProfile.objects.create(user=self.artisan_user, profile_status="ACTIVE")
        category = ServiceCategory.objects.create(name="Carpentry")
        location = Locations.objects.create(country="Nigeria", state="Lagos", area="Yaba")
        service_request = ServiceRequest.objects.create(
            requester=self.customer_user, category=category, location=location,
            preferred_date=timezone.localdate(), description="Build a shelf.",
        )
        start = timezone.now() - timedelta(hours=1)
        self.booking = Booking.objects.create(
            request=service_request, artisan=self.artisan, customer=self.customer,
            scheduled_time_start=start, scheduled_time_end=start + timedelta(hours=2),
            price_agreed="18000.00", status=Booking.Status.IN_PROGRESS,
        )
        self.base_payment = EscrowPayment.objects.create(
            booking=self.booking, customer=self.customer_user, artisan=self.artisan,
            reference="PAY-NOTIF-BASE", service_amount_kobo=1_800_000,
            total_amount_kobo=1_815_000, platform_commission_kobo=180_000,
            artisan_payout_kobo=1_620_000, status=EscrowPayment.Status.HELD,
        )
        self.conversation = Conversation.objects.create(
            customer=self.customer_user, artisan=self.artisan,
            service_request=self.booking.request,
        )

    def _create(self, amount="12000.00", description="Replace the worn valve"):
        from booking.additional_orders import create_additional_order

        return create_additional_order(
            booking_id=self.booking.id, user=self.artisan_user,
            amount=amount, description=description,
        )

    def test_a_new_proposal_sends_a_decision_modal_to_the_customer(self):
        order = self._create()
        note = InAppNotification.objects.get(
            recipient=self.customer_user, event_key__startswith="additional_order_proposed:"
        )
        self.assertEqual(note.data.get("popup"), "modal")
        self.assertEqual(note.data.get("additional_order_id"), str(order.id))
        self.assertEqual(note.data.get("offer_id"), str(order.offers.get(revision=1).id))
        self.assertEqual(note.data.get("conversation_id"), str(self.conversation.id))
        self.assertEqual(note.data.get("revision"), 1)
        self.assertEqual(note.data.get("amount"), "12000.00")
        self.assertEqual(note.data.get("order_status"), "NEGOTIATING")
        self.assertEqual(note.data.get("description"), "Replace the worn valve")
        self.assertTrue(str(note.data.get("action_url", "")).endswith(f"#additional-order-{order.id}"))

    def test_the_artisan_gets_a_confirmation_toast_not_a_modal(self):
        order = self._create()
        note = InAppNotification.objects.get(
            recipient=self.artisan_user, event_key__startswith="additional_order_sent:"
        )
        self.assertEqual(note.data.get("popup"), "toast")
        self.assertEqual(note.data.get("additional_order_id"), str(order.id))

    def test_a_counter_offer_sends_a_modal_naming_both_amounts(self):
        order = self._create()
        offer = order.offers.get(revision=1)
        from booking.additional_orders import respond_to_additional_order

        respond_to_additional_order(
            booking_id=self.booking.id, order_id=order.id, offer_id=offer.id,
            user=self.customer_user, action="counter",
            amount="14000.00", description="Replace the valve and test pressure",
        )
        note = InAppNotification.objects.get(
            recipient=self.artisan_user, event_key__startswith="additional_order_countered:"
        )
        self.assertEqual(note.data.get("popup"), "modal")
        self.assertEqual(note.data.get("revision"), 2)
        self.assertEqual(note.data.get("amount"), "14000.00")
        self.assertIn("12,000.00", note.message)
        self.assertIn("14,000.00", note.message)

    def test_acceptance_notifies_the_proposer_with_a_toast(self):
        order = self._create()
        offer = order.offers.get(revision=1)
        from booking.additional_orders import respond_to_additional_order

        respond_to_additional_order(
            booking_id=self.booking.id, order_id=order.id, offer_id=offer.id,
            user=self.customer_user, action="accept",
        )
        note = InAppNotification.objects.get(
            recipient=self.artisan_user, event_key__startswith="additional_order_accepted:"
        )
        self.assertEqual(note.data.get("popup"), "toast")
        self.assertEqual(note.data.get("order_status"), "APPROVED")
        self.assertTrue(str(note.data.get("action_url", "")).endswith(f"#additional-order-{order.id}"))

    def test_declining_notifies_the_proposer_and_never_creates_a_payment(self):
        order = self._create()
        offer = order.offers.get(revision=1)
        from booking.additional_orders import respond_to_additional_order

        respond_to_additional_order(
            booking_id=self.booking.id, order_id=order.id, offer_id=offer.id,
            user=self.customer_user, action="decline",
        )
        note = InAppNotification.objects.get(
            recipient=self.artisan_user, event_key__startswith="additional_order_declined:"
        )
        self.assertEqual(note.data.get("popup"), "toast")
        self.assertFalse(EscrowPayment.objects.filter(addon_order=order).exists())


class AddonPaymentPayloadTests(AddonPaymentFixture):
    """The conversation payload must carry the full PaymentSerializer contract."""

    def setUp(self):
        super().setUp()
        self.conversation = Conversation.objects.create(
            customer=self.customer_user, artisan=self.artisan,
            service_request=self.booking.request,
        )

    def _conversation_payload(self, user):
        self.api.force_authenticate(user)
        response = self.api.get(f"/api/v1/conversations/{self.conversation.id}/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["additional_orders"][0]

    def test_the_customer_sees_the_complete_payment_contract(self):
        payment = prepare_additional_order_payment(order=self.order, user=self.customer_user)
        addon = self._conversation_payload(self.customer_user)
        payload = addon["payment"]

        for field in (
            "id", "reference", "status", "service_amount", "customer_fee", "total_amount",
            "currency", "authorization_url", "checkout", "bank_transfer", "failure_reason",
            "verified_at", "held_at", "released_at", "receipt",
        ):
            self.assertIn(field, payload, field)
        self.assertEqual(payload["reference"], payment.reference)
        self.assertEqual(payload["status"], "PAYMENT_PENDING")
        self.assertEqual(Decimal(str(payload["total_amount"])), Decimal("121.50").__class__(payment.total_amount_kobo) / Decimal(100))

    def test_the_customer_sees_the_ready_account_details(self):
        payment = prepare_additional_order_payment(order=self.order, user=self.customer_user)
        make_transfer_row(payment, account="9755152912", bank="Flutterwave MFB")

        addon = self._conversation_payload(self.customer_user)
        transfer = addon["payment"]["bank_transfer"]
        self.assertEqual(transfer.get("account_number"), "9755152912")
        self.assertEqual(transfer.get("bank"), "Flutterwave MFB")
        self.assertEqual(transfer.get("beneficiary_name"), "Artisan Market Escrow")

    def test_the_artisan_never_sees_the_transfer_account_but_sees_status(self):
        payment = prepare_additional_order_payment(order=self.order, user=self.customer_user)
        make_transfer_row(payment, account="9755152912")

        addon = self._conversation_payload(self.artisan_user)
        payload = addon["payment"]
        self.assertEqual(payload["bank_transfer"], {})
        self.assertEqual(payload["status"], "PAYMENT_PENDING")
        self.assertEqual(addon["can_pay"], False)


@override_settings(FLW_SECRET_HASH="test-secret")
@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "addon-webhook-tests"}})
class AddonFallbackVerificationTests(AddonPaymentFixture):
    """The fallback path must settle an add-on transfer exactly once."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def provider_result(self, payment, tx_id=982):
        return {"status": "success", "data": {
            "id": tx_id, "tx_ref": payment.reference, "status": "successful",
            "currency": "NGN", "amount": payment.total_amount_kobo // 100,
        }}

    def test_the_fallback_verifier_settles_an_add_on_transfer_once(self):
        payment = prepare_additional_order_payment(order=self.order, user=self.customer_user)
        row = make_transfer_row(payment)

        with patch("payments.services.FlutterwaveClient.verify_transaction_by_reference",
                   return_value=self.provider_result(payment)):
            auto_verify_pending_transfer(payment)

        payment.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(payment.status, EscrowPayment.Status.HELD)
        self.assertIsNotNone(payment.verified_at)
        self.assertIsNotNone(payment.held_at)
        self.assertEqual(row.status, PaymentBankTransfer.Status.PAID)
        self.assertEqual(
            PaymentLedgerEntry.objects.filter(payment=payment, event_type="ESCROW_HELD").count(),
            1,
        )

        # A webhook arriving after the fact must not double-settle or duplicate the ledger.
        with patch("payments.services.FlutterwaveClient.verify_transaction_by_reference",
                   return_value=self.provider_result(payment)):
            auto_verify_pending_transfer(payment)
        self.assertEqual(
            PaymentLedgerEntry.objects.filter(payment=payment, event_type="ESCROW_HELD").count(),
            1,
        )

    def test_a_webhook_for_the_add_on_payment_enqueues_verification(self):
        payment = prepare_additional_order_payment(order=self.order, user=self.customer_user)
        payload = {"event": "charge.completed", "data": {
            "id": 982, "tx_ref": payment.reference, "status": "successful",
            "currency": "NGN", "amount": payment.total_amount_kobo // 100,
        }}
        body = json.dumps(payload).encode()
        signature = base64.b64encode(
            hmac.new(b"test-secret", body, hashlib.sha256).digest()
        ).decode()
        with patch("payments.views.verify_flutterwave_payment.delay") as delay, \
                self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/v1/payments/flutterwave/webhook/", data=body,
                content_type="application/json", HTTP_FLUTTERWAVE_SIGNATURE=signature,
            )
        self.assertEqual(response.status_code, 200)
        delay.assert_called_once()
        called_kwargs = delay.call_args
        self.assertEqual(called_kwargs.args[0], str(payment.id))

    def test_an_unpaid_add_on_transfer_stays_pending(self):
        payment = prepare_additional_order_payment(order=self.order, user=self.customer_user)
        make_transfer_row(payment)

        class FakeFlutterwavePending(Exception):
            pass

        pending_result = {"status": "success", "data": {
            "id": 982, "tx_ref": payment.reference, "status": "pending",
            "currency": "NGN", "amount": payment.total_amount_kobo // 100,
        }}
        with patch("payments.services.FlutterwaveClient.verify_transaction_by_reference",
                   return_value=pending_result):
            auto_verify_pending_transfer(payment)

        payment.refresh_from_db()
        self.assertEqual(payment.status, EscrowPayment.Status.PAYMENT_PENDING)
