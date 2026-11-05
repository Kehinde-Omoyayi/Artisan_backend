from uuid import uuid4

from django.conf import settings
from django.db import transaction

from booking.models import booking_display_name

from .constants import NotificationEvent
from .tasks import fanout_service_request
from core.services import enqueue_outbox
import logging
from core.tasks import dispatch_outbox_events
from .in_app import create_in_app_notification
from .links import absolute_url, booking_link, conversation_link, dashboard_link, cancellation_risk_link, payment_link, promotion_application_link, promotion_center_link, service_request_link, support_case_link, verification_link

logger = logging.getLogger(__name__)

def _safe_dispatch_outbox():
    try: dispatch_outbox_events.delay()
    except Exception:
        logger.exception("notification_outbox_dispatch_enqueue_failed")

def schedule_email(*, event_key, recipient, subject, message, action_url=None, action_label=None, attachments=None, popup=None, extra_data=None):
    """Create the in-app notification and the durable email outbox event for one recipient.

    The rows are written in the caller's transaction, so they commit atomically with the business change
    and vanish with it on rollback. Only the Celery hand-off waits for the commit, and it never raises.
    A notification failure is logged and must never break the business operation that caused it.
    """
    if not recipient: return
    label = action_label or ("Open in Artisan Marketplace" if action_url else None)
    try:
        with transaction.atomic():
            data = {"action_url": action_url, "action_label": label}
            if popup:
                # "modal" is a decision the recipient must make; "toast" is a
                # plain confirmation. Both are durable in-app notifications and
                # stay in the notification list after being acknowledged.
                data["popup"] = popup
            if extra_data:
                data.update(extra_data)
            create_in_app_notification(user=recipient, event_key=event_key, title=subject, message=message, data=data)
            if recipient.email:
                payload = {"event_key": event_key, "recipient_email": recipient.email, "recipient_name": recipient.first_name or recipient.email, "subject": subject, "message": message, "action_url": absolute_url(action_url), "action_label": label}
                if attachments:
                    payload["attachments"] = attachments
                enqueue_outbox(event_type="notification.email", event_key=event_key, payload=payload)
    except Exception:
        logger.exception("notification_write_failed event_key=%s", event_key)
        return
    transaction.on_commit(_safe_dispatch_outbox, robust=True)

def _request_label(service_request):
    """Name the work for the customer, even when they simply described it in chat.

    A request sent from a conversation does not have to name a catalogue service
    or category -- the customer says what needs doing and the artisan itemises it
    in the offer -- so nothing here may assume either one exists.
    """
    category = getattr(service_request, "category", None)
    service = getattr(service_request, "service", None)
    for candidate in (category, service):
        name = getattr(candidate, "name", "") or getattr(candidate, "service_name", "")
        if str(name or "").strip():
            return str(name).strip()
    return ""


def _request_has_catalogue(service_request):
    """Only a request that names a category can be fanned out to matching artisans."""
    return bool(getattr(service_request, "category_id", None))


def _queue_service_request_fanout(service_request, variant):
    """Ask every matching artisan in the request's category and area to look again.

    `variant` keeps the outbox key distinct per lifecycle event, so an update or a
    withdrawal is never swallowed as a duplicate of the original "new request" fan-out.
    """
    if not _request_has_catalogue(service_request):
        # A chat request lives in one conversation with one artisan; there is no
        # category to match against, so there is nobody else to notify.
        return
    revision = uuid4().hex
    variant = f"{variant}:{revision}"
    try:
        with transaction.atomic():
            enqueue_outbox(
                event_type="automation.fanout_service_request",
                event_key=f"service-demand:{service_request.id}:fanout:{variant}",
                payload={"service_request_id": str(service_request.id), "variant": variant},
            )
    except Exception:
        logger.exception(
            "service_request_fanout_enqueue_failed request=%s variant=%s",
            service_request.id,
            variant,
        )
        return
    transaction.on_commit(_safe_dispatch_outbox, robust=True)


def notify_service_request_created(service_request):
    customer = service_request.requester
    label = _request_label(service_request)
    subject_line = f"Your {label} service request" if label else "Your service request"

    schedule_email(
        event_key=f"{NotificationEvent.SERVICE_REQUEST_CREATED}:{service_request.id}:customer",
        recipient=customer,
        subject=f"{subject_line} has been received",
        message=f"{subject_line} has been received successfully. We are now looking for suitable artisans.",
        action_url=service_request_link(service_request),
    )

    _queue_service_request_fanout(service_request, "created")


def notify_service_request_updated(service_request):
    """The customer changed a live request: tell the same artisans who saw it first."""
    label = _request_label(service_request)
    customer = service_request.requester
    subject_line = f"Your {label} request" if label else "Your request"
    if _request_has_catalogue(service_request):
        detail = "Artisans serving this category and area have been notified of the change."
    else:
        detail = "The artisan in your conversation can see the change."

    schedule_email(
        event_key=f"{NotificationEvent.SERVICE_REQUEST_UPDATED}:{service_request.id}:{uuid4().hex}:customer",
        recipient=customer,
        subject=f"{subject_line} has been updated",
        message=f"{subject_line} has been updated. {detail}",
        action_url=service_request_link(service_request),
    )

    _queue_service_request_fanout(service_request, "updated")


