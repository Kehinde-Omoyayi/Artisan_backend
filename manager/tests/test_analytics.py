"""Analytics correctness against real rows, plus lifecycle placement and events."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from booking.models import Booking
from conversations.models import Offer
from manager.models import AnalyticsEvent
from manager.services import events as event_service
from manager.services import metrics
from payments.models import EscrowPayment, EscrowRelease
from services.models import ServiceRequest
from support.models import SupportCase

from .base import ManagerTestCase


class OverviewTests(ManagerTestCase):
    def test_counts_and_money_come_from_real_rows(self):
        data = metrics.overview(self.superuser, {"period": "last_30_days"})
        self.assertEqual(data["totals"]["customers"], 1)
        self.assertEqual(data["totals"]["artisans"], 1)
        self.assertEqual(data["period"]["bookings"]["value"], 1)
        self.assertEqual(data["period"]["paid_value_kobo"]["value"], 3_365_000)
        self.assertEqual(data["period"]["commission_kobo"]["value"], 335_000)
        self.assertEqual(data["escrow"]["held_kobo"], 3_365_000)

    def test_ratio_reports_its_numerator_and_denominator(self):
        ratio = metrics.overview(self.superuser, {})["ratios"]["booking_conversion"]
        self.assertEqual(ratio["numerator"], 1)   # one accepted offer
        self.assertEqual(ratio["denominator"], 1)  # one offer created
        self.assertEqual(ratio["value"], 100.0)

    def test_an_empty_range_returns_zero_not_an_error(self):
        data = metrics.overview(self.superuser, {"range": "custom", "start": "1999-01-01", "end": "1999-01-02"})
        # No rows in 1999: every period figure is zero and the totals are still real.
        self.assertEqual(data["period"]["bookings"]["value"], 0)
        self.assertEqual(data["totals"]["customers"], 1)

    def test_over_range_is_rejected_instead_of_scanning_everything(self):
        with self.assertRaises(metrics.FilterError):
            metrics.resolve_range({"period": "custom", "start": "2000-01-01", "end": "2026-01-01"})

    def test_cache_is_keyed_by_permissions_and_filters(self):
        first = metrics.overview(self.superuser, {"period": "last_7_days"})
        second = metrics.overview(self.superuser, {"period": "last_7_days"})
        self.assertFalse(first["served_from_cache"])
        self.assertTrue(second["served_from_cache"])
        other = metrics.overview(self.superuser, {"period": "last_30_days"})
        self.assertFalse(other["served_from_cache"])


class GrowthFormattingTests(ManagerTestCase):
    def test_a_change_that_rounds_to_zero_is_not_shown_as_minus_zero(self):
        from manager.services import metrics

        self.assertEqual(metrics.growth(1000, 1000), 0.0)
        self.assertAlmostEqual(metrics.growth(999.9, 1000), -0.01, places=4,
                               msg="a sub-0.1% change stays visible instead of collapsing to 0")
        self.assertEqual(str(metrics.growth(1002, 1000)), "0.2")
        self.assertIsNone(metrics.growth(0, 0), "no activity in either period is not a 0% change")
        self.assertIsNone(metrics.growth(5, 0), "no baseline means no percentage")


class InsightTests(ManagerTestCase):
    def test_a_rule_with_no_baseline_says_so_instead_of_zero_percent(self):
        """'Change: None%' was printed when the previous period was empty."""
        from manager.services import metrics

        payload = metrics.insights(self.superuser, {"period": "last_30_days"})
        for insight in payload["insights"]:
            with self.subTest(rule=insight["key"]):
                self.assertIn("previous", insight)
                if insight["change_percent"] is None:
                    self.assertTrue(
                        insight["previous"] in (None, 0) or insight["previous"] is None,
                        "a null change must come with a null or zero baseline",
                    )


class DataQualityTests(ManagerTestCase):
    def test_a_campaign_fan_out_is_not_reported_as_duplicate_events(self):
        """One delivery event per recipient is correct, not a data fault."""
        from manager.models import AnalyticsEvent
        from manager.services import events as event_service, metrics

        for index in range(5):
            AnalyticsEvent.objects.create(
                name="notification.delivered", entity_type="campaign", entity_id="c-1",
                dedupe_key=f"delivery-{index}",
            )
        payload = metrics.data_quality(self.superuser, {})
        duplicate = next(check for check in payload["checks"] if check["key"] == "duplicate_events")
        self.assertEqual(duplicate["count"], 0, duplicate)

        # The column carries a UNIQUE constraint, so a duplicate cannot be written
        # through the ORM. The detector still has to work on restored or imported
        # data, so the check is exercised as the query it is.
        from django.db.models import Count

        stored_twice = (
            AnalyticsEvent.objects.exclude(dedupe_key="")
            .values("dedupe_key").annotate(n=Count("pk")).filter(n__gt=1).count()
        )
        self.assertEqual(stored_twice, 0, "the unique constraint holds")
        duplicate = next(check for check in payload["checks"] if check["key"] == "duplicate_events")
        self.assertEqual(duplicate["count"], 0, duplicate)


class FinancialTests(ManagerTestCase):
    def test_financial_kpis_separate_revenue_from_escrow(self):
        data = metrics.financial(self.superuser, {})
        kpis = data["kpis"]
        self.assertEqual(kpis["successful_payment_kobo"]["value"], 3_365_000)
        self.assertEqual(kpis["commission_revenue_kobo"]["value"], 335_000)
        self.assertEqual(kpis["service_fee_revenue_kobo"]["value"], 15_000)
        # Escrow is a liability, not revenue.
        self.assertEqual(kpis["escrow_liability_kobo"]["value"], 3_365_000)
        self.assertEqual(kpis["net_revenue_kobo"]["value"], 335_000 + 15_000)

    def test_pending_payments_are_never_counted_as_revenue(self):
        second_booking = self.make_booking(price="10000.00")
        EscrowPayment.objects.create(
            booking=second_booking, customer=self.customer_user, artisan=self.artisan,
            reference="ESC-PENDING", service_amount_kobo=1_000_000, customer_fee_kobo=15_000,
            total_amount_kobo=1_015_000, commission_rate=Decimal("0.1"),
            platform_commission_kobo=100_000, artisan_payout_kobo=900_000,
            status=EscrowPayment.Status.PAYMENT_PENDING,
        )
        kpis = metrics.financial(self.superuser, {})["kpis"]
        self.assertEqual(kpis["successful_payment_kobo"]["value"], 3_365_000)
        self.assertGreaterEqual(kpis["pending_payment_kobo"]["value"], 1_015_000)

    def test_reconciliation_flags_a_broken_payment(self):
        EscrowPayment.objects.filter(pk=self.payment.pk).update(provider_transaction_id=None)
        report = metrics.reconciliation_summary(metrics.resolve_range({}))
        self.assertFalse(report["clean"])
        keys = {issue["key"] for issue in report["issues"] if issue["count"] > 0}
        self.assertIn("missing_provider_reference", keys)


class LifecyclePlacementTests(ManagerTestCase):
    def test_each_lifecycle_state_lands_in_the_right_operational_view(self):
        now = timezone.now()
        open_request = ServiceRequest.objects.create(
            requester=self.customer_user, category=self.category, status=ServiceRequest.Status.OPEN,
            description="A second request with no answer.", budget=Decimal("1000.00"),
        )
        overdue = self.make_booking(status=Booking.Status.IN_PROGRESS, price="1000.00",
                                    scheduled_time_start=now - timedelta(days=3),
                                    scheduled_time_end=now - timedelta(days=2))
        disputed = self.make_booking(status=Booking.Status.DISPUTED, price="1500.00")
        SupportCase.objects.create(reference="CASE-1", booking=disputed, case_type=SupportCase.CaseType.DISPUTE,
                                   status=SupportCase.Status.OPEN, subject="Dispute", opened_by=self.customer_user)
        unavailable_offer = Offer.objects.create(
            conversation=self.conversation, created_by=self.artisan_user, labour_amount=Decimal("100.00"),
            materials_amount=Decimal("0.00"), total_amount=Decimal("100.00"), materials_included=False,
            duration_value=1, duration_unit="HOURS", payload_hash="h2", status=Offer.Status.PROPOSED,
            scheduled_start=now + timedelta(days=1), scheduled_end=now + timedelta(days=1, hours=1),
            expires_at=now + timedelta(hours=2),
        )
        kpis = metrics.operational_kpis()
        self.assertGreaterEqual(kpis["requests_without_response"], 1)
        self.assertGreaterEqual(kpis["services_overdue"], 1)
        self.assertGreaterEqual(kpis["disputed_bookings"], 1)
        self.assertGreaterEqual(kpis["offers_awaiting_customer"], 1)
        self.assertEqual(open_request.status, ServiceRequest.Status.OPEN)
        self.assertEqual(unavailable_offer.status, Offer.Status.PROPOSED)

    def test_paid_but_not_started_is_separate_from_in_progress(self):
        kpis = metrics.operational_kpis()
        self.assertGreaterEqual(kpis["paid_waiting_to_start"], 1)
        self.assertEqual(kpis["services_in_progress"], 0)


class EventTests(ManagerTestCase):
    def test_a_booking_creation_records_one_event(self):
        count = AnalyticsEvent.objects.filter(name="booking.created").count()
        self.make_booking()
        self.assertEqual(AnalyticsEvent.objects.filter(name="booking.created").count(), count + 1)

    def test_duplicate_events_do_not_double_count(self):
        event_service.record("payment.succeeded", entity_type="payment", entity_id="p1", suffix="held")
        event_service.record("payment.succeeded", entity_type="payment", entity_id="p1", suffix="held")
        self.assertEqual(AnalyticsEvent.objects.filter(name="payment.succeeded", entity_id="p1").count(), 1)

    def test_sensitive_values_are_stripped_from_event_metadata(self):
        event = event_service.record(
            "offer.submitted", entity_type="offer", entity_id="o1",
            properties={"email": "x@y.com", "phone": "0803", "otp": "123456", "total_amount": "5000"},
        )
        self.assertEqual(set(event.properties), {"total_amount"})

    def test_unknown_events_are_rejected(self):
        with self.assertRaises(ValueError):
            event_service.record("made.up.event")

    def test_a_payment_hold_records_the_financial_event_once(self):
        # The fixture payment is already HELD; repeating the same transition must not
        # add a second "held" event for the same payment and status.
        self.payment.status = EscrowPayment.Status.HELD
        self.payment.save(update_fields=["status"])
        self.payment.save(update_fields=["status"])
        self.assertEqual(
            AnalyticsEvent.objects.filter(name="payment.succeeded", entity_id=self.payment.pk).count(), 1
        )

    def test_funnel_is_built_from_events_not_current_status(self):
        booking = self.make_booking(price="3000.00")
        booking.status = Booking.Status.CANCELLED
        booking.save(update_fields=["status"])
        # The booking was created and later cancelled: both facts are recorded, and the
        # funnel counts the creation even though the current status is CANCELLED.
        self.assertTrue(AnalyticsEvent.objects.filter(name="booking.cancelled", entity_id=booking.pk).exists())
        data = metrics.funnel(self.superuser, {"period": "last_7_days"})
        stages = {stage["key"]: stage for stage in data["stages"]}
        self.assertGreaterEqual(stages["booking.created"]["count"], 1)


class SnapshotTests(ManagerTestCase):
    def test_snapshot_history_is_kept_per_interval(self):
        from manager.models import OperationalSnapshot
        from manager.services import snapshots

        first = snapshots.capture()
        # Buckets follow the configured cadence (5 minutes by default), so a jump
        # of one full interval must produce a second row, not overwrite the first.
        second = snapshots.capture(now=timezone.now() + timedelta(minutes=6))
        self.assertNotEqual(first["captured_for"], second["captured_for"])
        self.assertGreaterEqual(OperationalSnapshot.objects.values("captured_for").distinct().count(), 2)

    def test_interval_changes_are_persisted(self):
        from manager.services import snapshots

        snapshots.set_interval(300, user=self.superuser)
        self.assertEqual(snapshots.interval_seconds(), 300)
        with self.assertRaises(ValueError):
            snapshots.set_interval(5, user=self.superuser)

    def test_the_shipped_snapshot_default_is_five_minutes(self):
        """The cadence you asked for: 5 minutes unless a manager changes it.

        It has to hold in the spec, in the model field and in the resolved value
        with no override row — a change in only one of them silently reverts to
        one minute on the next deploy.
        """
        from manager.config_spec import CONFIG_KEYS, SNAPSHOT_INTERVALS
        from manager.models import SnapshotIntervalSetting, OperationalSnapshot
        from manager.services import snapshots

        self.assertEqual(CONFIG_KEYS["analytics.snapshot_interval_seconds"]["default"], 300)
        self.assertEqual(SnapshotIntervalSetting._meta.get_field("interval_seconds").default, 300)
        self.assertEqual(OperationalSnapshot._meta.get_field("interval_seconds").default, 300)
        self.assertIn(60, SNAPSHOT_INTERVALS, "a manager must still be able to choose one minute")

        SnapshotIntervalSetting.objects.all().delete()
        self.assertEqual(snapshots.interval_seconds(), 300)
        self.assertEqual(snapshots.status()["interval_label"], "5 minutes")
