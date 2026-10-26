from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import patch
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from .guidance import booking_guidance, transition_instructions
from .models import Conversation, Message
from .events import record_conversation_event
from .tests import ConversationTestMixin


class GuidanceTests(SimpleTestCase):
    def setUp(self):
        self.customer=NS(pk=1)
        self.artisan=NS(pk=2)
        self.room=NS(conversation_type='SERVICE',customer_id=1,artisan_id=3,
                     artisan=NS(user_id=2),service_request=None,request_submitted=False,
                     draft={},_available_actions=[],_description_confirmed=True)

    def guide(self, user=None, **kwargs):
        return booking_guidance(conversation=self.room,user=user or self.customer,**kwargs)

    def test_direct_and_wrong_role_excluded(self):
        self.assertIsNone(self.guide(NS(pk=99)))
        self.room.conversation_type='DIRECT'
        self.assertIsNone(self.guide())

    def test_draft_has_explicit_submission_and_no_mutation(self):
        self.assertEqual(self.guide()['stage'],'DRAFT_DETAILS')
        self.assertIsNone(self.guide(self.artisan)['action'])
        self.room.draft=dict(service_id=1,description='Fix',location_id=4,preferred_date='2026-11-01')
        self.assertEqual(self.guide()['stage'],'DRAFT_REVIEW')
        self.assertFalse(self.room.request_submitted)

    def test_quote_requires_description_confirmation_first(self):
        self.room.request_submitted=True
        self.room._description_confirmed=False
        self.assertEqual(self.guide()['stage'],'CLARIFY_DESCRIPTION')
        artisan_guide=self.guide(self.artisan,actions=['CONFIRM_DESCRIPTION'])
        self.assertEqual(artisan_guide['stage'],'CLARIFY_DESCRIPTION')
        self.assertEqual(artisan_guide['action']['code'],'CONFIRM_DESCRIPTION')
        self.room._description_confirmed=True
        self.assertEqual(self.guide()['stage'],'WAITING_FOR_QUOTE')

    def test_quote_wait_review_revision_and_expiry(self):
        self.room.request_submitted=True
        self.assertEqual(self.guide()['stage'],'WAITING_FOR_QUOTE')
        self.assertEqual(self.guide(self.artisan,actions=['SEND_OFFER'])['action']['code'],'SEND_OFFER')
        offer=NS(status='PROPOSED',expires_at=timezone.now()+timedelta(days=1),created_by_id=2)
        self.assertEqual(self.guide(offer=offer)['stage'],'QUOTE_REVIEW')
        offer.status='REVISION_REQUESTED'
        self.assertEqual(self.guide(self.artisan,offer=offer,actions=['REVISE_OFFER'])['action']['code'],'REVISE_OFFER')
        offer.expires_at=timezone.now()-timedelta(seconds=1)
        self.assertEqual(self.guide(offer=offer)['stage'],'WAITING_FOR_QUOTE')

    def test_booking_payment_and_work_role_permissions(self):
        booking=NS(status='ACCEPTED')
        payment=NS(status='PAYMENT_PENDING')
        self.assertEqual(self.guide(booking=booking,payment=payment,actions=['PAY'])['action']['code'],'PAY')
        self.assertNotEqual(self.guide(self.artisan,booking=booking,payment=payment,actions=['PAY'])['action']['code'],'PAY')
        payment.status='HELD'
        self.assertEqual(self.guide(self.artisan,booking=booking,payment=payment,actions=['START_BOOKING'])['action']['code'],'START_BOOKING')
        self.assertNotEqual(self.guide(booking=booking,payment=payment,actions=['START_BOOKING'])['action']['code'],'START_BOOKING')
        booking.status='IN_PROGRESS'
        self.assertEqual(self.guide(self.artisan,booking=booking,payment=payment,actions=['SUBMIT_COMPLETION'])['action']['code'],'SUBMIT_COMPLETION')
        self.assertEqual(self.guide(self.artisan,booking=booking,payment=payment)['action']['code'],'BOOKING')
        booking.status='COMPLETION_REQUESTED'
        self.assertEqual(self.guide(booking=booking,payment=payment,actions=['CONFIRM_COMPLETION'])['action']['code'],'CONFIRM_COMPLETION')
        self.assertEqual(self.guide(self.artisan,booking=booking,payment=payment,actions=['CONFIRM_COMPLETION'])['action']['code'],'BOOKING')
        booking.status='COMPLETED'
        self.assertEqual(self.guide(booking=booking,payment=payment)['stage'],'COMPLETED')
        self.assertEqual(payment.status,'HELD')

    def test_failure_dispute_cancellation_and_closed_priority(self):
        booking=NS(status='ACCEPTED');payment=NS(status='FAILED')
        self.assertEqual(self.guide(booking=booking,payment=payment)['stage'],'PAYMENT_REVIEW')
        booking.status='DISPUTED'
        self.assertEqual(self.guide(booking=booking,payment=payment)['stage'],'SUPPORT_REVIEW')
        booking.status='CANCELLED';payment.status='HELD'
        self.assertEqual(self.guide(booking=booking,payment=payment)['stage'],'BOOKING_CLOSED')
        self.room.service_request=NS(status='CLOSED')
        self.assertEqual(self.guide()['stage'],'REQUEST_CLOSED')

    def test_only_selected_events_get_prompts(self):
        self.assertIsNotNone(transition_instructions('PAYMENT_HELD'))
        self.assertIsNone(transition_instructions('SERVICE_PROGRESS_UPDATED'))


class GuidanceIntegrationTests(TestCase, ConversationTestMixin):
    def setUp(self):
        self.customer=self.make_user('guide-customer@example.com')
        self.artisan_user,self.artisan=self.make_artisan('guide-artisan@example.com')
        self.room=self.make_conversation()

    def test_idempotent_event_preserves_body_and_no_poll_message_spam(self):
        with patch('conversations.notifications.notify_room_event') as notify:
            first=record_conversation_event(conversation=self.room,event_code='REQUEST_SUBMITTED',event_key='guide:submitted',body='Original commercial details')
            second=record_conversation_event(conversation=self.room,event_code='REQUEST_SUBMITTED',event_key='guide:submitted',body='Different retry')
            self.assertEqual(first.pk,second.pk)
            self.assertTrue(first.body.startswith('Original commercial details'))
            self.assertIn('Next steps:',first.body)
            self.assertEqual(notify.call_count,1)
        client=APIClient();client.force_authenticate(self.customer)
        before=Message.objects.count()
        for _ in range(2):
            response=client.get(f'/api/v1/conversations/{self.room.id}/messages/')
            self.assertEqual(response.status_code,200,response.data)
            self.assertEqual(response.data['guidance']['role'],'customer')
            self.assertIn('results',response.data)
        self.assertEqual(Message.objects.count(),before)
        client.force_authenticate(self.artisan_user)
        response=client.get(f'/api/v1/conversations/{self.room.id}/messages/')
        self.assertEqual(response.data['guidance']['role'],'artisan')

    def test_active_hold_overrides_ready_to_start(self):
        from .models import MediaPreservationHold
        MediaPreservationHold.objects.create(service_request=self.room.service_request,kind='CHARGEBACK',active=True)
        result=booking_guidance(conversation=self.room,user=self.artisan_user,booking=NS(status='ACCEPTED'),payment=NS(status='HELD'),actions=['START_BOOKING'])
        self.assertEqual(result['stage'],'SUPPORT_REVIEW')
