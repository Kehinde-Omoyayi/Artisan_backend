"""Deterministic role-specific help; never a booking/payment state machine.

Read committed marketplace records. Guidance only points to existing controls;
it cannot accept an offer, collect money, start work or confirm completion.
"""
from django.utils import timezone


def booking_guidance(*, conversation, user, booking=None, offer=None, payment=None, actions=None):
    if conversation.conversation_type != 'SERVICE':
        return None
    customer = conversation.customer_id == getattr(user, 'pk', None)
    artisan = bool(conversation.artisan_id and conversation.artisan.user_id == getattr(user, 'pk', None))
    if not customer and not artisan:
        return None
    role = 'customer' if customer else 'artisan'
    request = conversation.service_request
    available = set(actions if actions is not None else getattr(conversation, '_available_actions', []))
    result = {'role': role, 'request_status': request.status if request else None,
              'booking_status': getattr(booking, 'status', None), 'payment_status': getattr(payment, 'status', None)}

    def show(stage, title, message, steps, action=None, label=None):
        result.update(stage=stage, title=title, message=message, steps=steps,
                      action={'code': action, 'label': label} if action else None)
        return result

    bs = getattr(booking, 'status', None)
    ps = getattr(payment, 'status', None)
    investigation = bs == 'DISPUTED' or ps == 'DISPUTED'
    if booking and request and not investigation:
        from .models import MediaPreservationHold
        investigation = (MediaPreservationHold.objects.filter(service_request=request,active=True,kind__in=['CHARGEBACK','DISPUTE']).exists())
    if investigation:
        return show('SUPPORT_REVIEW','Follow the support review',
                    'An investigation is active. Follow the linked support case; do not use chat messages as proof of payment or a resolved dispute.',
                    ['Add relevant evidence to the support case.', 'Wait for the recorded support/payment outcome before taking financial next steps.'], 'SUPPORT','Find support options')
    if bs in ('CANCELLED','REJECTED'):
        return show('BOOKING_CLOSED','This booking is not going ahead',
                    'Do not start work or make another payment for this booking. Check its recorded payment/refund status separately.',
                    ['Use support for outstanding payments or refunds.', 'Start a new request if more work is needed.'], 'SUPPORT','Find support options')
    if bs == 'COMPLETED':
        pending = request and request.status != 'COMPLETED'
        return show('COMPLETION_RECONCILIATION' if pending else 'COMPLETED',
                    'Check the remaining completion records' if pending else 'Your service is marked complete',
                    'The booking is completed, but the request is not yet marked completed. Contact support; do not assume recording deletion has happened.' if pending else 'Review the job and recorded payment outcome. Booking completion does not itself prove payout or refund completion.',
                    ['Leave a review through the booking page if available.', 'For a remaining issue, contact support. Text, photos and transaction history remain.'], 'BOOKING','Find booking details')
    if ps in ('FAILED','REFUND_PENDING','REFUNDED'):
        return show('PAYMENT_REVIEW','Check the payment outcome first',
                    'The recorded payment state needs attention. Do not start or continue on an assumption that payment is confirmed.',
                    ['Use an explicit retry control only if the marketplace offers it.', 'For pending or completed refunds, check the transaction and contact support if needed.'], 'SUPPORT','Find payment and support options')
    if bs == 'REQUESTED':
        return show('BOOKING_REQUESTED','Review the booking request',
                    'Review the requested scope, price and schedule in the booking details before confirming availability.' if artisan else 'The artisan still needs to review this booking request. A requested booking is not confirmation that work has started.',
                    ['Use the existing booking accept/reject controls, not a chat promise.' if artisan else 'Keep clarifications in this conversation.', 'Wait for the recorded booking/payment steps before work begins.'], 'BOOKING','Find booking details')
    if bs == 'ACCEPTED':
        if ps == 'HELD':
            return show('READY_TO_START','Confirm arrangements, then start the service' if artisan else 'Arrange access and wait for the artisan to start',
                        'The backend records payment as HELD. Confirm access and the agreed schedule; start work only through the booking control.' if artisan else 'The backend records payment as HELD. Keep access arrangements here; the artisan records the actual service start.',
                        ['Use Start service when the work actually begins.' if artisan else 'Do not confirm completion before the work is delivered.', 'Extra work needs a separate agreed add-on; the accepted quote stays unchanged.'],
                        'START_BOOKING' if artisan and 'START_BOOKING' in available else 'BOOKING', 'Find Start service' if artisan and 'START_BOOKING' in available else 'Find booking details')
        return show('PAYMENT_PENDING','Complete the marketplace payment step' if customer else 'Wait for backend payment confirmation',
                    'Use the marketplace checkout and wait for its recorded confirmation. A receipt screenshot or message in chat is not payment verification.' if customer else 'Do not start work because someone says they have paid. Wait until the marketplace records confirmed payment and enables Start service.',
                    ['Check the agreed total and payment terms before paying.' if customer else 'Confirm schedule/access without starting prematurely.', 'If confirmation is delayed, check payment details or contact support rather than paying twice.'],
                    'PAY' if customer and 'PAY' in available else 'BOOKING','Find secure payment' if customer and 'PAY' in available else 'Find booking details')
    if bs == 'COMPLETION_REQUESTED':
        return show('CHECK_COMPLETION','Inspect the work before confirming' if customer else 'Wait for the customer to inspect the work',
                    'Test the agreed work. Confirm only if it is finished; raise an issue with support before confirming if something is wrong.' if customer else 'The completion request is recorded. Answer questions and provide relevant updates; only the customer can confirm their acceptance.',
                    ['Confirmation is a separate action, not inferred from a thank-you message.', 'Audio/video is eligible for deletion only under the request-and-booking completion policy, subject to holds. Text/photos remain.'],
                    'CONFIRM_COMPLETION' if customer and 'CONFIRM_COMPLETION' in available else 'BOOKING', 'Find confirmation controls' if customer and 'CONFIRM_COMPLETION' in available else 'Find booking details')
    if bs == 'IN_PROGRESS':
        can_complete = artisan and 'SUBMIT_COMPLETION' in available
        return show('WORK_IN_PROGRESS','Share progress and request completion when ready' if artisan else 'Follow progress and check the agreed scope',
                    'Share updates/photos. When the agreed work is finished, submit a completion request; do not confirm on the customer’s behalf.' if artisan else 'Ask questions here, keep the scope clear and inspect the delivered work before any completion confirmation.',
                    ['Resolve outstanding add-on agreements/payments if completion controls are unavailable.', 'Do not replace accepted terms with an informal extra charge in chat.'],
                    'SUBMIT_COMPLETION' if can_complete else 'BOOKING', 'Find completion request' if can_complete else 'Find booking controls')
    if request and request.status in ('CLOSED','ARCHIVED'):
        return show('REQUEST_CLOSED','This request is closed or archived',
                    'Review the history or start a new request for new work. No booking or payment is created by continuing this conversation.',
                    ['Only explicit unbooked closure starts the seven-day recording period.', 'A hold may preserve evidence; ordinary text/photos remain.'])
    if not conversation.request_submitted:
        draft = conversation.draft if isinstance(conversation.draft,dict) else {}
        ready = all(draft.get(k) for k in ('service_id','description','location_id','preferred_date'))
        return show('DRAFT_REVIEW' if ready else 'DRAFT_DETAILS',
                    'Review your draft, then choose Send Request' if customer and ready else 'Describe the job and add the missing details' if customer else 'Help the customer clarify their draft',
                    'Chat, photos and voice notes can explain the job. The customer must review the summary and explicitly choose Send Request; neither chat nor an optional budget submits it.',
                    ['Check service, description, location and preferred date.' if customer else 'Ask about scope, location and access; wait for explicit submission.', 'A preferred date is not a confirmed appointment.'], 'EDIT_REQUEST' if customer else None, 'Review request details' if customer else None)
    if offer and offer.expires_at > timezone.now() and offer.status in ('PROPOSED','REVISION_REQUESTED'):
        if offer.status == 'REVISION_REQUESTED':
            return show('QUOTE_REVISION','Prepare a revised formal quote' if artisan else 'Wait for the revised quote',
                        'A revision was requested. The artisan must send a new version; no changed amount or term is accepted just because it was discussed in chat.',
                        ['Keep the previous quote in the history.', 'Review the new total, scope, timing and terms before accepting.'],
                        'REVISE_OFFER' if artisan and 'REVISE_OFFER' in available else 'REVIEW_OFFER','Find revised quote form' if artisan else 'Find quote details')
        artisan_quote = conversation.artisan_id and offer.created_by_id == conversation.artisan.user_id
        return show('QUOTE_REVIEW','Review the formal quote' if customer and artisan_quote else 'Review the customer’s counter-offer' if artisan and not artisan_quote else 'Wait for the other party to review',
                    'Check scope, labour, materials, transport, extra charges, total, schedule, exclusions, payment and workmanship terms. Ask questions or request a revision before accepting.',
                    ['Use the formal Accept/Decline/Revision controls, not a message saying “agreed”.', 'Accepting a quote creates the next booking/payment step; it is not payment confirmation.'],
                    'REVIEW_OFFER','Find quote controls')
    return show('PREPARE_QUOTE' if artisan else 'WAITING_FOR_QUOTE',
                'Prepare a clear formal quote' if artisan else 'Clarify the job while waiting for a quote',
                'Review the submitted request, confirm coverage/availability and prepare the complete formal quote. A chat estimate is not an accepted booking.' if artisan else 'The request is submitted. Answer questions here and wait for the artisan’s formal quote; do not pay an informal chat estimate.',
                ['Disclose all charges, scope, timing and terms.' if artisan else 'Check that the artisan understands the location, scope and schedule.', 'Booking and payment each require their own recorded step.'],
                'SEND_OFFER' if artisan and 'SEND_OFFER' in available else None,'Find formal quote form' if artisan and 'SEND_OFFER' in available else None)