def notify_service_request_withdrawn(service_request):
    """The customer deleted a request: the record is archived, artisans are told to stop."""
    category_name = service_request.category.name
    customer = service_request.requester

    schedule_email(
        event_key=f"{NotificationEvent.SERVICE_REQUEST_WITHDRAWN}:{service_request.id}:customer",
        recipient=customer,
        subject="Your service request has been withdrawn",
        message=(
            f"Your {category_name} request has been withdrawn and no longer appears in "
            "your workspace. It is kept on record for your history and reporting."
        ),
        action_url=service_request_link(service_request),
    )

    _queue_service_request_fanout(service_request, "withdrawn")

def notify_artisan_service_demand(service_request, artisan_user, revision="created"):
    """Tell one matching artisan that a customer request exists in their category and area (once per artisan)."""
    category_name = service_request.category.name
    schedule_email(
        event_key=f"service-demand:{service_request.id}:artisan:{artisan_user.id}",
        recipient=artisan_user,
        subject=f"New {category_name} request in your service area",
        message=f"A customer has posted a {category_name} request in an area you serve. Keep your profile active and check your conversations: the customer may contact you directly.",
        action_url=dashboard_link(),
        action_label="Open dashboard",
    )


def notify_artisan_service_request_changed(service_request, artisan_user, revision="updated"):
    """An open request this artisan could quote for was edited by the customer."""
    category_name = service_request.category.name
    area = service_request.location.area if service_request.location_id else ""
    where = f" in {area}" if area else ""
    schedule_email(
        event_key=f"service-demand:{service_request.id}:updated:{revision}:artisan:{artisan_user.id}",
        recipient=artisan_user,
        subject=f"{category_name} request updated{where}",
        message=(
            f"A {category_name} request you can serve{where} has been updated by the customer. "
            "Budget, date or description may have changed, so review it before you respond."
        ),
        action_url=dashboard_link(),
        action_label="Open dashboard",
    )


def notify_artisan_service_request_withdrawn(service_request, artisan_user, revision="withdrawn"):
    """An open request this artisan could quote for was withdrawn by the customer."""
    category_name = service_request.category.name
    area = service_request.location.area if service_request.location_id else ""
    where = f" in {area}" if area else ""
    schedule_email(
        event_key=f"service-demand:{service_request.id}:withdrawn:{revision}:artisan:{artisan_user.id}",
        recipient=artisan_user,
        subject=f"{category_name} request withdrawn{where}",
        message=(
            f"The {category_name} request{where} you could serve has been withdrawn by the "
            "customer and is no longer available to quote for. No further action is needed."
        ),
        action_url=dashboard_link(),
        action_label="Open dashboard",
    )

def notify_service_request_no_match(service_request):
    customer = service_request.requester
    category_name = service_request.category.name

    schedule_email(
        event_key=f"{NotificationEvent.SERVICE_REQUEST_NO_MATCH}:{service_request.id}:customer",
        recipient=customer,
        subject="No artisan matched your request yet",
        message=f"We could not currently find an available artisan matching your {category_name} request in the selected service area. You can browse artisans and start a conversation directly.",
        action_url=service_request_link(service_request),
    )

    notify_support(
        event_key=f"{NotificationEvent.SERVICE_REQUEST_NO_MATCH}:{service_request.id}:support",
        subject=f"No artisan match for service request {service_request.id}",
        message=f"No active verified artisan currently matches service request {service_request.id}. Category: {category_name}.",
    )

def _money(amount):
    try:
        return f"₦{int(amount):,}.00"
    except (TypeError, ValueError):
        return str(amount)


def _offer_recipient(offer):
    """The participant who must see this offer: never the person who sent it."""
    conversation = offer.conversation
    if offer.created_by_id == conversation.customer_id:
        return conversation.artisan.user
    return conversation.customer


def offer_recipient(offer):
    """Public alias: the participant who must be told about this offer."""
    return _offer_recipient(offer)


def offer_notification_key(offer, *, counter=None):
    if counter is None:
        counter = bool(offer.parent_offer_id)
    recipient = _offer_recipient(offer)
    event = (
        NotificationEvent.COUNTER_OFFER_RECEIVED
        if counter
        else NotificationEvent.OFFER_RECEIVED
    )
    return f"{event}:{offer.id}:{recipient.id}", recipient


def _person_name(user, fallback):
    name = str(getattr(user, "first_name", "") or "").strip()
    return name or fallback


def _artisan_name(conversation):
    """How the artisan is introduced: their business name, else their own name."""
    artisan = getattr(conversation, "artisan", None)
    business = str(getattr(artisan, "business_name", "") or "").strip()
    if business:
        return business
    return _person_name(getattr(artisan, "user", None), "The artisan")


def _customer_name(conversation):
    return _person_name(getattr(conversation, "customer", None), "The customer")


