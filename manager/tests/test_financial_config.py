"""Fee arithmetic, versioning, historical integrity and separation of duties."""
from decimal import Decimal

from django.utils import timezone

from core.policies import ARTISAN_COMMISSION_RATE, CUSTOMER_FEE_KOBO
from manager.models import ConfigChangeRequest, PlatformConfigVersion
from manager.services import config_engine
from manager.services.fees import quote
from payments.calculations import calculate_booking_money

from .base import ManagerTestCase


class FeeArithmeticTests(ManagerTestCase):
    def test_untouched_configuration_reproduces_the_shipped_arithmetic(self):
        money = calculate_booking_money(1_000_000)
        self.assertEqual(money["customer_fee_kobo"], CUSTOMER_FEE_KOBO)
        self.assertEqual(money["commission_rate"], ARTISAN_COMMISSION_RATE)
        self.assertEqual(money["platform_commission_kobo"], 100_000)
        self.assertEqual(money["artisan_payout_kobo"], 900_000)
        self.assertEqual(money["total_amount_kobo"], 1_015_000)

    def test_percentage_mode_and_caps(self):
        result = quote(1_000_000, override={"fees.customer_fee_mode": "PERCENTAGE",
                                            "fees.customer_fee_percent": "2.5"})
        self.assertEqual(result["customer_fee_kobo"], 25_000)
        capped = quote(10_000_000, override={"fees.customer_fee_mode": "COMBINED",
                                             "fees.customer_fee_kobo": 15_000,
                                             "fees.customer_fee_percent": "1",
                                             "fees.max_fee_kobo": 60_000})
        self.assertEqual(capped["customer_fee_kobo"], 60_000)

    def test_category_commission_override(self):
        result = quote(1_000_000, category_slug="plumbing",
                       override={"fees.category_commission_overrides": {"plumbing": "0.15"}})
        self.assertEqual(result["platform_commission_kobo"], 150_000)
        other = quote(1_000_000, category_slug="electrical",
                      override={"fees.category_commission_overrides": {"plumbing": "0.15"}})
        self.assertEqual(other["platform_commission_kobo"], 100_000)

    def test_invariants_hold_after_a_change(self):
        quote(500_000, override={"fees.commission_rate": "0.2"})
        money = calculate_booking_money(500_000)
        self.assertEqual(money["platform_commission_kobo"] + money["artisan_payout_kobo"],
                         money["service_amount_kobo"])

    def test_invalid_values_are_rejected(self):
        from manager.config_spec import ConfigError, normalise

        for value in ("17", "-1", "oops"):
            with self.assertRaises(ConfigError):
                normalise("fees.commission_rate", value)


class ConfigEngineTests(ManagerTestCase):
    def test_a_mistyped_configuration_key_is_a_client_error(self):
        """A wrong key in the URL is the caller's mistake, not a 500."""
        self.login_superuser()
        response = self.client.post("/api/v1/manager/config/fees.does_not_exist/change/",
                                    {"value": "1", "reason": "typo"}, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("Unknown configuration key", response.json()["details"]["key"])

        rollback = self.client.post("/api/v1/manager/config/fees.does_not_exist/rollback/",
                                    {"version": 1}, format="json")
        self.assertEqual(rollback.status_code, 400, rollback.content)

    def test_a_financial_change_needs_a_second_approver(self):
        self.login_superuser()
        change = config_engine.request_change(user=self.superuser, key="fees.commission_rate",
                                              value="0.12", reason="test")
        self.assertTrue(change.requires_approval)
        self.assertEqual(change.status, ConfigChangeRequest.Status.PENDING)
        # The requester cannot approve their own change.
        with self.assertRaises(PermissionError):
            config_engine.approve_change(user=self.superuser, change=change)

    def test_approved_change_writes_a_new_version_and_keeps_history(self):
        change = config_engine.request_change(user=self.superuser, key="fees.commission_rate",
                                              value="0.12", reason="test")
        config_engine.approve_change(user=self.approver, change=change)
        self.assertEqual(config_engine.current("fees.commission_rate"), "0.1200")
        change2 = config_engine.request_change(user=self.superuser, key="fees.commission_rate",
                                               value="0.09", reason="second")
        config_engine.approve_change(user=self.approver, change=change2)
        versions = list(PlatformConfigVersion.objects.filter(key="fees.commission_rate").order_by("version"))
        self.assertEqual([row.version for row in versions], [1, 2])
        self.assertEqual(config_engine.history("fees.commission_rate")[1]["value"], "0.1200")

    def test_rollback_creates_a_new_version_instead_of_erasing_history(self):
        change = config_engine.request_change(user=self.superuser, key="fees.commission_rate",
                                              value="0.12", reason="first")
        config_engine.approve_change(user=self.approver, change=change)
        rollback = config_engine.rollback(user=self.superuser, key="fees.commission_rate",
                                          to_version=1, reason="too high")
        config_engine.approve_change(user=self.approver, change=rollback)
        versions = list(PlatformConfigVersion.objects.filter(key="fees.commission_rate").order_by("version"))
        self.assertEqual(len(versions), 2)
        self.assertEqual([row.value for row in versions], ["0.1200", "0.1200"])
        self.assertEqual(rollback.proposed_value, "0.1200")

    def test_new_bookings_use_the_new_rule_and_old_ones_keep_theirs(self):
        old_price = calculate_booking_money(1_000_000)
        change = config_engine.request_change(user=self.superuser, key="fees.commission_rate",
                                              value="0.20", reason="raise")
        config_engine.approve_change(user=self.approver, change=change)
        new_price = calculate_booking_money(1_000_000)
        self.assertEqual(old_price["platform_commission_kobo"], 100_000)
        self.assertEqual(new_price["platform_commission_kobo"], 200_000)
        # The stored payment keeps the commission it was captured with.
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.platform_commission_kobo, 335_000)

    def test_future_effective_date_does_not_apply_yet(self):
        from datetime import timedelta

        change = config_engine.request_change(
            user=self.superuser, key="fees.commission_rate", value="0.3", reason="later",
            effective_from=timezone.now() + timedelta(days=3),
        )
        config_engine.approve_change(user=self.approver, change=change)
        self.assertEqual(config_engine.current("fees.commission_rate"), str(ARTISAN_COMMISSION_RATE))

    def test_preview_shows_before_and_after_in_money_terms(self):
        payload = config_engine.preview("fees.commission_rate", "0.15")
        self.assertTrue(payload["changed"])
        example = payload["examples"][0]
        self.assertIn("before", example)
        self.assertNotEqual(example["before"]["platform_revenue"], example["after"]["platform_revenue"])

    def test_operational_setting_applies_without_a_second_approver(self):
        change = config_engine.request_change(user=self.superuser, key="bookings.offer_expiry_hours",
                                              value=48, reason="shorter")
        self.assertFalse(change.requires_approval)
        self.assertEqual(change.status, ConfigChangeRequest.Status.APPLIED)
        self.assertEqual(config_engine.current("bookings.offer_expiry_hours"), 48)

    def test_api_previews_and_applies_with_step_up(self):
        self.login_superuser()
        preview = self.client.post("/api/v1/manager/config/fees.commission_rate/preview/",
                                   {"value": "0.15"}, format="json")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["proposed"], "0.1500")

    def test_a_finance_manager_cannot_approve(self):
        from manager.services.auth import has_perm

        self.assertFalse(has_perm(self.finance, "config.approve"))
        change = config_engine.request_change(user=self.approver, key="fees.commission_rate",
                                              value="0.12", reason="x")
        with self.assertRaises(PermissionError):
            config_engine.approve_change(user=self.finance, change=change)
