"""Bank-transfer state machine and concurrency regression tests.

What these pin down, in one place:

* one active virtual account per payment, forever, however the caller behaves;
* duplicate, concurrent or retried requests never mint a second account;
* a provider that never answered is reconciled on the original reference, not
  re-minted until the provider says whether the charge exists;
* the provider's own ``account_expiration`` -- never a local TTL -- decides
  when an account is too old to use or to renew;
* the payer's name is never substituted for the provider-named beneficiary.
"""

from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from rest_framework.test import APIClient

from booking.models import Booking
from home.models import (
    ArtisanProfile,
    CustomerProfile,
    CustomUser,
    Locations,
    Service,
    ServiceCategory,
)
from services.models import ServiceRequest

from .flutterwave import FlutterwaveError, FlutterwaveUnknownError
from .models import EscrowPayment, PaymentBankTransfer
from .services import ensure_bank_transfer_charge



class BankTransferStateMachineTests(TestCase):
    def setUp(self):
        cache.clear()
        self.customer_user = CustomUser.objects.create_user(
            email="payer@example.com", first_name="Kehinde", last_name="Omoyayi",
            password="StrongPassword123!", is_active=True,
        )
        artisan_user = CustomUser.objects.create_user(
            email="artisan-sm@example.com", first_name="Ada", last_name="Obi",
            password="StrongPassword123!", is_active=True,
        )
        customer = CustomerProfile.objects.create(user=self.customer_user)
        artisan = ArtisanProfile.objects.create(user=artisan_user)
        category = ServiceCategory.objects.create(name="Machines")
        Service.objects.create(category=category, name="Machine Repair")
        location = Locations.objects.create(country="Nigeria", state="Lagos", area="Lekki")
        request = ServiceRequest.objects.create(
            requester=self.customer_user, category=category, location=location,
            preferred_date=(timezone.now() + timedelta(days=1)).date(),
            description="The generator will not start.",
        )
        self.booking = Booking.objects.create(
            request=request, artisan=artisan, customer=customer,
            scheduled_time_start=timezone.now() + timedelta(days=1),
            scheduled_time_end=timezone.now() + timedelta(days=1, hours=2),
            price_agreed="25150.00", status=Booking.Status.ACCEPTED,
        )
        self.payment = EscrowPayment.objects.create(
            booking=self.booking, customer=self.customer_user, artisan=artisan,
            reference="PAY-SM-1", service_amount_kobo=2_500_000,
            total_amount_kobo=2_515_000, platform_commission_kobo=250_000,
            artisan_payout_kobo=2_250_000,
        )

    # ------------------------------------------------------------------ fixtures

    @staticmethod
    def provider_answer(*, account="9755152912", bank="Flutterwave MFB",
                        expires_in_hours=1, amount="25150.00",
                        transfer_reference="ZZSS4548178111081675127069",
                        beneficiary=None):
        authorization = {
            "transfer_reference": transfer_reference,
            "transfer_account": account,
            "transfer_bank": bank,
            "account_expiration": (
                timezone.now() + timedelta(hours=expires_in_hours)
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "transfer_note": "Please make a bank transfer to Air Conditioning with test FLW",
            "transfer_amount": amount,
            "narration": "Air Conditioning with test FLW",
            "mode": "banktransfer",
        }
        if beneficiary:
            authorization["account_name"] = beneficiary
        return {"status": "success", "message": "Charge initiated",
                "meta": {"authorization": authorization}}

    def api(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.customer_user)
        return client

    def expire_account(self, *, hours=0):
        row = PaymentBankTransfer.objects.get(payment=self.payment)
        row.account_expires_at = timezone.now() - timedelta(hours=hours)
        row.status = PaymentBankTransfer.Status.READY
        row.save(update_fields=["account_expires_at", "status", "updated_at"])

    # ------------------------------------------------------------- state machine

    @patch("payments.services.FlutterwaveClient")
    def test_one_account_is_minted_for_one_payment(self, client_class):
        client_class.return_value.create_bank_transfer_charge.return_value = self.provider_answer()
        api = self.api()

        first = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")
        second = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["account_number"], second.json()["account_number"])
        self.assertEqual(client_class.return_value.create_bank_transfer_charge.call_count, 1)
        self.assertEqual(
            PaymentBankTransfer.objects.filter(payment=self.payment).count(), 1
        )

    @patch("payments.services.FlutterwaveClient")
    def test_duplicate_requests_reuse_the_stored_account_and_reference(self, client_class):
        client_class.return_value.create_bank_transfer_charge.return_value = self.provider_answer()
        api = self.api()
        api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")
        self.payment.refresh_from_db()
        reference_after_first = self.payment.reference

        again = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.assertEqual(again.json()["reference"], reference_after_first)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.reference, reference_after_first)

    @patch("payments.services.FlutterwaveClient")
    def test_a_request_in_flight_is_reported_as_processing_and_never_reexecuted(self, client_class):
        """A reserved-but-unfinished attempt blocks a competing mint."""
        PaymentBankTransfer.objects.create(
            payment=self.payment,
            status=PaymentBankTransfer.Status.CREATING,
            reference=self.payment.reference,
            creation_started_at=timezone.now(),
            attempts=1,
        )
        api = self.api()

        response = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "CREATING")
        client_class.return_value.create_bank_transfer_charge.assert_not_called()

    @patch("payments.services.FlutterwaveClient")
    def test_a_stale_reservation_becomes_outcome_unknown_not_a_second_account(self, client_class):
        PaymentBankTransfer.objects.create(
            payment=self.payment,
            status=PaymentBankTransfer.Status.CREATING,
            reference=self.payment.reference,
            creation_started_at=timezone.now() - timedelta(minutes=10),
            attempts=1,
        )
        client_class.return_value.verify_transaction_by_reference.side_effect = (
            FlutterwaveUnknownError("still nothing")
        )
        api = self.api()

        response = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.assertEqual(response.status_code, 202)
        row = PaymentBankTransfer.objects.get(payment=self.payment)
        self.assertEqual(row.status, PaymentBankTransfer.Status.OUTCOME_UNKNOWN)
        client_class.return_value.create_bank_transfer_charge.assert_not_called()

    @patch("payments.services.FlutterwaveClient")
    def test_an_interrupted_provider_call_is_reconciled_not_retried(self, client_class):
        client_class.return_value.create_bank_transfer_charge.side_effect = (
            FlutterwaveUnknownError("request timed out")
        )
        api = self.api()

        first = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.assertEqual(first.status_code, 202)
        row = PaymentBankTransfer.objects.get(payment=self.payment)
        self.assertEqual(row.status, PaymentBankTransfer.Status.OUTCOME_UNKNOWN)
        self.assertEqual(client_class.return_value.create_bank_transfer_charge.call_count, 1)

        # The provider did, in fact, create the account; the response was lost.
        client_class.return_value.create_bank_transfer_charge.side_effect = None
        client_class.return_value.verify_transaction_by_reference.return_value = {
            "status": "success",
            "data": {
                "tx_ref": "PAY-SM-1",
                "status": "pending",
                "currency": "NGN",
                "amount": 25150,
                "meta": {"authorization": self.provider_answer()["meta"]["authorization"]},
            },
        }
        second = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["account_number"], "9755152912")
        row.refresh_from_db()
        self.assertEqual(row.status, PaymentBankTransfer.Status.READY)
        # Never one more charge: the same account is recovered, not re-created.
        self.assertEqual(client_class.return_value.create_bank_transfer_charge.call_count, 1)

    @patch("payments.services.FlutterwaveClient")
    def test_an_unknown_outcome_blocks_minting_until_the_provider_answers(self, client_class):
        client_class.return_value.create_bank_transfer_charge.side_effect = (
            FlutterwaveUnknownError("request timed out")
        )
        api = self.api()
        api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")
        client_class.return_value.verify_transaction_by_reference.side_effect = (
            FlutterwaveUnknownError("still down")
        )

        again = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.assertEqual(again.status_code, 202)
        self.assertEqual(client_class.return_value.create_bank_transfer_charge.call_count, 1)

    @patch("payments.services.FlutterwaveClient")
    def test_provider_confirms_the_attempt_never_existed_then_a_fresh_mint_is_allowed(self, client_class):
        client_class.return_value.create_bank_transfer_charge.side_effect = (
            FlutterwaveUnknownError("request timed out")
        )
        api = self.api()
        api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")
        client_class.return_value.verify_transaction_by_reference.side_effect = (
            FlutterwaveError("No transaction was found for this id")
        )
        client_class.return_value.create_bank_transfer_charge.side_effect = None
        client_class.return_value.create_bank_transfer_charge.return_value = self.provider_answer()

        second = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["account_number"], "9755152912")
        self.assertEqual(client_class.return_value.create_bank_transfer_charge.call_count, 2)

    # --------------------------------------------------------------- expiry rules

    @patch("payments.services.FlutterwaveClient")
    def test_provider_expiry_controls_the_window_not_a_local_countdown(self, client_class):
        """The provider account lives for an hour; the local clock is irrelevant."""
        client_class.return_value.create_bank_transfer_charge.return_value = self.provider_answer(
            expires_in_hours=3
        )
        api = self.api()
        api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")
        row = PaymentBankTransfer.objects.get(payment=self.payment)

        self.assertGreater((row.account_expires_at - timezone.now()).total_seconds(), 2 * 3600)
        # The old local TTL has passed; the provider account has not.
        row.minted_at = timezone.now() - timedelta(minutes=40)
        row.save(update_fields=["minted_at", "updated_at"])

        shown = api.get(f"/api/v1/payments/{self.payment.id}/").json()["bank_transfer"]
        self.assertTrue(shown["live"])
        self.assertGreater(shown["seconds_left"], 2 * 3600)
        reference_before = EscrowPayment.objects.get(pk=self.payment.pk).reference

        again = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(
            EscrowPayment.objects.get(pk=self.payment.pk).reference, reference_before
        )

    @patch("payments.services.FlutterwaveClient")
    def test_renewal_is_refused_until_the_provider_expiry_passes(self, client_class):
        client_class.return_value.create_bank_transfer_charge.return_value = self.provider_answer()
        api = self.api()
        api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        response = api.post(
            f"/api/v1/payments/{self.payment.id}/bank-transfer/", {"renew": True}
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            client_class.return_value.create_bank_transfer_charge.call_count, 1
        )

    @patch("payments.services.FlutterwaveClient")
    def test_an_expired_account_is_replaced_against_a_rotated_reference(self, client_class):
        client_class.return_value.create_bank_transfer_charge.return_value = self.provider_answer()
        client_class.return_value.verify_transaction_by_reference.return_value = {
            "status": "success",
            "data": {"tx_ref": "PAY-SM-1", "status": "pending", "currency": "NGN", "amount": 25150},
        }
        api = self.api()
        api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")
        self.expire_account(hours=0)
        retired = EscrowPayment.objects.get(pk=self.payment.pk).reference

        response = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.assertEqual(response.status_code, 200)
        self.payment.refresh_from_db()
        self.assertNotEqual(self.payment.reference, retired)
        row = PaymentBankTransfer.objects.get(payment=self.payment)
        self.assertEqual(row.reference, self.payment.reference)
        self.assertEqual(row.status, PaymentBankTransfer.Status.READY)

    @patch("payments.services.FlutterwaveClient")
    def test_an_expired_account_with_money_in_flight_is_not_replaced(self, client_class):
        client_class.return_value.create_bank_transfer_charge.return_value = self.provider_answer()
        client_class.return_value.verify_transaction_by_reference.return_value = {
            "status": "success",
            "data": {
                "id": 2028146665,
                "tx_ref": "PAY-SM-1",
                "status": "successful",
                "currency": "NGN",
                "amount": 25150,
            },
        }
        api = self.api()
        api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")
        self.expire_account(hours=0)
        reference_before = EscrowPayment.objects.get(pk=self.payment.pk).reference

        response = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.assertEqual(response.status_code, 400)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, EscrowPayment.Status.HELD)
        # No new account was minted for money that had already arrived.
        self.assertEqual(
            client_class.return_value.create_bank_transfer_charge.call_count, 1
        )
        self.assertEqual(self.payment.reference, reference_before)

    # -------------------------------------------------------------- data quality

    @patch("payments.services.FlutterwaveClient")
    def test_the_full_transfer_card_is_persisted(self, client_class):
        answer = self.provider_answer(beneficiary="Artisan Market Escrow")
        client_class.return_value.create_bank_transfer_charge.return_value = answer
        api = self.api()

        response = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")
        data = response.json()

        for field in (
            "amount", "amount_kobo", "currency", "bank", "account_number",
            "account_name", "beneficiary_name", "payer_name", "narration",
            "reference", "transfer_reference", "expires_at", "minted_at",
            "mode", "note", "status", "seconds_left", "live",
        ):
            self.assertIn(field, data, field)
        self.assertEqual(data["currency"], "NGN")
        self.assertEqual(data["amount_kobo"], 2_515_000)
        self.assertEqual(data["beneficiary_name"], "Artisan Market Escrow")
        self.assertEqual(data["payer_name"], "Kehinde Omoyayi")
        self.assertEqual(data["transfer_reference"], "ZZSS4548178111081675127069")
        self.assertEqual(data["mode"], "banktransfer")

    @patch("payments.services.FlutterwaveClient")
    def test_a_provider_amount_below_the_payment_is_refused_not_stored(self, client_class):
        answer = self.provider_answer(amount="25000.00")  # less than the payment calculates
        client_class.return_value.create_bank_transfer_charge.return_value = answer
        api = self.api()

        response = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.assertEqual(response.status_code, 400)
        row = PaymentBankTransfer.objects.get(payment=self.payment)
        self.assertEqual(row.status, PaymentBankTransfer.Status.FAILED)
        # The refused reference is retired: the next attempt never collides
        # with the account the provider may have created.
        self.assertNotEqual(
            EscrowPayment.objects.get(pk=self.payment.pk).reference, "PAY-SM-1"
        )

    @patch("payments.services.FlutterwaveClient")
    def test_a_provider_amount_implausibly_above_the_payment_is_refused(self, client_class):
        answer = self.provider_answer(amount="30000.00")  # excess far beyond any fee
        client_class.return_value.create_bank_transfer_charge.return_value = answer
        api = self.api()

        response = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.assertEqual(response.status_code, 400)
        row = PaymentBankTransfer.objects.get(payment=self.payment)
        self.assertEqual(row.status, PaymentBankTransfer.Status.FAILED)

    @patch("payments.services.FlutterwaveClient")
    def test_a_provider_fee_inside_the_band_is_accepted_and_stored(self, client_class):
        # ₦25,150 escrow; the provider asks for ₦25,175.00 (their 2%-ish fee is
        # baked into transfer_amount). The payer must be told the real figure.
        answer = self.provider_answer(amount="25175.00")
        client_class.return_value.create_bank_transfer_charge.return_value = answer
        api = self.api()
        reference_before = EscrowPayment.objects.get(pk=self.payment.pk).reference

        response = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["amount"], "25175.00")
        self.assertEqual(data["amount_kobo"], 2_517_500)
        row = PaymentBankTransfer.objects.get(payment=self.payment)
        self.assertEqual(row.status, PaymentBankTransfer.Status.READY)
        self.assertEqual(row.transfer_amount_kobo, 2_517_500)
        # No rotation on success: the reference is stable.
        self.assertEqual(
            EscrowPayment.objects.get(pk=self.payment.pk).reference, reference_before
        )

    @patch("payments.services.FlutterwaveClient")
    def test_a_refused_attempt_remints_against_a_fresh_reference(self, client_class):
        # Exactly the state the live system was left in: FAILED with the
        # mismatch marker. The next tap must mint against a NEW reference.
        row = PaymentBankTransfer.objects.create(
            payment=self.payment,
            status=PaymentBankTransfer.Status.FAILED,
            reference=self.payment.reference,
            last_error="provider_amount_mismatch",
            attempts=1,
        )
        answer = self.provider_answer(amount="25175.00")
        client_class.return_value.create_bank_transfer_charge.return_value = answer
        api = self.api()

        response = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.assertEqual(response.status_code, 200)
        payment = EscrowPayment.objects.get(pk=self.payment.pk)
        self.assertNotEqual(payment.reference, "PAY-SM-1")
        row.refresh_from_db()
        self.assertEqual(row.reference, payment.reference)
        # And the provider was called with the fresh reference, never the retired one.
        _, kwargs = client_class.return_value.create_bank_transfer_charge.call_args
        self.assertEqual(kwargs["reference"], payment.reference)

    @patch("payments.services.FlutterwaveClient")
    def test_an_unauthorised_user_cannot_retrieve_transfer_details(self, client_class):
        client_class.return_value.create_bank_transfer_charge.return_value = self.provider_answer()
        api = self.api()
        api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")
        stranger = CustomUser.objects.create_user(
            email="stranger@example.com", first_name="Stranger", last_name="One",
            password="StrongPassword123!",
        )
        artisan = CustomUser.objects.create_user(
            email="worker-sm@example.com", first_name="Worker", last_name="Two",
            password="StrongPassword123!",
        )

        stranger_response = self.api(stranger).get(f"/api/v1/payments/{self.payment.id}/")
        artisan_response = self.api(artisan).get(f"/api/v1/payments/{self.payment.id}/")

        self.assertEqual(stranger_response.status_code, 404)
        self.assertEqual(artisan_response.status_code, 404)

    @patch("payments.services.FlutterwaveClient")
    def test_cancelling_the_payment_blocks_new_transfer_requests(self, client_class):
        client_class.return_value.create_bank_transfer_charge.return_value = self.provider_answer()
        from .services import void_booking_payment_link

        void_booking_payment_link(payment=self.payment, reason="booking_cancelled")
        api = self.api()

        response = api.post(f"/api/v1/payments/{self.payment.id}/bank-transfer/")

        self.assertEqual(response.status_code, 400)
        client_class.return_value.create_bank_transfer_charge.assert_not_called()

    @patch("payments.services.FlutterwaveClient")
    def test_service_layer_reports_processing_when_reservation_is_in_flight(self, client_class):
        """A second reservation opened by another worker is never executed twice."""
        PaymentBankTransfer.objects.create(
            payment=self.payment,
            status=PaymentBankTransfer.Status.CREATING,
            reference=self.payment.reference,
            creation_started_at=timezone.now(),
            attempts=1,
        )
        result = ensure_bank_transfer_charge(self.payment)
        self.assertEqual(result["status"], "CREATING")
        client_class.return_value.create_bank_transfer_charge.assert_not_called()