EVENT_GUIDANCE = {
    'REQUEST_CLOSED': 'This request was explicitly closed. For an unbooked request, audio/video expires seven days after closure unless held; text and ordinary photos remain. New work needs a new request.',
    'BOOKING_CANCELLED': 'Do not start work or pay again on this cancelled booking. Check payment/refund records separately and contact support for an outstanding issue.',
    'REFUND_PENDING': 'Wait for the recorded refund outcome. A refund request or chat promise is not a completed refund.',
    'REFUND_FAILED': 'The refund needs review. Check the payment record and contact support; do not assume money has been returned.',
    'REFUNDED': 'Review the transaction record for the refund outcome. Start a new request if new work is needed.',

    'CONVERSATION_STARTED': 'Customer: describe the job, review the draft and choose Send Request. Artisan: clarify the job; wait for submission before quoting. Chat alone does not book or pay.',
    'REQUEST_SUBMITTED': 'Artisan: review the job and send a full formal quote. Customer: answer questions and review the quote before accepting. Do not pay a chat estimate.',
    'ARTISAN_RESPONDED': 'Customer and artisan: clarify scope, access and availability here. Artisan: prepare a formal quote; the room assignment is not a confirmed booking.',
    'OFFER_CREATED': 'Customer: review all terms, then explicitly accept, decline or request revision. Artisan: answer questions. Quote acceptance is not payment confirmation.',
    'OFFER_COUNTERED': 'Review the new quote version. Use the formal controls to respond; a chat agreement does not change accepted terms.',
    'QUOTE_REVISION_REQUESTED': 'Artisan: send a revised quote as a new version. Customer: review the revised terms before accepting; no revised amount has been accepted yet.',
    'OFFER_DECLINED': 'Discuss what needs changing. The artisan can prepare another formal quote; do not start work or request payment on the declined quote.',
    'OFFER_EXPIRED': 'The expired quote cannot be accepted. Ask for a current formal quote before proceeding.',
    'BOOKING_CREATED': 'Check booking details and recorded payment status. Do not start work until the backend confirms payment and enables the start control.',
    'PAYMENT_PENDING': 'Customer: use the marketplace checkout and wait for recorded confirmation. Artisan: do not start on the strength of a message or screenshot.',
    'PAYMENT_FAILED': 'Check the recorded payment failure and available retry/support controls. Do not assume payment succeeded or pay again without checking.',
    'PAYMENT_HELD': 'Artisan: confirm access/schedule, then use Start service when work begins. Customer: do not confirm completion until the agreed work is delivered.',
    'PAYMENT_CONFIRMED': 'Check the current payment/booking controls before starting. This event is a backend payment update; participant text is never payment verification.',
    'SERVICE_STARTED': 'Share progress and questions here. Agree extra work through a separate add-on; request completion only when the agreed work is finished.',
    'COMPLETION_REQUESTED': 'Customer: inspect/test the work, then explicitly confirm completion or raise an issue before confirming. Artisan: wait for the customer’s decision.',
    'COMPLETION_ACCEPTED': 'Check the final request, booking and transaction records. Completion does not itself prove payout/refund settlement. Recording retention remains hold-aware.',
    'DISPUTE_OPENED': 'Follow the linked support case and add relevant evidence. Do not treat a chat promise as a resolved dispute or confirmed refund.',
}


def transition_instructions(event_code):
    return EVENT_GUIDANCE.get(event_code)
