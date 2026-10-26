"""First-unread message bursts and separate, safe workflow notifications.

Only identifiers and a generic action prompt leave the chat. Neither a transcript,
filenames nor recording contents are placed in email/outbox notification payloads.
"""

import hashlib
from django.conf import settings
from django.contrib.auth import get_user_model
from notifications.events import schedule_email
from .models import ConversationReadState


def _recipients(room):
    ids = set(room.participants.values_list("pk", flat=True))
    if room.customer_id:
        ids.add(room.customer_id)
    if room.artisan_id:
        ids.add(room.artisan.user_id)
    return get_user_model().objects.filter(pk__in=ids, is_active=True)


def _deliver(room, user, key, event_type, title):
    url = f"/discover/conversations/{room.pk}/"
    schedule_email(
        event_key=key,
        recipient=user,
        subject=title,
        message="Open the service conversation to review the latest activity. Quotes, payment and completion require their own confirmed actions.",
        action_label="Open conversation",
        action_url=url,
    )


def notify_room_message(message):
    room = message.conversation
    for user in _recipients(room):
        if user.pk == message.sender_id:
            continue
        state = ConversationReadState.objects.filter(
            conversation=room, user=user
        ).first()
        anchor = (
            state.last_read_at.isoformat()
            if state and state.last_read_at
            else "never-read"
        )
        burst = hashlib.sha256(anchor.encode()).hexdigest()[:24]
        _deliver(
            room,
            user,
            f"chat-burst:{room.pk}:{user.pk}:{burst}",
            "conversation.message",
            "New unread service message",
        )


def notify_room_event(message):
    # Draft greetings are not workflow alerts; only committed, explicit actions fan out.
    if message.event_code in {
        "CONVERSATION_STARTED",
        "REQUEST_GREETING",
        "REQUEST_DRAFT_CREATED",
        "DRAFT_SAVED",
    }:
        return
    for user in _recipients(message.conversation):
        _deliver(
            message.conversation,
            user,
            f"chat-workflow:{message.pk}:{user.pk}",
            "conversation.workflow",
            "Service conversation update",
        )
