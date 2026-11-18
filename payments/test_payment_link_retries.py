"""A payment link is submitted once, looked up once more, and then reported.

The payer is the only person watching, and they are watching a chat page. A
queue that kept retrying left them reading "preparing your link" for twenty
minutes with no idea whether money had moved. The rule now is: one submit, one
more look, and if the provider still cannot say, the payment is failed in plain
words and the customer is told -- with nothing charged.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from datetime import timedelta

from booking.models import Booking
from home.models import ArtisanProfile, CustomUser, CustomerProfile, Locations, ServiceCategory, Service
from notifications.models import InAppNotification
from services.models import ServiceRequest

from .flutterwave import FlutterwaveUnknownError
from .models import EscrowPayment
from .tasks import RECONCILE_MAX_RETRIES, reconcile_payment_initialization


class PaymentLinkRetryTests(TestCase):
    """One more look, then an answer the payer can actually read."""

    def setUp(self):
        cache.clear()
        self.customer_user = CustomUser.objects.create_user(
            email="link-retry@example.com", first_name="Kehinde", last_name="Omoyayi",
            password="StrongPassword123!", is_active=True,
        )
        artisan_user = CustomUser.objects.create_user(
            email="link-retry-worker@example.com", first_name="Ada", last_name="Obi",
            password="StrongPassword123!", is_active=True,
        )
        customer = CustomerProfile.objects.create(user=self.customer_user)
        artisan = ArtisanProfile.objects.create(user=artisan_user)
        category = ServiceCategory.objects.create(name="Link Retries")
        Service.objects.create(category=category, name="Link Repair")
        location = Locations.objects.create(country="Nigeria", state="Lagos", area="Yaba")
        request = ServiceRequest.objects.create(
            requester=self.customer_user, category=category, location=location,
            preferred_date=(timezone.now() + timedelta(days=1)).date(),
            description="A socket needs replacing.",
        )
        self.booking = Booking.objects.create(
            request=request, artisan=artisan, customer=customer,
            scheduled_time_start=timezone.now() + timedelta(days=1),
            scheduled_time_end=timezone.now() + timedelta(days=1, hours=2),
            price_agreed="25150.00", status=Booking.Status.ACCEPTED,
        )
        self.payment = EscrowPayment.objects.create(
            booking=self.booking, customer=self.customer_user, artisan=artisan,
            reference="PAY-LINK-RETRY-1", service_amount_kobo=2_500_000,
            total_amount_kobo=2_515_000, platform_commission_kobo=250_000,
            artisan_payout_kobo=2_250_000,
        )

    def _run(self, retries=0):
        """Run the task inline; in eager mode a retry re-runs it straight away."""
        task = reconcile_payment_initialization
        task.push_request(retries=retries)
        try:
            applied = task.apply(args=[str(self.payment.pk)])
        finally:
            task.pop_request()
        return applied.result

    def test_the_queue_is_told_to_chase_a_link_only_once(self):
        """The knob itself, so a future edit cannot quietly put the wait back."""
        self.assertEqual(RECONCILE_MAX_RETRIES, 1)
        self.assertEqual(
            reconcile_payment_initialization.max_retries, 1,
            "A payment link must not be chased by the queue more than once.",
        )

    @patch("payments.tasks.FlutterwaveClient")
    def test_an_unknown_outcome_is_two_looks_and_then_an_answer(self, client_class):
        """Look, look once more, then stop -- and say what happened."""
        client_class.return_value.list_transactions.side_effect = FlutterwaveUnknownError(
            "Flutterwave request outcome is unknown."
        )

        result = self._run()

        self.assertEqual(result["status"], "abandoned_unknown")
        # The provider was asked exactly twice: the first look and the one
        # retry that is allowed. No third, fourth or twentieth attempt.
        self.assertEqual(client_class.return_value.list_transactions.call_count, 2)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, EscrowPayment.Status.FAILED)
        self.assertIn("Nothing has been charged", self.payment.failure_reason)

    @patch("payments.tasks.FlutterwaveClient")
    def test_a_missing_provider_record_ends_the_same_way(self, client_class):
        """Nothing ever appeared on the provider side: same two looks, same answer."""
        client_class.return_value.list_transactions.return_value = {"data": []}

        result = self._run()

        self.assertEqual(result["status"], "abandoned_unknown")
        self.assertEqual(client_class.return_value.list_transactions.call_count, 2)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, EscrowPayment.Status.FAILED)

    @patch("payments.tasks.FlutterwaveClient")
    def test_abandoning_tells_the_customer_in_plain_words(self, client_class):
        """The whole point: the payer learns what happened, not just the logs."""
        client_class.return_value.list_transactions.side_effect = FlutterwaveUnknownError(
            "Flutterwave request outcome is unknown."
        )

        with self.captureOnCommitCallbacks(execute=True):
            self._run()

        notes = InAppNotification.objects.filter(recipient=self.customer_user)
        self.assertTrue(
            notes.exists(),
            "An abandoned link must reach the customer, not just the logs.",
        )
        self.assertTrue(
            any("payment" in note.message.lower() for note in notes),
            [note.message for note in notes],
        )

    @patch("payments.tasks.FlutterwaveClient")
    def test_a_payment_that_already_moved_on_is_left_alone(self, client_class):
        """Abandoning must never overwrite a payment that settled meanwhile."""
        client_class.return_value.list_transactions.side_effect = FlutterwaveUnknownError(
            "Flutterwave request outcome is unknown."
        )
        EscrowPayment.objects.filter(pk=self.payment.pk).update(
            status=EscrowPayment.Status.HELD, provider_transaction_id="2028146660",
        )

        result = self._run()

        self.assertIn(result["status"], {"already_settled", "not_reconciliation_candidate"})
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, EscrowPayment.Status.HELD)
        self.assertEqual(self.payment.failure_reason, "")

    @patch("payments.tasks.verify_flutterwave_payment.delay")
    @patch("payments.tasks.FlutterwaveClient")
    def test_a_record_that_appears_is_still_reconciled(self, client_class, delayed):
        """The happy path is untouched: the link is found and confirmed."""
        client_class.return_value.list_transactions.return_value = {
            "data": [
                {
                    "id": 2028146660,
                    "tx_ref": self.payment.reference,
                    "flw_ref": "FLW-MOCK-REF",
                    "currency": "NGN",
                    "amount": 25150,
                    "status": "successful",
                }
            ]
        }

        with self.captureOnCommitCallbacks(execute=True):
            result = self._run()

        self.assertEqual(result["status"], "reconciled")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.provider_transaction_id, "2028146660")
        self.assertEqual(self.payment.status, EscrowPayment.Status.PAYMENT_PENDING)
        delayed.assert_called_once()
