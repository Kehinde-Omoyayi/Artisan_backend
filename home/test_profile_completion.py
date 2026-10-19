from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from conversations.models import Conversation, Message
from home.models import (
    ArtisanProfile,
    CustomUser,
    CustomerProfile,
    Locations,
    Service,
    ServiceCategory,
)
from services.models import ArtisanVerification, ServiceRequest


class ProtectedProfileCompletionApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.category = ServiceCategory.objects.create(name="Home Repair")
        self.service = Service.objects.create(
            category=self.category,
            name="Plumbing Repairs",
            description="Repairs and installation.",
            is_active=True,
        )
        self.location = Locations.objects.create(
            country="Nigeria",
            state="Lagos",
            area="Ikeja",
        )
        self.customer = CustomUser.objects.create_user(
            email="incomplete-customer@example.test",
            first_name="Ada",
            last_name="Customer",
            password="StrongPassword123!",
            is_active=True,
        )
        CustomerProfile.objects.create(user=self.customer)
        self.artisan_user = CustomUser.objects.create_user(
            email="incomplete-artisan@example.test",
            first_name="Ayo",
            last_name="Artisan",
            password="StrongPassword123!",
            is_active=True,
        )
        self.artisan = ArtisanProfile.objects.create(
            user=self.artisan_user,
            profile_status="ACTIVE",
        )
        ArtisanVerification.objects.create(
            artisan=self.artisan,
            verification_status="VERIFIED",
            verification_method="NIN",
        )
        self.complete_customer = CustomUser.objects.create_user(
            email="complete-customer@example.test",
            first_name="Nia",
            last_name="Customer",
            phone_number="+2348012345678",
            password="StrongPassword123!",
            is_active=True,
        )
        CustomerProfile.objects.create(
            user=self.complete_customer,
            country="Nigeria",
            state="Lagos",
            area="Yaba",
        )

    def assert_profile_incomplete(self, response, expected_role, expected_fields):
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "PROFILE_INCOMPLETE")
        self.assertIn("complete your", response.data["message"].lower())
        details = response.data["details"]
        self.assertEqual(details["profile_role"], expected_role)
        self.assertTrue(set(expected_fields).issubset(set(details["missing_fields"])))
        self.assertNotIn("request_id", response.data["message"].lower())

    def test_direct_service_request_rejects_incomplete_customer_without_creating_data(self):
        self.client.force_authenticate(self.customer)
        response = self.client.post(
            "/api/v1/services/requests/",
            {
                "service_name": self.service.name,
                "location": {
                    "country": self.location.country,
                    "state": self.location.state,
                    "area": self.location.area,
                },
                "preferred_date": (timezone.localdate() + timedelta(days=2)).isoformat(),
                "urgency": "NORMAL",
                "description": "Please repair a leaking kitchen tap.",
            },
            format="json",
        )
        self.assert_profile_incomplete(
            response,
            "customer",
            {"phone_number", "country", "state", "area"},
        )
        self.assertEqual(ServiceRequest.objects.count(), 0)

    def test_direct_artisan_conversation_rejects_incomplete_customer_before_creation(self):
        self.client.force_authenticate(self.customer)
        response = self.client.post(
            f"/api/v1/conversations/direct/artisan/{self.artisan.pk}/",
            {"initial_message": "Could you help with a leaking tap?"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="profile-incomplete-direct-message",
        )
        self.assert_profile_incomplete(
            response,
            "customer",
            {"phone_number", "country", "state", "area"},
        )
        self.assertEqual(Conversation.objects.count(), 0)
        self.assertEqual(Message.objects.count(), 0)

    def test_artisan_cannot_send_a_message_with_an_incomplete_artisan_profile(self):
        conversation = Conversation.objects.create(
            conversation_type=Conversation.ConversationType.DIRECT,
            direct_key="profile-incomplete-artisan-test",
        )
        conversation.participants.set([self.complete_customer, self.artisan_user])
        self.client.force_authenticate(self.artisan_user)
        response = self.client.post(
            f"/api/v1/conversations/{conversation.pk}/messages/",
            {"body": "I can help with this repair."},
            format="json",
            HTTP_IDEMPOTENCY_KEY="profile-incomplete-artisan-message",
        )
        self.assert_profile_incomplete(
            response,
            "artisan",
            {"phone_number", "country", "state", "area"},
        )
        self.assertEqual(Message.objects.filter(conversation=conversation).count(), 0)

    def test_complete_profile_can_submit_a_service_request(self):
        self.client.force_authenticate(self.complete_customer)
        response = self.client.post(
            "/api/v1/services/requests/",
            {
                "service_name": self.service.name,
                "location": {
                    "country": self.location.country,
                    "state": self.location.state,
                    "area": self.location.area,
                },
                "preferred_date": (timezone.localdate() + timedelta(days=2)).isoformat(),
                "urgency": "NORMAL",
                "description": "Please repair a leaking kitchen tap.",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(ServiceRequest.objects.filter(requester=self.complete_customer).count(), 1)