class BankTransferBeneficiaryDisplayTests(TestCase):
    """Legacy rows minted before the fallback existed still surface the name."""

    def setUp(self):
        cache.clear()
        self.customer_user = CustomUser.objects.create_user(
            email="payer-ben@example.com", first_name="Kehinde", last_name="Omoyayi",
            password="StrongPassword123!", is_active=True,
        )
        artisan_user = CustomUser.objects.create_user(
            email="artisan-ben@example.com", first_name="Ada", last_name="Obi",
            password="StrongPassword123!", is_active=True,
        )
        customer = CustomerProfile.objects.create(user=self.customer_user)
        artisan = ArtisanProfile.objects.create(user=artisan_user)
        category = ServiceCategory.objects.create(name="Aircon")
        Service.objects.create(category=category, name="AC Repair")
        location = Locations.objects.create(country="Nigeria", state="Lagos", area="Lekki")
        request = ServiceRequest.objects.create(
            requester=self.customer_user, category=category, location=location,
            preferred_date=(timezone.now() + timedelta(days=1)).date(),
            description="Unit not cooling.",
        )
        self.booking = Booking.objects.create(
            request=request, artisan=artisan, customer=customer,
            scheduled_time_start=timezone.now() + timedelta(days=1),
            scheduled_time_end=timezone.now() + timedelta(days=1, hours=2),
            price_agreed="25150.00", status=Booking.Status.ACCEPTED,
        )
        self.payment = EscrowPayment.objects.create(
            booking=self.booking, customer=self.customer_user, artisan=artisan,
            reference="PAY-BEN-1", service_amount_kobo=2_500_000,
            total_amount_kobo=2_515_000, platform_commission_kobo=250_000,
            artisan_payout_kobo=2_250_000,
        )

    def test_a_stored_empty_beneficiary_is_derived_from_the_provider_instruction(self):
        """A row minted before the fallback still displays the provider's name."""
        PaymentBankTransfer.objects.create(
            payment=self.payment,
            status=PaymentBankTransfer.Status.READY,
            reference=self.payment.reference,
            transfer_account="9173169600",
            transfer_bank="Flutterwave MFB (Formerly OK MFB)",
            transfer_note="Please make a bank transfer to Air Conditioning with test FLW",
            narration="Air Conditioning with test FLW",
            beneficiary_name="",          # persisted before the fallback chain
            payer_name="Kehinde Omoyayi",
            transfer_amount_kobo=2_517_500,
            account_expires_at=timezone.now() + timedelta(hours=2),
            attempts=1,
        )
        client_auth = APIClient()
        client_auth.force_authenticate(user=self.customer_user)

        shown = client_auth.get(f"/api/v1/payments/{self.payment.id}/").json()["bank_transfer"]

        self.assertEqual(shown["beneficiary_name"], "Air Conditioning with test FLW")
        self.assertEqual(shown["account_name"], "Air Conditioning with test FLW")
        self.assertEqual(shown["payer_name"], "Kehinde Omoyayi")
