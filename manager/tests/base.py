"""Shared fixtures: real users, real rows, real API calls (no mocks of business logic)."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from booking.models import Booking
from conversations.models import Conversation, Offer
from home.models import ArtisanProfile, CustomerProfile, ServiceCategory
from manager.models import ManagerProfile, ManagerRole, PlatformConfigVersion
from manager.services import auth as auth_service
from manager.services import config_engine
from payments.models import EscrowPayment
from services.models import ServiceRequest

User = get_user_model()


class ManagerTestCase(APITestCase):
    def setUp(self):
        from django.core.cache import cache

        super().setUp()
        cache.clear()
        config_engine.invalidate_cache()
        PlatformConfigVersion.objects.all().delete()
        self.superuser = User.objects.create_user(
            email="owner@example.com", password="Passw0rd!234", first_name="Owner", last_name="Manager",
            is_staff=True, is_superuser=True, is_active=True,
        )
        self.role_finance = ManagerRole.objects.get_or_create(
            slug="finance-manager",
            defaults={"name": "Finance Manager", "is_system": True,
                      "permissions": ["analytics.view", "analytics.financial", "config.view", "config.finance"]},
        )[0]
        self.finance = User.objects.create_user(
            email="finance@example.com", password="Passw0rd!234", first_name="Femi", last_name="Finance",
            is_staff=True, is_active=True,
        )
        ManagerProfile.objects.create(user=self.finance, role=self.role_finance)

        from manager.permissions import PERMISSIONS

        self.role_super = ManagerRole.objects.get_or_create(
            slug="super-admin",
            defaults={"name": "Super Administrator", "is_system": True, "permissions": sorted(PERMISSIONS)},
        )[0]
        self.approver = User.objects.create_user(
            email="approver@example.com", password="Passw0rd!234", first_name="Ada", last_name="Approver",
            is_staff=True, is_active=True,
        )
        ManagerProfile.objects.create(user=self.approver, role=self.role_super)

        self.customer_user = User.objects.create_user(
            email="customer@example.com", password="Passw0rd!234", first_name="Chi", last_name="Customer",
            is_active=True,
        )
        self.customer = CustomerProfile.objects.create(user=self.customer_user)

        self.artisan_user = User.objects.create_user(
            email="artisan@example.com", password="Passw0rd!234", first_name="Ade", last_name="Artisan",
            is_active=True,
        )
        self.artisan = ArtisanProfile.objects.create(
            user=self.artisan_user, business_name="Ade Plumbing", profile_status="ACTIVE", state="Lagos",
        )
        self.category = ServiceCategory.objects.create(name="Plumbing")
        self.request = ServiceRequest.objects.create(
            requester=self.customer_user, category=self.category, status=ServiceRequest.Status.OPEN,
            description="Two sockets keep tripping.", budget=Decimal("50000.00"),
        )
        self.conversation = Conversation.objects.create(
            conversation_type="SERVICE", customer=self.customer_user, artisan=self.artisan,
            service_request=self.request,
        )
        self.offer = Offer.objects.create(
            conversation=self.conversation, created_by=self.artisan_user,
            labour_amount=Decimal("30000.00"), materials_amount=Decimal("3500.00"),
            total_amount=Decimal("33500.00"), materials_included=False,
            duration_value=2, duration_unit="HOURS", payload_hash="hash-test",
            status=Offer.Status.ACCEPTED, scheduled_start=timezone.now() + timedelta(days=2),
            scheduled_end=timezone.now() + timedelta(days=2, hours=2),
            expires_at=timezone.now() + timedelta(days=3),
        )
        self.booking = Booking.objects.create(
            request=self.request, artisan=self.artisan, customer=self.customer,
            scheduled_time_start=timezone.now() + timedelta(days=2),
            scheduled_time_end=timezone.now() + timedelta(days=2, hours=2),
            price_agreed=Decimal("33500.00"), status=Booking.Status.ACCEPTED,
        )
        self.payment = EscrowPayment.objects.create(
            booking=self.booking, customer=self.customer_user, artisan=self.artisan,
            reference="ESC-TEST-1", service_amount_kobo=3_350_000, customer_fee_kobo=15_000,
            total_amount_kobo=3_365_000, commission_rate=Decimal("0.1000"),
            platform_commission_kobo=335_000, artisan_payout_kobo=3_015_000,
            status=EscrowPayment.Status.HELD, held_at=timezone.now(),
            provider_transaction_id="12345",
        )

    def make_request(self, status="OPEN", description="Second request"):
        return ServiceRequest.objects.create(
            requester=self.customer_user, category=self.category, status=status,
            description=description, budget=Decimal("20000.00"),
        )

    def make_booking(self, *, request=None, status="REQUESTED", price="2000.00", **kwargs):
        now = timezone.now()
        return Booking.objects.create(
            request=request or self.make_request(),
            artisan=self.artisan,
            customer=self.customer,
            scheduled_time_start=kwargs.pop("scheduled_time_start", now + timedelta(days=4)),
            scheduled_time_end=kwargs.pop("scheduled_time_end", now + timedelta(days=4, hours=2)),
            price_agreed=Decimal(price),
            status=status,
            **kwargs,
        )

    def login(self, user):
        self.client.force_authenticate(user=user)
        return user

    def login_superuser(self):
        return self.login(self.superuser)

    def manager_profile(self, user):
        return auth_service.ensure_profile(user)
