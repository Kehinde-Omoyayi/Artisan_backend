"""Service-request creation honours the Idempotency-Key header.

A create retried by the browser (double click, dropped connection, resumed
submission after the profile gate) must record the request once, no matter how
many times the client resends the same key — and a different key must create a
separate request.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from home.models import (
    CustomerProfile,
    CustomUser,
    Locations,
    Service,
    ServiceCategory,
)
from services.models import ServiceRequest


class ServiceRequestCreateIdempotencyTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.category = ServiceCategory.objects.create(name="Electrical")
        self.service = Service.objects.create(
            category=self.category,
            name="Electrical Installation",
            description="Wiring and outlets.",
            is_active=True,
        )
        self.location = Locations.objects.create(
            country="Nigeria",
            state="Lagos",
            area="Ikeja",
        )
        self.customer = CustomUser.objects.create_user(
            email="complete-customer@example.test",
            first_name="Ada",
            last_name="Customer",
            phone_number="+2348012345678",
            password="StrongPassword123!",
            is_active=True,
        )
        CustomerProfile.objects.create(
            user=self.customer,
            country="Nigeria",
            state="Lagos",
            area="Ikeja",
        )
        self.client.force_authenticate(self.customer)
        self.url = "/api/v1/services/requests/"

    def _payload(self, **overrides):
        payload = {
            "service_name": self.service.name,
            "location": {
                "country": self.location.country,
                "state": self.location.state,
                "area": self.location.area,
            },
            "preferred_date": (timezone.localdate() + timedelta(days=2)).isoformat(),
            "urgency": "NORMAL",
            "description": "Please install two wall outlets.",
        }
        payload.update(overrides)
        return payload

    def test_same_key_replay_returns_the_original_request(self):
        key = "service-request-create-testkey-1"
        first = self.client.post(
            self.url, self._payload(), format="json", HTTP_IDEMPOTENCY_KEY=key
        )
        self.assertEqual(first.status_code, 201)
        second = self.client.post(
            self.url, self._payload(), format="json", HTTP_IDEMPOTENCY_KEY=key
        )
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(
            ServiceRequest.objects.filter(requester=self.customer).count(), 1
        )

    def test_different_keys_create_separate_requests(self):
        for index in range(2):
            response = self.client.post(
                self.url,
                self._payload(),
                format="json",
                HTTP_IDEMPOTENCY_KEY=f"service-request-create-testkey-{index}",
            )
            self.assertEqual(response.status_code, 201)
        self.assertEqual(
            ServiceRequest.objects.filter(requester=self.customer).count(), 2
        )

    def test_replay_with_different_content_does_not_create_a_second_row(self):
        key = "service-request-create-testkey-3"
        first = self.client.post(
            self.url, self._payload(), format="json", HTTP_IDEMPOTENCY_KEY=key
        )
        self.assertEqual(first.status_code, 201)
        replay = self.client.post(
            self.url,
            self._payload(description="A changed description."),
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(first.json()["id"], replay.json()["id"])
        self.assertEqual(
            ServiceRequest.objects.filter(requester=self.customer).count(), 1
        )

    def test_missing_key_still_creates_and_records_no_key(self):
        response = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(response.status_code, 201)
        created = ServiceRequest.objects.get(requester=self.customer)
        self.assertIsNone(created.idempotency_key)