def notify_offer_received(offer, *, counter=False):
    """Itemised work is announced to both sides the moment it is sent.

    The person who has to answer gets a decision popup that names who priced the
    work and what the payable total is. The person who sent it gets a short
    confirmation naming who is now holding the quote -- never a modal, because
    they cannot answer their own offer. Both are one-use: acknowledging marks the
    same durable notification as read, so neither repeats on the next page.
    """
    conversation = offer.conversation
    recipient = _offer_recipient(offer)
    created_by = offer.created_by
    sender_is_artisan = offer.created_by_id == conversation.artisan.user_id
    sender_name = (
        _artisan_name(conversation) if sender_is_artisan else _customer_name(conversation)
    )
    recipient_name = (
        _customer_name(conversation) if sender_is_artisan else _artisan_name(conversation)
    )
    service_request = (
        conversation.service_request if conversation.service_request_id else None
    )
    work = _request_label(service_request) if service_request is not None else ""
    work_line = f" for {work}" if work else ""
    kind = "revised quote" if counter else "quote"
    payload_data = {
        "offer_id": str(offer.id),
        "conversation_id": str(conversation.id),
        "offer_amount": str(offer.total_amount),
        "offer_kind": "counter" if counter else "offer",
    }

    schedule_email(
        event_key=f"{NotificationEvent.COUNTER_OFFER_RECEIVED if counter else NotificationEvent.OFFER_RECEIVED}:{offer.id}:{recipient.id}",
        recipient=recipient,
        subject=(
            f"Counter offer from {sender_name}"
            if counter
            else f"Quote from {sender_name}"
        ),
        message=(
            f"{sender_name} itemised the work{work_line}. "
            f"Payable total {_money(offer.total_amount)}. "
            "Accept it, decline it, or ask for a change."
        ),
        action_url=conversation_link(conversation),
        action_label="Review the offer",
        popup="modal",
        extra_data=dict(payload_data),
    )

    # The sender's side of the same moment: a confirmation, not a decision.
    if created_by is not None and created_by.id != recipient.id:
        schedule_email(
            event_key=f"{NotificationEvent.OFFER_SENT}:{offer.id}:{created_by.id}",
            recipient=created_by,
            subject=(
                f"Counter offer sent to {recipient_name}"
                if counter
                else f"Quote sent to {recipient_name}"
            ),
            message=(
                f"Your {kind} of {_money(offer.total_amount)}{work_line} is with "
                f"{recipient_name}. You will be told as soon as they accept, "
                "decline or ask for a change."
            ),
            action_url=conversation_link(conversation),
            action_label="Open the conversation",
            popup="toast",
            extra_data=dict(payload_data),
        )


def notify_offer_resolved(offer, *, accepted):
    """The outcome of a proposal, sent to the person who wrote it."""
    conversation = offer.conversation
    recipient = offer.created_by
    schedule_email(
        event_key=f"{NotificationEvent.OFFER_ACCEPTED if accepted else NotificationEvent.OFFER_DECLINED}:{offer.id}:{recipient.id}",
        recipient=recipient,
        subject="Your offer was accepted" if accepted else "Your offer was declined",
        message=(
            f"Your offer of {_money(offer.total_amount)} was accepted. The booking and the payment step are now open."
            if accepted
            else f"Your offer of {_money(offer.total_amount)} was declined. No price was agreed and no payment was taken."
        ),
        action_url=conversation_link(conversation),
        action_label="Open the conversation",
        popup="toast",
    )


def notify_offer_revision_requested(offer):
    conversation = offer.conversation
    schedule_email(
        event_key=f"{NotificationEvent.OFFER_REVISION_REQUESTED}:{offer.id}:{offer.created_by_id}",
        recipient=offer.created_by,
        subject="The customer asked for changes",
        message="The customer asked to change this quote. Edit the itemized list and send the revised price.",
        action_url=conversation_link(conversation),
        action_label="Edit and resend",
        popup="modal",
    )


def notify_booking_created(booking):
    schedule_email(
        event_key=f"{NotificationEvent.BOOKING_CREATED}:{booking.id}:customer",
        recipient=booking.customer.user,
        subject="Your booking request has been submitted",
        message=f"Your booking request for {booking.request.category.name} has been submitted successfully.",
        action_url=booking_link(booking),
        popup="toast",
    )

    schedule_email(
        event_key=f"{NotificationEvent.BOOKING_CREATED}:{booking.id}:artisan",
        recipient=booking.artisan.user,
        subject="You have a new booking request",
        message=f"You have received a booking request for {booking.request.category.name}. Accept or decline it to keep the schedule clear.",
        action_url=booking_link(booking),
        action_label="Review the request",
        popup="modal",
    )

def notify_booking_accepted(booking):
    schedule_email(
        event_key=f"{NotificationEvent.BOOKING_ACCEPTED}:{booking.id}:customer",
        recipient=booking.customer.user,
        subject="Your booking was accepted",
        message=f"The artisan has accepted your {booking.request.category.name} booking. You can now proceed with payment.",
        action_url=booking_link(booking),
        action_label="Continue to payment",
        popup="modal",
    )

