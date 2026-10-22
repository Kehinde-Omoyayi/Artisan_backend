import base64
import hashlib
import hmac
import json
import logging
import secrets
import uuid

from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from drf_spectacular.utils import OpenApiExample, OpenApiResponse, OpenApiTypes, extend_schema, inline_serializer
from rest_framework import generics, serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from booking.models import Booking, BookingAdditionalOrder
from home.models import ArtisanProfile, CustomUser
from home.otp_security import (
    hash_otp_code,
    otp_attempts_cache_key,
    otp_cache_key,
    otp_cooldown_cache_key,
    verify_otp_code,
)
from home.security_mail import encrypt_email_code
from notifications.events import notify_payment_initialized
from services.identity_names import (
    ACCOUNT_NAME_MISMATCH_MESSAGE,
    PROFILE_NAME_CORRECTION_MESSAGE,
    account_name_matches_identity,
    profile_name_matches_identity,
)
from services.kyc_crypto import decrypt_identity_name, decrypt_value, encrypt_value
from services.models import ArtisanVerification
from support.permissions import SupportFinancePermission
from core.audit import audit_request_action
from core.idempotency import begin, finish, idempotent_action
from drf_spectacular.utils import OpenApiParameter

from .flutterwave import FlutterwaveClient, FlutterwaveError, FlutterwaveUnknownError
from .models import ArtisanPayoutAccount, EscrowPayment, EscrowRelease, PaymentWebhookEvent
from .payout_accounts import (
    get_supported_bank,
    get_supported_nigerian_banks,
    resolved_account_details,
)
from .serializers import ArtisanPayoutAccountSerializer, PaymentSerializer
from .services import prepare_booking_payment, prepare_additional_order_payment
from .tasks import (
    initialize_prepared_payment,
    reconcile_artisan_payout,
    send_payout_change_email,
    verify_flutterwave_payment,
)

logger = logging.getLogger(__name__)

