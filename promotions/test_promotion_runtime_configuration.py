from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from Artisan_platform.celery import app


class PromotionTaskProductionWiringTests(SimpleTestCase):
    def test_periodic_reconciliation_is_registered_with_celery_beat(self):
        schedule = app.conf.beat_schedule
        self.assertIn("reconcile-promotion-campaigns-every-minute", schedule)
        self.assertEqual(
            schedule["reconcile-promotion-campaigns-every-minute"]["task"],
            "promotions.tasks.reconcile_promotion_campaigns",
        )
        self.assertIsNotNone(schedule["reconcile-promotion-campaigns-every-minute"]["schedule"])

    def test_promotion_payment_and_reconciliation_tasks_have_consumed_queues(self):
        routes = settings.CELERY_TASK_ROUTES
        self.assertEqual(routes["promotions.tasks.initialize_promotion_payment"]["queue"], "payments")
        self.assertEqual(routes["promotions.tasks.verify_promotion_payment"]["queue"], "payments")
        self.assertEqual(routes["promotions.tasks.reconcile_promotion_payment_initialization"]["queue"], "payments")
        self.assertEqual(routes["promotions.tasks.reconcile_promotion_campaigns"]["queue"], "maintenance")

        root = Path(settings.BASE_DIR)
        procfile = (root / "Procfile").read_text(encoding="utf-8")
        render = (root / "render.yaml").read_text(encoding="utf-8")
        self.assertIn("celery -A Artisan_platform worker", procfile)
        self.assertIn("-Q celery,verification,maintenance,notifications,media,payments", procfile)
        self.assertIn("beat: celery -A Artisan_platform beat", procfile)
        self.assertIn("name: artisan-marketplace-beat", render)
        self.assertIn("celery -A Artisan_platform beat -l info", render)
