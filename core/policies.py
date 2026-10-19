from decimal import Decimal

MAX_SERVICE_AMOUNT_KOBO = 100_000_000_000
CUSTOMER_FEE_KOBO = 15_000
ARTISAN_COMMISSION_RATE = Decimal("0.1000")
JSON_BODY_LIMIT_BYTES = 256 * 1024
MESSAGE_MAX_CHARS = 5_000
CONVERSATION_ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024
CONVERSATION_ATTACHMENT_MAX_PER_MESSAGE = 5
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
AUDIT_LOG_RETENTION_DAYS = 365
OUTBOX_RETENTION_DAYS = 30
IDEMPOTENCY_RETENTION_DAYS = 2
BOOKING_ACTIVE_STATUSES = {"REQUESTED", "ACCEPTED", "IN_PROGRESS", "COMPLETION_REQUESTED", "DISPUTED"}
BOOKING_TERMINAL_STATUSES = {"COMPLETED", "CANCELLED", "REJECTED"}

def can_access_conversation(conversation, user): return conversation.customer_id == user.id or conversation.artisan.user_id == user.id
def is_customer(conversation, user): return conversation.customer_id == user.id
def is_artisan(conversation, user): return conversation.artisan.user_id == user.id
def is_support_staff(user): return bool(user and user.is_staff)
