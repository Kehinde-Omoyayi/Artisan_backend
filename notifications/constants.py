class NotificationEvent:
    SERVICE_REQUEST_CREATED = "service_request_created"
    SERVICE_REQUEST_UPDATED = "service_request_updated"
    SERVICE_REQUEST_WITHDRAWN = "service_request_withdrawn"
    SERVICE_REQUEST_NO_MATCH = "service_request_no_match"

    BOOKING_CREATED = "booking_created"
    BOOKING_ACCEPTED = "booking_accepted"
    BOOKING_REJECTED = "booking_rejected"
    BOOKING_CANCELLED = "booking_cancelled"
    BOOKING_STARTED = "booking_started"
    ARTISAN_COMPLETED = "artisan_completed"
    CUSTOMER_COMPLETED = "customer_completed"
    BOOKING_DISPUTED = "booking_disputed"

    PAYMENT_INITIALIZED = "payment_initialized"
    PAYMENT_HELD = "payment_held"
    PAYMENT_FAILED = "payment_failed"

    PAYOUT_PENDING = "payout_pending"
    PAYOUT_SUCCESSFUL = "payout_successful"
    PAYOUT_FAILED = "payout_failed"

    VERIFICATION_SUBMITTED = "verification_submitted"
    VERIFICATION_APPROVED = "verification_approved"
    VERIFICATION_REJECTED = "verification_rejected"

    PROMOTION_APPLICATION_SUBMITTED = "promotion_application_submitted"
    PROMOTION_APPLICATION_DECISION = "promotion_application_decision"

    REVIEW_RECEIVED = "review_received"

    SUPPORT_CASE_CREATED = "support_case_created"
    SUPPORT_CASE_ASSIGNED = "support_case_assigned"
    SUPPORT_CASE_RESOLVED = "support_case_resolved"
    SUPPORT_CASE_CLOSED = "support_case_closed"
    SUPPORT_REFUND_REQUESTED = "support_refund_requested"
    SUPPORT_REFUND_SUCCEEDED = "support_refund_succeeded"
    SUPPORT_REFUND_FAILED = "support_refund_failed"

    ACCOUNT_SUSPENDED = "account_suspended"