class InitializeBookingPaymentView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "payment_initialize"

    @extend_schema(tags=["Payments"], description="Prepares payment locally and queues Flutterwave initialization asynchronously. Provider latency is never part of the request transaction.", request=None, parameters=[OpenApiParameter("Idempotency-Key", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True)], responses={202: OpenApiResponse(description="Payment initialization queued."), 400: OpenApiResponse(description="Payment cannot be prepared."), 403: OpenApiResponse(description="The authenticated user is not authorized."), 404: OpenApiResponse(description="Booking not found."), 409: OpenApiResponse(description="Payment initialization already exists or is not available.")})
    def post(self, request, booking_id):
        key = request.headers.get("Idempotency-Key", "").strip()
        if not key: return Response({"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "Idempotency-Key is required for payment initialization."}, status=status.HTTP_400_BAD_REQUEST)
        try: booking = Booking.objects.select_related("customer__user", "artisan").get(pk=booking_id, customer__user=request.user)
        except Booking.DoesNotExist: return Response({"detail": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            record, created, cached = begin(actor=request.user, operation="payment.initialize", key=key, payload={"booking_id": str(booking_id)}, resource_type="Booking", resource_id=str(booking.id))
            if not created: return Response(cached[1], status=cached[0])
            try: payment = prepare_booking_payment(booking=booking)
            except ValueError as exc:
                transaction.set_rollback(True)
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            data = {"payment_id": str(payment.id), "reference": payment.reference, "service_amount": payment.service_amount_kobo / 100, "customer_fee": payment.customer_fee_kobo / 100, "total_amount": payment.total_amount_kobo / 100, "currency": "NGN", "status": payment.status, "initialization_pending": payment.status == EscrowPayment.Status.PAYMENT_PENDING}
            finish(record, status_code=202, body=data, resource_type="Payment", resource_id=str(payment.id)); audit_request_action(request=request, action="PAYMENT_INITIALIZATION_QUEUED", resource_type="Payment", resource_id=str(payment.id), metadata={"booking_id": str(booking.id)}); transaction.on_commit(lambda payment_id=str(payment.id): initialize_prepared_payment.delay(payment_id))
        return Response(data, status=status.HTTP_202_ACCEPTED)


class InitializeAdditionalOrderPaymentView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "payment_initialize"

    @extend_schema(
        tags=["Payments"],
        summary="Prepare escrow payment for an approved add-on",
        description="Creates or resumes a real Flutterwave escrow payment for one approved add-on order. Payment success is confirmed only by server-side provider verification.",
        request=None,
        parameters=[OpenApiParameter("Idempotency-Key", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True)],
        responses={202: OpenApiResponse(description="Payment initialization queued."), 400: OpenApiResponse(description="The add-on is not payable."), 404: OpenApiResponse(description="Add-on order not found."), 409: OpenApiResponse(description="Payment initialization is already processing.")},
    )
    def post(self, request, order_id):
        key = request.headers.get("Idempotency-Key", "").strip()
        if not key:
            return Response({"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "Idempotency-Key is required for add-on payment initialization."}, status=status.HTTP_400_BAD_REQUEST)
        order = BookingAdditionalOrder.objects.select_related(
            "booking__customer__user", "booking__artisan", "accepted_offer"
        ).filter(pk=order_id, booking__customer__user=request.user).first()
        if order is None:
            return Response({"detail": "Add-on order not found."}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            record, created, cached = begin(
                actor=request.user,
                operation="payment.initialize_additional_order",
                key=key,
                payload={"additional_order_id": str(order_id)},
                resource_type="BookingAdditionalOrder",
                resource_id=str(order.id),
            )
            if not created:
                return Response(cached[1], status=cached[0])
            try:
                payment = prepare_additional_order_payment(order=order, user=request.user)
            except ValueError as exc:
                transaction.set_rollback(True)
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

            data = {
                "payment_id": str(payment.id),
                "additional_order_id": str(order.id),
                "booking_id": str(order.booking_id),
                "reference": payment.reference,
                "service_amount": payment.service_amount_kobo / 100,
                "customer_fee": payment.customer_fee_kobo / 100,
                "total_amount": payment.total_amount_kobo / 100,
                "currency": "NGN",
                "status": payment.status,
                "initialization_pending": payment.status == EscrowPayment.Status.PAYMENT_PENDING,
            }
            response_status = status.HTTP_202_ACCEPTED if payment.status == EscrowPayment.Status.PAYMENT_PENDING else status.HTTP_200_OK
            finish(record, status_code=response_status, body=data, resource_type="Payment", resource_id=str(payment.id))
            audit_request_action(
                request=request,
                action="ADDITIONAL_ORDER_PAYMENT_INITIALIZATION_QUEUED",
                resource_type="Payment",
                resource_id=str(payment.id),
                metadata={"booking_id": str(order.booking_id), "additional_order_id": str(order.id)},
            )
            if payment.status == EscrowPayment.Status.PAYMENT_PENDING:
                transaction.on_commit(lambda payment_id=str(payment.id): initialize_prepared_payment.delay(payment_id))
        return Response(data, status=response_status)


class ArtisanEarningsSummaryView(APIView):
    """Expose only successful payouts for the authenticated artisan."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Payments"],
        description="Returns completed artisan payouts for the current calendar month and lifetime, in NGN kobo.",
        responses={200: OpenApiResponse(description="Completed payout totals for the authenticated artisan.")},
    )
    def get(self, request):
        artisan = getattr(request.user, "artisan_profile", None)
        if artisan is None:
            raise PermissionDenied("An artisan profile is required to view earnings.")

        today = timezone.localdate()
        completed_releases = EscrowRelease.objects.filter(
            payment__artisan=artisan,
            status=EscrowRelease.Status.SUCCESSFUL,
            completed_at__isnull=False,
        )
        lifetime = completed_releases.aggregate(
            amount_kobo=Sum("amount_kobo"),
            payout_count=Count("pk"),
        )
        this_month = completed_releases.filter(
            completed_at__year=today.year,
            completed_at__month=today.month,
        ).aggregate(
            amount_kobo=Sum("amount_kobo"),
            payout_count=Count("pk"),
        )
        return Response({
            "currency": "NGN",
            "month_label": today.strftime("%B %Y"),
            "this_month": {
                "amount_kobo": int(this_month["amount_kobo"] or 0),
                "payout_count": int(this_month["payout_count"] or 0),
            },
            "lifetime": {
                "amount_kobo": int(lifetime["amount_kobo"] or 0),
                "payout_count": int(lifetime["payout_count"] or 0),
            },
        })


class PaymentDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["Payments"], responses={200: PaymentSerializer, 404: OpenApiResponse(description="Payment not found.")})
    def get(self, request, payment_id):
        payment = (
            EscrowPayment.objects
            .select_related("booking__customer__user", "booking__artisan__user")
            .filter(id=payment_id)
            .filter(Q(customer=request.user) | Q(artisan__user=request.user))
            .first()
        )
        if not payment:
            return Response({"detail": "Payment not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(PaymentSerializer(payment, context={"request": request}).data)

class FlutterwaveCallbackView(APIView):
    permission_classes: list[type] = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "payment_callback"

    @extend_schema(
        tags=["Payments"],
        description="Receives the Flutterwave payment callback and schedules server-side verification.",
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiResponse(description="Callback received."), 400: OpenApiResponse(description="Invalid callback payload.")},
    )
    def get(self, request):
        transaction_id = str(request.query_params.get("transaction_id") or "").strip()
        tx_ref = str(request.query_params.get("tx_ref") or "").strip()
        if not transaction_id or not tx_ref:
            return Response({"detail": "transaction_id and tx_ref are required."}, status=status.HTTP_400_BAD_REQUEST)
        if not transaction_id.isdigit() or len(transaction_id) > 30:
            return Response({"detail": "Invalid transaction_id."}, status=status.HTTP_400_BAD_REQUEST)

        payment = EscrowPayment.objects.filter(reference=tx_ref).first()
        if payment:
            if payment.provider_transaction_id and str(payment.provider_transaction_id) != transaction_id:
                return Response({"status": "received"}, status=status.HTTP_202_ACCEPTED)
            try:
                verify_flutterwave_payment.delay(
                    str(payment.id),
                    None if payment.provider_transaction_id else transaction_id,
                )
            except Exception:
                # The periodic reconciliation task re-verifies pending payments; never fail the redirect.
                logger.exception("payment_callback_enqueue_failed payment=%s", payment.id)
            return Response({"status": "received"}, status=status.HTTP_202_ACCEPTED)

        # Promotion fees have their own payment model and verifier; never route them through escrow.
        from promotions.models import PromotionPayment
        promotion_payment = PromotionPayment.objects.filter(reference=tx_ref).first()
        if promotion_payment:
            if promotion_payment.provider_transaction_id and str(promotion_payment.provider_transaction_id) != transaction_id:
                return Response({"status": "received"}, status=status.HTTP_202_ACCEPTED)
            try:
                from promotions.tasks import verify_promotion_payment
                verify_promotion_payment.delay(
                    str(promotion_payment.id),
                    None if promotion_payment.provider_transaction_id else transaction_id,
                )
            except Exception:
                logger.exception("promotion_payment_callback_enqueue_failed payment=%s", promotion_payment.id)
        return Response({"status": "received"}, status=status.HTTP_202_ACCEPTED)

def _valid_flutterwave_signature(request):
    secret_hash = settings.FLW_SECRET_HASH
    if not secret_hash:
        return False

    signature = request.headers.get("flutterwave-signature")
    if signature:
        expected = base64.b64encode(hmac.new(secret_hash.encode(), request.body, hashlib.sha256).digest()).decode()
        return hmac.compare_digest(signature, expected)

    legacy = request.headers.get("verif-hash")
    return bool(legacy and hmac.compare_digest(legacy, secret_hash))

@csrf_exempt
@require_POST
def flutterwave_webhook(request):
    if not _valid_flutterwave_signature(request):
        return HttpResponse(status=401)

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    if not isinstance(payload, dict):
        return HttpResponse(status=400)
    event_type = str(payload.get("event") or payload.get("event.type") or "")
    data = payload.get("data") or payload.get("transfer") or {}
    if not isinstance(data, dict):
        data = {}
    data_id = str(data.get("id") or payload.get("id") or "")
    if not event_type or not data_id:
        return HttpResponse(status=200)

    event_key = f"{event_type}:{data_id}"

    with transaction.atomic():
        event, created = PaymentWebhookEvent.objects.select_for_update(of=("self",)).get_or_create(
            provider="flutterwave",
            event_key=event_key,
            defaults={"event_type": event_type, "payload": payload},
        )
        if not created and event.processed_at:
            return HttpResponse(status=200)
        if not created:
            event.event_type = event_type
            event.payload = payload
            event.save(update_fields=["event_type", "payload"])

        if event_type in {"charge.completed", "charge.failed", "charge.cancelled"}:
            tx_ref = str(data.get("tx_ref") or "")
            payment = EscrowPayment.objects.filter(reference=tx_ref).first() if tx_ref else None
            if payment:
                transaction.on_commit(lambda payment_id=str(payment.id), webhook_event_id=event.id: verify_flutterwave_payment.delay(payment_id, webhook_event_id=webhook_event_id))
            elif tx_ref:
                from promotions.models import PromotionPayment
                promotion_payment = PromotionPayment.objects.filter(reference=tx_ref).first()
                if promotion_payment:
                    def enqueue_promotion_verification(payment_id=str(promotion_payment.id), webhook_event_id=event.id):
                        from promotions.tasks import verify_promotion_payment
                        verify_promotion_payment.delay(payment_id, webhook_event_id=webhook_event_id)
                    transaction.on_commit(enqueue_promotion_verification)

        elif event_type in {"transfer.completed", "transfer.failed", "transfer.cancelled"}:
            reference = str(data.get("reference") or "")
            release = EscrowRelease.objects.filter(reference=reference).first() if reference else None
            if release:
                release.provider_transfer_id = str(data.get("id")) if data.get("id") else release.provider_transfer_id
                release.provider_reference = reference
                release.provider_payload = payload
                release.save(update_fields=["provider_transfer_id", "provider_reference", "provider_payload", "updated_at"])
                transaction.on_commit(lambda release_id=str(release.id), webhook_event_id=event.id: reconcile_artisan_payout.delay(release_id, webhook_event_id=webhook_event_id))

    return HttpResponse(status=200)

PAYOUT_PROTECTED_PAYMENT_STATUSES = (
    EscrowPayment.Status.HELD,
    EscrowPayment.Status.RELEASE_PENDING,
    EscrowPayment.Status.DISPUTED,
)


def _reject_verified_identity_for_profile_mismatch(verification):
    """Revoke stale verification if a previously verified profile name no longer matches."""
    with transaction.atomic():
        locked = ArtisanVerification.objects.select_for_update(of=("self",)).filter(
            pk=verification.pk
        ).first()
        if locked and locked.verification_status == "VERIFIED":
            locked.verification_status = "REJECTED"
            locked.verified_identity_name_encrypted = ""
            locked.verified_at = None
            locked.save(update_fields=[
                "verification_status",
                "verified_identity_name_encrypted",
                "verified_at",
                "updated_at",
            ])


def _eligible_payout_identity(artisan):
    if artisan.profile_status != "ACTIVE":
        raise PermissionDenied(
            "Your artisan profile must be active before payout details can be added or changed."
        )

    verification = ArtisanVerification.objects.filter(artisan_id=artisan.pk).first()
    if not verification or verification.verification_status != "VERIFIED":
        raise PermissionDenied(
            "Complete identity verification before adding or changing payout details."
        )
    if not verification.verified_identity_name_encrypted:
        raise PermissionDenied(
            "Please complete identity verification again so your verified name can be securely confirmed before setting payout details."
        )
    try:
        identity_name = decrypt_identity_name(verification.verified_identity_name_encrypted)
    except ValueError:
        raise PermissionDenied(
            "We could not securely confirm your verified identity name. Please complete identity verification again."
        ) from None

    if not profile_name_matches_identity(
        identity_name,
        artisan.user.first_name,
        artisan.user.last_name,
    ):
        _reject_verified_identity_for_profile_mismatch(verification)
        raise PermissionDenied(PROFILE_NAME_CORRECTION_MESSAGE)
    return verification, identity_name


def _payout_has_protected_funds(artisan):
    return EscrowPayment.objects.filter(
        artisan=artisan,
        status__in=PAYOUT_PROTECTED_PAYMENT_STATUSES,
    ).exists()


def _provider_error_response(*, verification_step=False, unknown=False):
    if verification_step:
        detail = (
            "The payout provider could not complete verification just now. "
            "The account remains pending and payouts are not enabled. Please retry later."
            if unknown
            else "The payout provider could not confirm this account. The account remains pending and payouts are not enabled."
        )
    else:
        detail = (
            "Flutterwave could not complete bank verification just now. No payout details were saved. Please try again later."
            if unknown
            else "Flutterwave could not verify this bank account. Check the bank and account number, then try again."
        )
    return Response({"detail": detail}, status=status.HTTP_503_SERVICE_UNAVAILABLE if unknown else status.HTTP_400_BAD_REQUEST)


# Payout-account change protection: after every successful save the details are
# locked for a cooldown window, and replacing an existing account always
# requires a one-time code emailed to the artisan.
PAYOUT_CHANGE_LOCK_DURATION = timedelta(days=3)
PAYOUT_CHANGE_OTP_PURPOSE = "payout_change"
PAYOUT_CHANGE_OTP_TTL_SECONDS = 600
PAYOUT_CHANGE_OTP_RESEND_SECONDS = 60
PAYOUT_CHANGE_OTP_MAX_ATTEMPTS = 5
PAYOUT_CHANGE_OTP_REQUIRED_MESSAGE = (
    "Enter the 6-digit security code emailed to you to change your payout account."
)


def _payout_change_lock_response(locked_until):
    now = timezone.now()
    remaining = max(1, int((locked_until - now).total_seconds()))
    days = remaining // 86400
    hours = (remaining % 86400) // 3600
    if days:
        wait = f"{days} day{'s' if days != 1 else ''}"
    elif hours:
        wait = f"{hours} hour{'s' if hours != 1 else ''}"
    else:
        wait = "a few minutes"
    return Response(
        {
            "detail": (
                "Payout account details are locked for your protection. "
                f"You can change them again in about {wait}."
            ),
            "payout_change_locked_until": locked_until.isoformat(),
            "retry_after_seconds": remaining,
        },
        status=423,  # Locked
    )


def _payout_change_locked_until(account):
    locked_until = getattr(account, "payout_change_locked_until", None)
    if locked_until and locked_until > timezone.now():
        return locked_until
    return None


def _record_failed_payout_otp_attempt(email):
    key = otp_attempts_cache_key(PAYOUT_CHANGE_OTP_PURPOSE, email)
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=PAYOUT_CHANGE_OTP_TTL_SECONDS)
        return 1


def _consume_payout_change_otp(user, submitted_code):
    """Return True on a valid code, False on an invalid/expired one, None when missing.

    A successful check deletes the stored digest so each code is single-use; failures
    are counted and past the cap the stored code is invalidated to stop guessing.
    """
    email = (getattr(user, "email", "") or "").strip().casefold()
    submitted = str(submitted_code or "").strip()
    if not email:
        return False
    if not submitted:
        return None
    attempts_key = otp_attempts_cache_key(PAYOUT_CHANGE_OTP_PURPOSE, email)
    attempts = int(cache.get(attempts_key) or 0)
    if attempts >= PAYOUT_CHANGE_OTP_MAX_ATTEMPTS:
        cache.delete(otp_cache_key(PAYOUT_CHANGE_OTP_PURPOSE, email))
        return False
    stored_digest = cache.get(otp_cache_key(PAYOUT_CHANGE_OTP_PURPOSE, email))
    if not stored_digest:
        return False
    if verify_otp_code(PAYOUT_CHANGE_OTP_PURPOSE, email, submitted, stored_digest):
        cache.delete(otp_cache_key(PAYOUT_CHANGE_OTP_PURPOSE, email))
        cache.delete(attempts_key)
        return True
    _record_failed_payout_otp_attempt(email)
    return False


class PayoutBankListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Payout Accounts"],
        request=None,
        responses={
            200: inline_serializer(
                name="PayoutBankListResponse",
                fields={
                    "banks": serializers.ListField(
                        child=inline_serializer(
                            name="PayoutBank",
                            fields={
                                "code": serializers.CharField(),
                                "name": serializers.CharField(),
                            },
                        ),
                    ),
                },
            ),
            403: OpenApiResponse(description="An active artisan profile is required."),
            503: OpenApiResponse(description="The bank directory is temporarily unavailable."),
        },
    )
    def get(self, request):
        if not getattr(request.user, "artisan_profile", None):
            raise PermissionDenied("An artisan profile is required for payout account management.")
        try:
            banks = get_supported_nigerian_banks()
        except FlutterwaveUnknownError:
            return Response(
                {"detail": "The supported-bank list is temporarily unavailable from Flutterwave."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except FlutterwaveError:
            return Response(
                {"detail": "The supported-bank list is unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"banks": banks}, status=status.HTTP_200_OK)


class ArtisanPayoutAccountView(APIView):
    permission_classes = [IsAuthenticated]

    def get_artisan(self):
        artisan = getattr(self.request.user, "artisan_profile", None)
        if artisan is None:
            raise PermissionDenied("An artisan profile is required for payout account management.")
        if artisan.profile_status == "SUSPENDED":
            raise PermissionDenied("Suspended artisan profiles cannot manage payout accounts.")
        return artisan

    @extend_schema(
        tags=["Payout Accounts"],
        request=None,
        responses={
            200: ArtisanPayoutAccountSerializer,
            403: OpenApiResponse(description="An eligible artisan profile is required."),
            404: OpenApiResponse(description="Payout account not configured."),
        },
    )
    def get(self, request):
        account = ArtisanPayoutAccount.objects.filter(artisan=self.get_artisan()).first()
        if account is None:
            return Response({"detail": "Payout account not configured."}, status=status.HTTP_404_NOT_FOUND)
        return Response(ArtisanPayoutAccountSerializer(account).data, status=status.HTTP_200_OK)

    @extend_schema(
        tags=["Payout Accounts"],
        request=ArtisanPayoutAccountSerializer,
        responses={
            200: ArtisanPayoutAccountSerializer,
            201: ArtisanPayoutAccountSerializer,
            400: OpenApiResponse(description="Invalid bank, account, or verified-identity details."),
            403: OpenApiResponse(description="The artisan account is not eligible to manage payouts."),
            409: OpenApiResponse(description="The payout account cannot be changed in its current state."),
            503: OpenApiResponse(description="Flutterwave verification is temporarily unavailable."),
        },
    )
    @idempotent_action("payout_account.update", resource_type="ArtisanPayoutAccount", atomic=False)
    def put(self, request):
        return self._save(request)

    @extend_schema(
        tags=["Payout Accounts"],
        request=ArtisanPayoutAccountSerializer,
        responses={
            200: ArtisanPayoutAccountSerializer,
            400: OpenApiResponse(description="Invalid bank, account, or verified-identity details."),
            403: OpenApiResponse(description="The artisan account is not eligible to manage payouts."),
            409: OpenApiResponse(description="The payout account cannot be changed in its current state."),
            503: OpenApiResponse(description="Flutterwave verification is temporarily unavailable."),
        },
    )
    @idempotent_action("payout_account.update", resource_type="ArtisanPayoutAccount", atomic=False)
    def patch(self, request):
        return self._save(request)

    def _save(self, request):
        artisan = self.get_artisan()
        _verification, identity_name = _eligible_payout_identity(artisan)

        serializer = ArtisanPayoutAccountSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        account_number = serializer.validated_data["account_number"]
        submitted_account_name = serializer.validated_data["account_name"]
        bank_code = serializer.validated_data["bank_code"]

        if not account_name_matches_identity(
            submitted_account_name,
            identity_name,
            artisan.user.first_name,
            artisan.user.last_name,
        ):
            raise ValidationError({"account_name": ACCOUNT_NAME_MISMATCH_MESSAGE})

        existing = ArtisanPayoutAccount.objects.filter(artisan=artisan).first()
        if existing and _payout_has_protected_funds(artisan):
            return Response(
                {"detail": "Payout account details cannot be changed while protected funds are active on your account."},
                status=status.HTTP_409_CONFLICT,
            )

        if existing is not None:
            # Changing an existing account is gated twice: a cooldown window
            # after the last change, then a one-time code emailed to the artisan.
            locked_until = _payout_change_locked_until(existing)
            if locked_until is not None:
                return _payout_change_lock_response(locked_until)
            otp_result = _consume_payout_change_otp(
                artisan.user, serializer.validated_data.get("otp")
            )
            if otp_result is None:
                raise ValidationError({"otp": PAYOUT_CHANGE_OTP_REQUIRED_MESSAGE})
            if not otp_result:
                raise ValidationError(
                    {"otp": "That security code is invalid or expired. Request a new code and try again."}
                )

        try:
            banks = get_supported_nigerian_banks()
        except FlutterwaveUnknownError:
            return _provider_error_response(unknown=True)
        except FlutterwaveError:
            return _provider_error_response(unknown=False)

        bank = get_supported_bank(bank_code, banks)
        if bank is None:
            raise ValidationError({"bank_code": "Select a bank supported by Flutterwave."})

        try:
            provider_result = FlutterwaveClient().resolve_account(
                account_number=account_number,
                bank_code=bank_code,
            )
        except FlutterwaveUnknownError:
            return _provider_error_response(unknown=True)
        except FlutterwaveError:
            return _provider_error_response(unknown=False)

        resolved = resolved_account_details(provider_result, account_number)
        if not resolved:
            return _provider_error_response(unknown=False)
        if not account_name_matches_identity(
            resolved["account_name"],
            identity_name,
            artisan.user.first_name,
            artisan.user.last_name,
        ):
            raise ValidationError({"account_name": ACCOUNT_NAME_MISMATCH_MESSAGE})

        response_status = status.HTTP_201_CREATED if existing is None else status.HTTP_200_OK
        with transaction.atomic():
            locked_artisan = (
                ArtisanProfile.objects.select_for_update(of=("self",))
                .select_related("user")
                .get(pk=artisan.pk)
            )
            locked_user = CustomUser.objects.select_for_update(of=("self",)).get(
                pk=locked_artisan.user_id
            )
            locked_verification = (
                ArtisanVerification.objects.select_for_update(of=("self",))
                .filter(artisan_id=locked_artisan.pk)
                .first()
            )
            if (
                locked_artisan.profile_status != "ACTIVE"
                or not locked_verification
                or locked_verification.verification_status != "VERIFIED"
                or not locked_verification.verified_identity_name_encrypted
            ):
                return Response(
                    {"detail": "Your artisan profile must remain active and verified while payout details are saved."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            try:
                latest_identity_name = decrypt_identity_name(
                    locked_verification.verified_identity_name_encrypted
                )
            except ValueError:
                return Response(
                    {"detail": "Please complete identity verification again before setting payout details."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if not profile_name_matches_identity(
                latest_identity_name,
                locked_user.first_name,
                locked_user.last_name,
            ):
                locked_verification.verification_status = "REJECTED"
                locked_verification.verified_identity_name_encrypted = ""
                locked_verification.verified_at = None
                locked_verification.save(update_fields=[
                    "verification_status",
                    "verified_identity_name_encrypted",
                    "verified_at",
                    "updated_at",
                ])
                return Response(
                    {"detail": PROFILE_NAME_CORRECTION_MESSAGE},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if _payout_has_protected_funds(locked_artisan):
                return Response(
                    {"detail": "Payout account details cannot be changed while protected funds are active on your account."},
                    status=status.HTTP_409_CONFLICT,
                )

            account = (
                ArtisanPayoutAccount.objects.select_for_update(of=("self",))
                .filter(artisan=locked_artisan)
                .first()
            )
            created = account is None
            if account is None:
                account = ArtisanPayoutAccount(artisan=locked_artisan)
            account.bank_code = bank_code
            account.bank_name = bank["name"]
            account.account_name = resolved["account_name"]
            account.account_number_last4 = account_number[-4:]
            account.account_number_encrypted = encrypt_value(account_number)
            account.status = ArtisanPayoutAccount.Status.PENDING
            account.is_active = False
            account.is_verified = False
            account.provider_beneficiary_id = None
            account.provider_subaccount_id = ""
            account.payout_change_locked_until = timezone.now() + PAYOUT_CHANGE_LOCK_DURATION
            account.save()
            response_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK

        response_data = ArtisanPayoutAccountSerializer(account).data
        response_data["message"] = (
            "Flutterwave confirmed the bank account name. The account is pending protected finance verification; payouts are not enabled yet."
        )
        return Response(response_data, status=response_status)


class ArtisanPayoutChangeOTPView(APIView):
    """Issue the one-time code that authorises replacing a saved payout account."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Payout Accounts"],
        request=None,
        responses={
            202: OpenApiResponse(description="A security code was emailed to the artisan."),
            400: OpenApiResponse(description="No saved payout account exists to change."),
            403: OpenApiResponse(description="An eligible artisan profile is required."),
            423: OpenApiResponse(description="Payout changes are still locked from the last change."),
            429: OpenApiResponse(description="A code was sent recently; wait before resending."),
        },
    )
    def post(self, request):
        artisan = getattr(request.user, "artisan_profile", None)
        if artisan is None:
            raise PermissionDenied("An artisan profile is required for payout account management.")
        if artisan.profile_status == "SUSPENDED":
            raise PermissionDenied("Suspended artisan profiles cannot manage payout accounts.")

        account = ArtisanPayoutAccount.objects.filter(artisan=artisan).first()
        if account is None:
            return Response(
                {"detail": "Save your first payout account without a code. A security code is only needed to replace saved details."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        locked_until = _payout_change_locked_until(account)
        if locked_until is not None:
            return _payout_change_lock_response(locked_until)

        email = (getattr(request.user, "email", "") or "").strip()
        if not email:
            return Response(
                {"detail": "Add an email address to your account before changing payout details."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.now()
        cooldown_key = otp_cooldown_cache_key(PAYOUT_CHANGE_OTP_PURPOSE, email)
        cooldown_until = cache.get(cooldown_key)
        if cooldown_until and cooldown_until > now:
            retry_after = max(1, int((cooldown_until - now).total_seconds()))
            return Response(
                {
                    "detail": "A security code was already sent. Check your email or wait a moment before requesting another.",
                    "retry_after_seconds": retry_after,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        code = f"{secrets.randbelow(900000) + 100000:06d}"
        cache.set(
            otp_cache_key(PAYOUT_CHANGE_OTP_PURPOSE, email),
            hash_otp_code(PAYOUT_CHANGE_OTP_PURPOSE, email, code),
            timeout=PAYOUT_CHANGE_OTP_TTL_SECONDS,
        )
        cache.set(
            otp_attempts_cache_key(PAYOUT_CHANGE_OTP_PURPOSE, email),
            0,
            timeout=PAYOUT_CHANGE_OTP_TTL_SECONDS,
        )
        cache.set(
            cooldown_key,
            now + timedelta(seconds=PAYOUT_CHANGE_OTP_RESEND_SECONDS),
            timeout=PAYOUT_CHANGE_OTP_RESEND_SECONDS,
        )
        try:
            send_payout_change_email.delay(
                request.user.first_name, email, encrypt_email_code(code)
            )
        except Exception:
            # Never turn a committed action into a 500 because the broker is down;
            # never log the code, email or ciphertext.
            logger.exception("payout_change_email_enqueue_failed user_id=%s", request.user.pk)

        audit_request_action(
            request=request,
            action="PAYOUT_CHANGE_OTP_REQUESTED",
            resource_type="ArtisanPayoutAccount",
            resource_id=str(account.pk),
            metadata={"expires_in_seconds": PAYOUT_CHANGE_OTP_TTL_SECONDS},
        )
        return Response(
            {
                "detail": "A 6-digit security code was sent to your email. It expires in 10 minutes.",
                "expires_in_seconds": PAYOUT_CHANGE_OTP_TTL_SECONDS,
                "resend_after_seconds": PAYOUT_CHANGE_OTP_RESEND_SECONDS,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class ArtisanPayoutAccountStatusView(APIView):
    """Protected finance action: re-resolve the account and create a provider beneficiary before activation."""
    permission_classes = [SupportFinancePermission]

    @extend_schema(
        tags=["Payout Accounts"],
        description=(
            "Re-verifies a pending payout account with Flutterwave, checks the provider-returned name against "
            "the encrypted identity name and current artisan profile, then creates the provider beneficiary. "
            "The account is activated only if every provider response matches."
        ),
        request=None,
        parameters=[OpenApiParameter("Idempotency-Key", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True)],
        responses={
            200: ArtisanPayoutAccountSerializer,
            400: OpenApiResponse(description="The provider could not verify the account."),
            403: OpenApiResponse(description="Finance permissions or artisan eligibility are missing."),
            404: OpenApiResponse(description="Payout account not found."),
            409: OpenApiResponse(description="The account is not pending or changed during verification."),
            503: OpenApiResponse(description="Flutterwave verification is temporarily unavailable."),
        },
    )
    @idempotent_action("payout_account.verify", resource_type="ArtisanPayoutAccount", atomic=False)
    def post(self, request, pk):
        account = (
            ArtisanPayoutAccount.objects.select_related("artisan__user")
            .filter(artisan_id=pk)
            .first()
        )
        if account is None:
            return Response({"detail": "Payout account not found."}, status=status.HTTP_404_NOT_FOUND)
        if (
            account.status == ArtisanPayoutAccount.Status.VERIFIED
            and account.is_verified
            and account.is_active
            and account.provider_beneficiary_id
        ):
            return Response(ArtisanPayoutAccountSerializer(account).data, status=status.HTTP_200_OK)
        if account.status != ArtisanPayoutAccount.Status.PENDING:
            return Response({"detail": "Only a pending payout account can be verified."}, status=status.HTTP_409_CONFLICT)
        if not account.account_number_encrypted:
            return Response(
                {"detail": "The pending account number is unavailable. Ask the artisan to submit the payout details again."},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            _verification, identity_name = _eligible_payout_identity(account.artisan)
        except PermissionDenied as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)

        lock_key = f"payout_provider_verify:{account.artisan_id}"
        lock_token = uuid.uuid4().hex
        if not cache.add(lock_key, lock_token, timeout=180):
            return Response(
                {"detail": "Payout account verification is already in progress. Please retry shortly."},
                status=status.HTTP_409_CONFLICT,
            )

        encrypted_number = account.account_number_encrypted
        bank_code = account.bank_code
        stored_account_name = account.account_name
        account_updated_at = account.updated_at
        artisan_id = account.artisan_id
        account_number = ""
        try:
            try:
                account_number = decrypt_value(encrypted_number)
            except ValueError:
                return Response(
                    {"detail": "The encrypted account number could not be read. Ask the artisan to submit the details again."},
                    status=status.HTTP_409_CONFLICT,
                )

            try:
                banks = get_supported_nigerian_banks()
                bank = get_supported_bank(bank_code, banks)
                if bank is None:
                    return Response(
                        {"detail": "This bank is no longer supported by Flutterwave. Ask the artisan to select a supported bank."},
                        status=status.HTTP_409_CONFLICT,
                    )
                provider = FlutterwaveClient()
                resolved_result = provider.resolve_account(
                    account_number=account_number,
                    bank_code=bank_code,
                )
                resolved = resolved_account_details(resolved_result, account_number)
                if not resolved:
                    return _provider_error_response(verification_step=True, unknown=False)
                user = CustomUser.objects.get(pk=account.artisan.user_id)
                if (
                    not account_name_matches_identity(
                        stored_account_name,
                        identity_name,
                        user.first_name,
                        user.last_name,
                    )
                    or not account_name_matches_identity(
                        resolved["account_name"],
                        identity_name,
                        user.first_name,
                        user.last_name,
                    )
                ):
                    return Response(
                        {"detail": "The provider account name no longer matches the verified identity and artisan profile. The payout account remains pending."},
                        status=status.HTTP_409_CONFLICT,
                    )
                beneficiary_result = provider.create_beneficiary(
                    account_number=account_number,
                    bank_code=bank_code,
                    beneficiary_name=resolved["account_name"],
                    bank_name=bank["name"],
                )
            except FlutterwaveUnknownError:
                return _provider_error_response(verification_step=True, unknown=True)
            except FlutterwaveError:
                return _provider_error_response(verification_step=True, unknown=False)

            beneficiary_data = beneficiary_result.get("data") if isinstance(beneficiary_result, dict) else None
            if not isinstance(beneficiary_data, dict):
                return _provider_error_response(verification_step=True, unknown=False)
            beneficiary_id_raw = beneficiary_data.get("id")
            beneficiary_id_text = str(beneficiary_id_raw or "").strip()
            beneficiary_number = str(beneficiary_data.get("account_number") or "").strip()
            beneficiary_bank_code = str(
                beneficiary_data.get("bank_code") or beneficiary_data.get("account_bank") or ""
            ).strip()
            beneficiary_name = beneficiary_data.get("full_name") or beneficiary_data.get("beneficiary_name")
            if (
                not beneficiary_id_text.isascii()
                or not beneficiary_id_text.isdigit()
                or beneficiary_number != account_number
                or beneficiary_bank_code != bank_code
                or not isinstance(beneficiary_name, str)
                or not account_name_matches_identity(
                    beneficiary_name,
                    identity_name,
                    user.first_name,
                    user.last_name,
                )
            ):
                return _provider_error_response(verification_step=True, unknown=False)

            with transaction.atomic():
                locked_artisan = (
                    ArtisanProfile.objects.select_for_update(of=("self",))
                    .select_related("user")
                    .get(pk=artisan_id)
                )
                locked_user = CustomUser.objects.select_for_update(of=("self",)).get(
                    pk=locked_artisan.user_id
                )
                locked_verification = (
                    ArtisanVerification.objects.select_for_update(of=("self",))
                    .filter(artisan_id=artisan_id)
                    .first()
                )
                locked_account = (
                    ArtisanPayoutAccount.objects.select_for_update(of=("self",))
                    .filter(artisan_id=artisan_id)
                    .first()
                )
                if locked_account is None:
                    return Response({"detail": "Payout account not found."}, status=status.HTTP_404_NOT_FOUND)
                if (
                    locked_account.status != ArtisanPayoutAccount.Status.PENDING
                    or locked_account.updated_at != account_updated_at
                    or locked_account.account_number_encrypted != encrypted_number
                    or locked_account.bank_code != bank_code
                    or locked_account.account_name != stored_account_name
                ):
                    return Response(
                        {"detail": "The payout account changed during verification. Please review the current details and try again."},
                        status=status.HTTP_409_CONFLICT,
                    )
                if (
                    locked_artisan.profile_status != "ACTIVE"
                    or not locked_verification
                    or locked_verification.verification_status != "VERIFIED"
                    or not locked_verification.verified_identity_name_encrypted
                ):
                    return Response(
                        {"detail": "The artisan must remain active and identity-verified before the payout account can be activated."},
                        status=status.HTTP_403_FORBIDDEN,
                    )
                try:
                    latest_identity_name = decrypt_identity_name(
                        locked_verification.verified_identity_name_encrypted
                    )
                except ValueError:
                    return Response(
                        {"detail": "The verified identity name is unavailable. The payout account remains pending."},
                        status=status.HTTP_403_FORBIDDEN,
                    )
                if not profile_name_matches_identity(
                    latest_identity_name,
                    locked_user.first_name,
                    locked_user.last_name,
                ):
                    locked_verification.verification_status = "REJECTED"
                    locked_verification.verified_identity_name_encrypted = ""
                    locked_verification.verified_at = None
                    locked_verification.save(update_fields=[
                        "verification_status",
                        "verified_identity_name_encrypted",
                        "verified_at",
                        "updated_at",
                    ])
                    return Response(
                        {"detail": PROFILE_NAME_CORRECTION_MESSAGE},
                        status=status.HTTP_403_FORBIDDEN,
                    )
                if _payout_has_protected_funds(locked_artisan):
                    return Response(
                        {"detail": "Payout account verification cannot be completed while protected funds are active."},
                        status=status.HTTP_409_CONFLICT,
                    )

                locked_account.status = ArtisanPayoutAccount.Status.VERIFIED
                locked_account.is_verified = True
                locked_account.is_active = True
                locked_account.provider_beneficiary_id = int(beneficiary_id_text)
                locked_account.account_number_encrypted = ""
                locked_account.save(update_fields=[
                    "status",
                    "is_verified",
                    "is_active",
                    "provider_beneficiary_id",
                    "account_number_encrypted",
                    "updated_at",
                ])

            response_data = ArtisanPayoutAccountSerializer(locked_account).data
            response_data["message"] = "Flutterwave verified the payout account and the account has been activated."
            return Response(response_data, status=status.HTTP_200_OK)
        finally:
            account_number = ""
            if cache.get(lock_key) == lock_token:
                cache.delete(lock_key)
