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

    def test_every_routed_queue_has_a_dedicated_worker(self):
        """Each queue named in CELERY_TASK_ROUTES is consumed by its own worker.

        A single shared worker let analytics, exports, media scanning and
        campaign dispatch occupy the same processes that verify and settle
        payments. The blueprint must hand every routed queue its own consumer.
        """
        routes = settings.CELERY_TASK_ROUTES
        routed_queues = {route["queue"] for route in routes.values()}
        # The default queue carries unrouted work and needs a consumer too.
        expected = routed_queues | {"celery"}

        root = Path(settings.BASE_DIR)
        procfile = (root / "Procfile").read_text(encoding="utf-8")
        render = (root / "render.yaml").read_text(encoding="utf-8")
        for queue in sorted(expected):
            with self.subTest(queue=queue):
                self.assertIn(f"-Q {queue}", procfile)
                self.assertIn(f"-Q {queue}", render)
        # The mail worker is isolated, so a wedged SMTP socket can never block
        # payment initialization, verification or reconciliation.
        self.assertIn("-Q notifications", procfile)
        self.assertIn("worker-notifications: celery -A Artisan_platform worker -l info -Q notifications", procfile)
        self.assertIn("artisan-marketplace-worker-notifications", render)
        self.assertIn("beat: celery -A Artisan_platform beat", procfile)
        self.assertIn("name: artisan-marketplace-beat", render)
        self.assertIn("celery -A Artisan_platform beat -l info", render)

    def test_no_worker_consumes_payment_work_alongside_unrelated_workloads(self):
        root = Path(settings.BASE_DIR)
        for name in ("Procfile", "render.yaml"):
            text = (root / name).read_text(encoding="utf-8")
            worker_lines = [
                line for line in text.splitlines() if "celery -A Artisan_platform worker" in line
            ]
            self.assertTrue(worker_lines, name)
            for line in worker_lines:
                token = line.split("-Q ", 1)[1].split()[0]
                queues = [part for part in token.split(",") if part]
                self.assertEqual(len(queues), 1, f"{name} mixes queues: {line}")