def notify_booking_rejected(booking):
    schedule_email(
        event_key=f"{NotificationEvent.BOOKING_REJECTED}:{booking.id}:customer",
        recipient=booking.customer.user,
        subject="Your booking was declined",
        message="The artisan has declined your booking request. No payment was taken. You can select another artisan.",
        action_url=booking_link(booking),
        popup="toast",
    )

def notify_booking_cancelled(booking):
    schedule_email(
        event_key=f"{NotificationEvent.BOOKING_CANCELLED}:{booking.id}:artisan",
        recipient=booking.artisan.user,
        subject="Booking cancelled",
        message="The customer has cancelled this booking. No work should start and no payment will be collected.",
        action_url=booking_link(booking),
        popup="toast",
    )

def notify_booking_started(booking):
    schedule_email(
        event_key=f"{NotificationEvent.BOOKING_STARTED}:{booking.id}:customer",
        recipient=booking.customer.user,
        subject="Your service has started",
        message="The artisan has marked your booking as started.",
        action_url=booking_link(booking),
        popup="toast",
    )

def notify_artisan_completed(booking):
    schedule_email(
        event_key=f"{NotificationEvent.ARTISAN_COMPLETED}:{booking.id}:customer",
        recipient=booking.customer.user,
        subject="The artisan marked your service complete",
        message="The artisan has marked the service as completed. Please review the work and confirm completion.",
        action_url=booking_link(booking),
        action_label="Review and confirm",
        popup="modal",
    )

def notify_customer_completed(booking):
    schedule_email(
        event_key=f"{NotificationEvent.CUSTOMER_COMPLETED}:{booking.id}:artisan",
        recipient=booking.artisan.user,
        subject="Customer confirmed completion",
        message="The customer has confirmed completion of the booking. Your payout has entered the release process.",
        action_url=booking_link(booking),
    )

def notify_booking_disputed(booking):
    schedule_email(
        event_key=f"{NotificationEvent.BOOKING_DISPUTED}:{booking.id}:customer",
        recipient=booking.customer.user,
        subject="Your booking dispute has been recorded",
        message="Your dispute has been recorded. Payout processing is currently frozen while the matter is reviewed.",
        action_url=booking_link(booking),
        action_label="Open the booking",
        popup="toast",
    )

    schedule_email(
        event_key=f"{NotificationEvent.BOOKING_DISPUTED}:{booking.id}:artisan",
        recipient=booking.artisan.user,
        subject="A booking has been disputed",
        message="This booking has been disputed by the customer. Payout processing is currently frozen while the matter is reviewed.",
        action_url=booking_link(booking),
    )

    notify_support(
        event_key=f"{NotificationEvent.BOOKING_DISPUTED}:{booking.id}:support",
        subject=f"Booking dispute: {booking_display_name(booking)}",
        message=f"The {booking_display_name(booking)} booking has been disputed and requires review.",
    )

def _booking_label(payment) -> str:
    """Human description of the booking behind a payment — never its UUID."""
    booking = getattr(payment, "booking", None)
    if booking is None:
        return "this booking"
    return booking_display_name(booking)


def _addon_label(payment) -> str:
    """Human description of a separate add-on order — never its UUID."""
    order = getattr(payment, "addon_order", None)
    offer = getattr(order, "accepted_offer", None)
    text = (getattr(offer, "description", "") or "").split("\n")[0].strip()[:60]
    return f"the add-on order for {text}" if text else "your add-on order"

def notify_payment_initialized(payment):
    is_addon = bool(payment.addon_order_id)
    subject = "Your add-on payment is ready" if is_addon else "Your payment is ready"
    target = _addon_label(payment) if is_addon else _booking_label(payment)
    schedule_email(
        event_key=f"{NotificationEvent.PAYMENT_INITIALIZED}:{payment.id}",
        recipient=payment.customer,
        subject=subject,
        message=f"Your payment for {target} has been initialized and is ready to complete.",
        action_url=payment_link(payment),
        action_label="Complete payment",
        popup="modal",
    )

def _payment_conversation(payment):
    """The chat this money was agreed in, when the payment came out of one."""
    booking = getattr(payment, "booking", None)
    service_request = getattr(booking, "request", None) if booking is not None else None
    conversation = (
        getattr(service_request, "conversation", None)
        if service_request is not None
        else None
    )
    return conversation


def notify_payment_held(payment):
    """The money landed: both sides are told, by email and in the app.

    The customer gets a receipt-style confirmation, the artisan gets the news
    that the escrow is funded. Both are popups that point back at the chat,
    because that is where the work and the schedule were agreed.
    """
    is_addon = bool(payment.addon_order_id)
    target = _addon_label(payment) if is_addon else _booking_label(payment)
    conversation = _payment_conversation(payment)
    action_url = (
        conversation_link(conversation) if conversation is not None else payment_link(payment)
    )
    amount = _money(payment.total_amount_kobo // 100)

    schedule_email(
        event_key=f"{NotificationEvent.PAYMENT_HELD}:{payment.id}:customer",
        recipient=payment.customer,
        subject="Add-on payment confirmed" if is_addon else "Payment confirmed",
        message=(
            f"Your payment of {amount} for the {target} has been confirmed and is "
            "protected in escrow. The artisan is paid only after you confirm the "
            "work is complete."
        ),
        action_url=action_url,
        action_label="Open the chat",
        popup="toast",
        extra_data={"payment_id": str(payment.id), "amount": amount},
    )

    schedule_email(
        event_key=f"{NotificationEvent.PAYMENT_HELD}:{payment.id}:artisan",
        recipient=payment.artisan.user,
        subject="Add-on payment confirmed" if is_addon else "Customer payment confirmed",
        message=(
            f"{amount} for the {target} is now held in escrow. You are paid out "
            "after you submit completion and the customer confirms the work."
        ),
        action_url=action_url,
        action_label="Open the chat",
        popup="toast",
        extra_data={"payment_id": str(payment.id), "amount": amount},
    )

def notify_payment_failed(payment):
    is_addon = bool(payment.addon_order_id)
    target = _addon_label(payment) if is_addon else _booking_label(payment)
    schedule_email(
        event_key=f"{NotificationEvent.PAYMENT_FAILED}:{payment.id}:customer",
        recipient=payment.customer,
        subject="Add-on payment could not be confirmed" if is_addon else "Payment could not be confirmed",
        message=f"We could not confirm the payment for this {target}. Please verify the transaction before attempting another payment.",
        action_url=payment_link(payment),
        action_label="Check the payment",
        popup="modal",
    )

    notify_support(
        event_key=f"{NotificationEvent.PAYMENT_FAILED}:{payment.id}:support",
        subject=f"Payment verification failure: {payment.id}",
        message=f"Payment {payment.id} could not be verified automatically.",
    )

def notify_payout_pending(payment):
    target = _addon_label(payment) if payment.addon_order_id else _booking_label(payment)
    schedule_email(
        event_key=f"{NotificationEvent.PAYOUT_PENDING}:{payment.id}",
        recipient=payment.artisan.user,
        subject="Your payout is being processed",
        message=f"Your payout for {target} has entered the transfer process. You will be notified when the provider confirms it.",
        action_url=payment_link(payment),
    )

def notify_payout_successful(payment):
    is_addon = bool(payment.addon_order_id)
    order_label = _addon_label(payment) if is_addon else _booking_label(payment)
    schedule_email(
        event_key=f"{NotificationEvent.PAYOUT_SUCCESSFUL}:{payment.id}",
        recipient=payment.artisan.user,
        subject="Your payout was successful",
        message=f"Your payout for {order_label} was confirmed and transferred to your registered payout account. A PDF receipt is attached.",
        action_url=payment_link(payment),
        attachments=[{"kind": "payout_receipt_pdf", "payment_id": str(payment.id), "filename": f"payout-receipt-{payment.id}.pdf"}],
    )

def notify_payout_failed(payment):
    target = _addon_label(payment) if payment.addon_order_id else _booking_label(payment)
    schedule_email(
        event_key=f"{NotificationEvent.PAYOUT_FAILED}:{payment.id}:artisan",
        recipient=payment.artisan.user,
        subject="Your payout requires attention",
        message=f"We could not complete your payout for {target}. Please review your payout account details or contact support.",
        action_url=payment_link(payment),
    )

    notify_support(
        event_key=f"{NotificationEvent.PAYOUT_FAILED}:{payment.id}:support",
        subject=f"Payout failure: {payment.id}",
        message=f"Payout for payment {payment.id} failed and requires review.",
    )

def notify_payout_account_verified(artisan_user, account_last4):
    schedule_email(
        event_key=f"payout-account-verified:{artisan_user.pk}:{uuid4().hex[:12]}",
        recipient=artisan_user,
        subject="Your payout account is verified",
        message=(
            f"Your payout account ending in {account_last4} was verified with Flutterwave and activated. "
            "Payouts can now be sent to this account."
        ),
    )


def notify_payout_account_verification_failed(artisan_user, account_last4):
    schedule_email(
        event_key=f"payout-account-verification-failed:{artisan_user.pk}:{uuid4().hex[:12]}",
        recipient=artisan_user,
        subject="Payout account verification failed",
        message=(
            f"We could not verify your payout account ending in {account_last4} with the payout provider, "
            "so no changes were saved. Confirm the bank and account number match your verified identity, "
            "then submit again or contact support."
        ),
    )
    notify_support(
        event_key=f"payout-account-verification-failed:support:{artisan_user.pk}:{uuid4().hex[:12]}",
        subject=f"Payout account verification failed for {artisan_user.pk}",
        message=(
            f"Payout account ending in {account_last4} for artisan {artisan_user.pk} failed automatic "
            "provider verification at save time."
        ),
    )


def notify_verification_submitted(artisan):
    schedule_email(
        event_key=f"{NotificationEvent.VERIFICATION_SUBMITTED}:{artisan.id}",
        recipient=artisan.user,
        subject="Verification submitted",
        message="Your identity verification request has been submitted successfully.",
        action_url=verification_link(),
    )

def notify_verification_approved(artisan):
    schedule_email(
        event_key=f"{NotificationEvent.VERIFICATION_APPROVED}:{artisan.id}",
        recipient=artisan.user,
        subject="Your artisan verification was approved",
        message="Your artisan verification has been successfully approved.",
        action_url=verification_link(),
    )

def notify_verification_rejected(artisan):
    schedule_email(
        event_key=f"{NotificationEvent.VERIFICATION_REJECTED}:{artisan.id}",
        recipient=artisan.user,
        subject="Your artisan verification requires attention",
        message="Your artisan verification could not be approved. Please review the verification requirements.",
        action_url=verification_link(),
    )

def _promotion_target_from_snapshot(application):
    revision = application.latest_revision
    snapshot = revision.snapshot if revision and isinstance(revision.snapshot, dict) else {}
    target = snapshot.get("target") if isinstance(snapshot.get("target"), dict) else {}
    if target.get("kind") == "INSTITUTION":
        label = str(target.get("name") or "Institution")
        if target.get("campus"):
            label = f"{label} — {target['campus']}"
        location = target.get("location") or {}
        place = ", ".join(str(location.get(key) or "") for key in ("area", "state", "country") if location.get(key))
        return f"{label} · {place}" if place else label
    if target.get("kind") == "AREA":
        return ", ".join(str(target.get(key) or "") for key in ("area", "state", "country") if target.get(key)) or "Area"
    if target.get("kind") == "STATE":
        return f"{target.get('state') or application.state_name}, {target.get('country') or application.state_country}"
    return "Homepage Feature"


def notify_promotion_application_submitted(application, revision):
    target = _promotion_target_from_snapshot(application)
    schedule_email(
        event_key=f"{NotificationEvent.PROMOTION_APPLICATION_SUBMITTED}:{application.id}:revision:{revision.revision_number}",
        recipient=application.artisan.user,
        subject="Promotion application submitted",
        message=(
            f"Your {application.get_promotion_type_display()} application for {target} was submitted "
            "and is awaiting Customer Support review. Submission or approval does not activate a campaign "
            "or process payment; payment and scheduling are handled separately."
        ),
        action_url=promotion_application_link(application),
        action_label="View promotion application",
    )


def notify_promotion_application_decision(application, decision):
    target = _promotion_target_from_snapshot(application)
    promotion_type = application.get_promotion_type_display()
    if decision.decision_type == "APPROVED":
        subject = f"{promotion_type} application approved"
        message = (
            f"Your {promotion_type} application for {target} was approved. This approval applies only to "
            "this promotion type and target. It does not activate a campaign or process payment; "
            "payment and scheduling will be handled separately."
        )
    elif decision.decision_type == "CHANGES_REQUIRED":
        subject = f"Changes requested for your {promotion_type} application"
        message = (
            f"Customer Support requested changes to your {promotion_type} application for {target}. "
            f"Reviewer feedback: {decision.reason} You can update the application and resubmit it for review."
        )
    elif decision.decision_type == "REJECTED":
        subject = f"Your {promotion_type} application was not approved"
        message = (
            f"Your {promotion_type} application for {target} was not approved. "
            f"Reviewer explanation: {decision.reason}"
        )
    else:
        return

    schedule_email(
        event_key=f"{NotificationEvent.PROMOTION_APPLICATION_DECISION}:{application.id}:{decision.id}",
        recipient=application.artisan.user,
        subject=subject,
        message=message,
        action_url=promotion_application_link(application),
        action_label="View application details",
    )


def notify_review_received(*, artisan_user, booking, rating):
    schedule_email(
        event_key=f"{NotificationEvent.REVIEW_RECEIVED}:{booking.id}:artisan",
        recipient=artisan_user,
        subject="You received a new customer review",
        message=f"A customer has submitted a {rating}-star review for the {booking_display_name(booking)} booking.",
        action_url=booking_link(booking),
    )

def notify_account_suspended(user, reason):
    schedule_email(
        event_key=f"{NotificationEvent.ACCOUNT_SUSPENDED}:{user.id}",
        recipient=user,
        subject="Your Artisan Marketplace account has been suspended",
        message=f"Your account has been suspended. Reason: {reason}",
    )

def notify_customer_cancellation_risk_warning(user, score, max_score=5):
    schedule_email(
        event_key=f"customer-cancellation-risk:{user.id}:{score}",
        recipient=user,
        subject="Notice about your booking cancellation activity",
        message=(
            f"Your recent booking cancellation activity has reached risk score {score}/{max_score}. "
            "Repeated customer-attributable cancellations may temporarily restrict or suspend account access. "
            "Please cancel only when necessary and select the reason that accurately describes the situation."
        ),
        action_url=cancellation_risk_link(),
    )

def notify_support(*, event_key, subject, message):
    recipients = getattr(settings, "NOTIFICATION_SUPPORT_EMAILS", [])

    def enqueue():
        for email in recipients:
            key = f"{event_key}:{email}"
            enqueue_outbox(event_type="notification.email", event_key=key, payload={"event_key": key, "recipient_email": email, "recipient_name": "Support Team", "subject": subject, "message": message})
        _safe_dispatch_outbox()
    if transaction.get_autocommit(): enqueue()
    else: transaction.on_commit(enqueue)

def notify_support_case_created(case):
    opened_by = case.opened_by

    schedule_email(
        event_key=f"{NotificationEvent.SUPPORT_CASE_CREATED}:{case.id}:opener",
        recipient=opened_by,
        subject=f"Support case {case.reference} has been opened",
        message=(
            f"Your support case {case.reference} has been successfully opened. "
            f"Our support team will review the case and contact you if additional information is required."
        ),
        action_url=support_case_link(case),
    )

    if case.booking:
        customer = case.booking.customer.user
        artisan = case.booking.artisan.user

        if opened_by.id == customer.id:
            recipient = artisan
            recipient_role = "artisan"
        else:
            recipient = customer
            recipient_role = "customer"

        schedule_email(
            event_key=f"{NotificationEvent.SUPPORT_CASE_CREATED}:{case.id}:{recipient_role}",
            recipient=recipient,
            subject=f"Support case opened for booking {case.booking_id}",
            message=(
                f"A support case ({case.reference}) has been opened regarding booking "
                f"{case.booking_id}. The matter is now under the customer-care process."
            ),
        action_url=support_case_link(case),
        )

    notify_support(
        event_key=f"{NotificationEvent.SUPPORT_CASE_CREATED}:{case.id}:support",
        subject=f"New support case: {case.reference}",
        message=(
            f"Support case {case.reference} has been opened. "
            f"Type: {case.get_case_type_display()}. "
            f"Priority: {case.get_priority_display()}. "
            f"Booking: {case.booking_id or 'N/A'}."
        ),
    )

def notify_support_case_closed(case):
    if not case.booking:
        schedule_email(
            event_key=f"{NotificationEvent.SUPPORT_CASE_CLOSED}:{case.id}:opener",
            recipient=case.opened_by,
            subject=f"Support case {case.reference} is closed",
            message=f"Your support case {case.reference} has now been closed.",
        action_url=support_case_link(case),
        )
        return

    customer = case.booking.customer.user
    artisan = case.booking.artisan.user

    message = (
        f"Support case {case.reference} for booking {case.booking_id} has been closed."
    )

    schedule_email(
        event_key=f"{NotificationEvent.SUPPORT_CASE_CLOSED}:{case.id}:customer",
        recipient=customer,
        subject=f"Support case {case.reference} is closed",
        message=message,
        action_url=support_case_link(case),
    )

    schedule_email(
        event_key=f"{NotificationEvent.SUPPORT_CASE_CLOSED}:{case.id}:artisan",
        recipient=artisan,
        subject=f"Support case {case.reference} is closed",
        message=message,
        action_url=support_case_link(case),
    )

def notify_support_case_assigned(case):
    if not case.assigned_to:
        return

    schedule_email(
        event_key=f"{NotificationEvent.SUPPORT_CASE_ASSIGNED}:{case.id}:opener",
        recipient=case.opened_by,
        subject=f"Your support case {case.reference} is under review",
        message=(
            f"Your support case {case.reference} has been assigned to our customer-care team "
            f"and is now under review."
        ),
        action_url=support_case_link(case),
    )

def notify_support_case_resolved(case):
    resolution = case.get_resolution_code_display()

    if not case.booking:
        schedule_email(
            event_key=f"{NotificationEvent.SUPPORT_CASE_RESOLVED}:{case.id}:opener",
            recipient=case.opened_by,
            subject=f"Your support case {case.reference} has been resolved",
            message=(
                f"Your support case {case.reference} has been resolved. "
                f"Resolution: {resolution}. "
                f"{case.resolution_note}"
            ),
        action_url=support_case_link(case),
        )

        notify_support(
            event_key=f"{NotificationEvent.SUPPORT_CASE_RESOLVED}:{case.id}:support",
            subject=f"Support case resolved: {case.reference}",
            message=(
                f"Support case {case.reference} has been resolved. "
                f"Resolution: {resolution}."
            ),
        )
        return

    customer = case.booking.customer.user
    artisan = case.booking.artisan.user

    message = (
        f"Support case {case.reference} for booking {case.booking_id} has been resolved. "
        f"Resolution: {resolution}. "
        f"{case.resolution_note}"
    )

    schedule_email(
        event_key=f"{NotificationEvent.SUPPORT_CASE_RESOLVED}:{case.id}:customer",
        recipient=customer,
        subject=f"Support case {case.reference} has been resolved",
        message=message,
        action_url=support_case_link(case),
    )

    schedule_email(
        event_key=f"{NotificationEvent.SUPPORT_CASE_RESOLVED}:{case.id}:artisan",
        recipient=artisan,
        subject=f"Support case {case.reference} has been resolved",
        message=message,
        action_url=support_case_link(case),
    )

    notify_support(
        event_key=f"{NotificationEvent.SUPPORT_CASE_RESOLVED}:{case.id}:support",
        subject=f"Support case resolved: {case.reference}",
        message=(
            f"Support case {case.reference} for booking {case.booking_id} has been resolved. "
            f"Resolution: {resolution}."
        ),
    )












def notify_support_refund_requested(action):
    notify_support(
        event_key=(
            f"{NotificationEvent.SUPPORT_REFUND_REQUESTED}:"
            f"{action.id}"
        ),
        subject=f"Refund requested: {action.reference}",
        message=(
            f"Refund {action.reference} was requested for "
            f"payment {action.payment_id}. "
            f"Amount: NGN {action.amount_kobo / 100:,.2f}."
        ),
    )


def notify_support_refund_succeeded(action):
    schedule_email(
        event_key=(
            f"{NotificationEvent.SUPPORT_REFUND_SUCCEEDED}:"
            f"{action.id}:customer"
        ),
        recipient=action.payment.customer,
        subject="Your refund has been completed",
        message=(
            f"A refund of NGN {action.amount_kobo / 100:,.2f} "
            f"for booking {action.payment.booking_id} "
            f"has been completed."
        ),
        action_url=support_case_link(action.case),
    )


def notify_support_refund_failed(action):
    notify_support(
        event_key=(
            f"{NotificationEvent.SUPPORT_REFUND_FAILED}:"
            f"{action.id}"
        ),
        subject=f"Refund failed: {action.reference}",
        message=(
            f"Refund {action.reference} failed. "
            f"Reason: {action.failure_reason or 'Provider did not complete the refund.'}"
        ),
    )

def notify_verification_result(
    artisan,
    *,
    event_key,
    title,
    message,
):
    recipient = artisan.user

    if not recipient or not recipient.is_active:
        return

    # Use your EXISTING in-app notification mechanism here.
    create_in_app_notification(user=recipient, event_key=event_key, title=title, message=message)

    schedule_email(
        event_key=f"{event_key}:email",
        recipient=recipient,
        subject=title,
        message=message,
        action_url=verification_link(),
    )

def notify_promotion_campaign_update(campaign, *, action, reason=""):
    """Notify the campaign owner about trusted payment and schedule/state transitions."""
    labels = {
        "PAYMENT_VERIFIED": ("Promotion payment verified", "Your promotion payment has been verified. The campaign is being placed in its paid target queue."),
        "SCHEDULED": ("Promotion campaign scheduled", "Your paid promotion has a reserved start time. The estimate may move if capacity changes or earlier verified payments enter the queue."),
        "STARTED": ("Promotion campaign is live", "Your paid promotion is now active."),
        "PAUSED": ("Promotion campaign paused for review", "Your campaign was temporarily paused while Customer Support reviews the linked case. A complaint alone is not a finding."),
        "SUSPENDED": ("Promotion campaign suspended", "Your campaign was temporarily suspended while the linked case is reviewed."),
        "RESTORED": ("Promotion campaign restored", "Your campaign was restored with its remaining paid duration and re-entered the paid queue."),
        "REVOKED": ("Promotion campaign ended", "Your promotion campaign was revoked. Any refund due under the accepted terms must go through the separate authorized refund approval and execution process."),
        "EXPIRED": ("Promotion campaign ended", "Your paid promotion term has ended."),
        "PAYMENT_REVIEW": ("Promotion payment needs review", "Flutterwave reported a payment that could not be safely matched to the approved promotion. Customer Support will review it; the campaign is not active."),
    }
    subject, base_message = labels.get(action, ("Promotion campaign updated", "Your promotion campaign status changed."))
    detail = f" Campaign: {campaign.plan_name_snapshot} for {campaign.inventory.target_label}."
    if reason:
        detail += f" Reason: {reason.strip()}"
    schedule_email(
        event_key=f"promotion-campaign:{campaign.id}:{action}:{campaign.updated_at.isoformat()}",
        recipient=campaign.artisan.user,
        subject=subject,
        message=base_message + detail,
        action_url=promotion_center_link(),
        action_label="View My Promotions",
    )


def notify_service_request_attended(service_request):
    from .matching import get_matching_artisans
    users = set()
    for offering in get_matching_artisans(service_request).iterator():
        user = offering.artisan.user
        if user.id in users:
            continue
        users.add(user.id)
        schedule_email(
            event_key=f"service-demand:{service_request.id}:attended:{user.id}",
            recipient=user,
            subject=f"{service_request.category.name} request is no longer available",
            message=f"Request #{service_request.id} has been attended to and is no longer open for responses.",
            action_url=dashboard_link(),
        )


def notify_request_not_interested(service_request, artisan):
    """Two durable alerts, once per personal dismissal; no global rejection."""
    key = f'request-not-interested:{service_request.pk}:{artisan.pk}'
    schedule_email(
        event_key=key + ':artisan', recipient=artisan.user,
        subject=f'Request #{service_request.pk} hidden from your matches',
        message='You marked this request as not interested. It is hidden only from your matched-request board and remains open to other eligible artisans.',
        action_url='/account/artisan/requests/', action_label='Open matching requests',
    )
    schedule_email(
        event_key=key + ':customer', recipient=service_request.requester,
        subject=f'An artisan passed on request #{service_request.pk}',
        message='One eligible artisan marked your request as not interested. Your request has not been rejected or closed for everyone; other eligible artisans can still respond.',
        action_url=f'/requests/{service_request.pk}/', action_label='View your request',
    )
