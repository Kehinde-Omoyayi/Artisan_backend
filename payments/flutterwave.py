
from decimal import Decimal
import logging
import re

import requests

from django.conf import settings
from django.utils import timezone


logger = logging.getLogger(__name__)

PAYMENT_OPTIONS = "card, banktransfer"


class FlutterwaveError(Exception):
    """Definite provider or request rejection."""

    def __init__(self, message, *, status_code=None, provider_code=None):
        super().__init__(message)
        self.status_code = status_code
        self.provider_code = provider_code


class FlutterwaveUnknownError(Exception):
    """The provider request outcome cannot yet be established."""


def _naira(value):
    """Format NGN amounts without unnecessary decimal places."""
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    return (
        int(amount)
        if amount == amount.to_integral_value()
        else float(amount)
    )


def _provider_id(value):
    """Validate numeric provider identifiers."""
    text = str(value).strip()

    if not text.isascii() or not text.isdigit():
        raise FlutterwaveError("Invalid provider identifier.")

    return int(text)


def _safe_provider_message(
    message,
    *,
    account_number=None,
    beneficiary_name=None,
):
    """Redact account and identity details from provider errors."""
    text = str(message or "Flutterwave request failed.")

    for sensitive in (account_number, beneficiary_name):
        sensitive = str(sensitive or "").strip()

        if sensitive:
            text = re.sub(
                re.escape(sensitive),
                "[REDACTED]",
                text,
                flags=re.IGNORECASE,
            )

    text = re.sub(
        r"(?<!\d)\d{7,}(?!\d)",
        "[REDACTED]",
        text,
    )

    return text[:240]


def _is_duplicate_beneficiary_error(exc):
    """Recognize Flutterwave's duplicate-beneficiary rejection."""
    message = str(exc).casefold()

    duplicate_messages = (
        "beneficiary already added",
        "beneficiary already exists",
        "already added to your account",
        "recipient already exists",
    )

    return any(
        phrase in message
        for phrase in duplicate_messages
    )


class FlutterwaveClient:

    def __init__(self):
        self.base_url = settings.FLW_API_BASE_URL.rstrip("/")
        self.secret_key = getattr(settings, "FLW_SECRET_KEY", "")

        if not self.secret_key:
            raise FlutterwaveError("Flutterwave is not configured.")

        self.timeout = max(
            3,
            min(
                int(getattr(settings, "FLW_TIMEOUT_SECONDS", 15)),
                30,
            ),
        )

        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _request(self, method, path, **kwargs):
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                timeout=self.timeout,
                **kwargs,
            )

        except (requests.Timeout, requests.ConnectionError) as exc:
            raise FlutterwaveUnknownError(
                "Flutterwave request outcome is unknown."
            ) from exc

        except requests.RequestException as exc:
            raise FlutterwaveError(
                "Unable to connect to Flutterwave."
            ) from exc

        if response.status_code == 408 or response.status_code >= 500:
            raise FlutterwaveUnknownError(
                "Flutterwave server error; outcome is unknown."
            )

        if response.status_code == 429:
            raise FlutterwaveUnknownError(
                "Flutterwave rate limit reached; reconcile before retrying."
            )

        try:
            payload = response.json()

        except ValueError as exc:
            raise FlutterwaveError(
                "Flutterwave returned an invalid response.",
                status_code=response.status_code,
            ) from exc

        if not isinstance(payload, dict):
            raise FlutterwaveError(
                "Flutterwave returned an invalid response.",
                status_code=response.status_code,
            )

        provider_code = payload.get("code")

        data = payload.get("data")
        if provider_code is None and isinstance(data, dict):
            provider_code = data.get("code")

        if (
            not response.ok
            or str(payload.get("status") or "").casefold() != "success"
        ):
            message = payload.get("message")

            if not isinstance(message, str) or not message.strip():
                message = "Flutterwave request failed."

            raise FlutterwaveError(
                message,
                status_code=response.status_code,
                provider_code=provider_code,
            )

        return payload

    # =========================================================
    # CUSTOMER PAYMENTS
    # =========================================================

    def initialize_payment(
        self,
        *,
        reference,
        amount_ngn,
        customer,
        booking_id,
        addon_order_id=None,
        booking_label=None,
    ):
        is_addon = addon_order_id is not None
        label = (booking_label or f"booking {booking_id}").strip()

        payload = {
            "tx_ref": reference,
            "amount": _naira(amount_ngn),
            "currency": "NGN",
            "country": "NG",
            "payment_options": PAYMENT_OPTIONS,
            "redirect_url": settings.FLW_REDIRECT_URL,
            "customer": {
                "email": customer.email,
                "name": customer.get_full_name() or customer.email,
            },
            "meta": {
                "booking_id": str(booking_id),
                "payment_reference": reference,
                **(
                    {"additional_order_id": str(addon_order_id)}
                    if is_addon else {}
                ),
            },
            "customizations": {
                "title": settings.FLW_PAYMENT_TITLE,
                "description": (
                    f"Payment for additional order {addon_order_id} "
                    f"on {label}"
                    if is_addon
                    else f"Payment for {label}"
                ),
            },
        }

        return self._request("POST", "/payments", json=payload)

    def initialize_promotion_payment(
        self,
        *,
        reference,
        amount_ngn,
        customer,
        promotion_payment_id,
        campaign_id,
        promotion_type,
        target_key,
        target_label,
    ):
        """Initialize a separate platform promotion payment."""
        payload = {
            "tx_ref": reference,
            "amount": _naira(amount_ngn),
            "currency": "NGN",
            "country": "NG",
            "payment_options": PAYMENT_OPTIONS,
            "redirect_url": settings.FLW_REDIRECT_URL,
            "customer": {
                "email": customer.email,
                "name": customer.get_full_name() or customer.email,
            },
            "meta": {
                "payment_kind": "promotion_campaign",
                "promotion_payment_id": str(promotion_payment_id),
                "promotion_campaign_id": str(campaign_id),
                "promotion_type": str(promotion_type),
                "target_key": str(target_key),
            },
            "customizations": {
                "title": settings.FLW_PAYMENT_TITLE,
                "description": (
                    f"{promotion_type.replace('_', ' ').title()} "
                    f"promotion for {target_label}"
                ),
            },
        }

        return self._request("POST", "/payments", json=payload)

    def create_bank_transfer_charge(
        self,
        *,
        reference,
        amount_ngn,
        customer,
        booking_id,
        booking_label=None,
    ):
        """Create a dedicated bank-transfer payment account."""
        label = (booking_label or f"booking {booking_id}").strip()

        payload = {
            "tx_ref": reference,
            "amount": _naira(amount_ngn),
            "currency": "NGN",
            "country": "NG",
            "email": customer.email,
            "fullname": customer.get_full_name() or customer.email,
            "narration": label[:100],
            "meta": {
                "booking_id": str(booking_id),
                "payment_reference": reference,
            },
            "bank_transfer_options": {
                "expires": 3600,
            },
        }

        return self._request(
            "POST",
            "/charges?type=bank_transfer",
            json=payload,
        )

    # =========================================================
    # PAYMENT VERIFICATION
    # =========================================================

    def verify_transaction_by_reference(self, reference):
        return self._request(
            "GET",
            "/transactions/verify_by_reference",
            params={"tx_ref": str(reference)},
        )

    def verify_transaction(self, transaction_id):
        return self._request(
            "GET",
            f"/transactions/{_provider_id(transaction_id)}/verify",
        )

    def list_transactions(self, *, tx_ref, from_date, to_date):
        return self._request(
            "GET",
            "/transactions",
            params={
                "tx_ref": tx_ref,
                "from": from_date,
                "to": to_date,
            },
        )

    # =========================================================
    # BANK DIRECTORY
    # =========================================================

    def list_banks(self, country="NG"):
        country = str(country or "NG").upper()

        if country != "NG":
            raise FlutterwaveError(
                "Only Nigerian bank accounts are supported."
            )

        try:
            return self._request(
                "GET",
                "/banks/NG",
                params={"include_provider_type": 1},
            )

        except FlutterwaveUnknownError:
            raise

        except FlutterwaveError as exc:
            logger.warning(
                "FLW_BANK_LIST_FAILED http_status=%s",
                exc.status_code,
            )

            raise FlutterwaveError(
                "Unable to retrieve supported banks from Flutterwave.",
                status_code=exc.status_code,
                provider_code=exc.provider_code,
            ) from exc

    # =========================================================
    # BANK ACCOUNT RESOLUTION
    # =========================================================

    def resolve_account(self, *, account_number, bank_code):
        account_number = str(account_number or "").strip()
        bank_code = str(bank_code or "").strip()

        if (
            len(account_number) != 10
            or not account_number.isascii()
            or not account_number.isdigit()
        ):
            raise FlutterwaveError(
                "Unable to verify the bank account details."
            )

        if not bank_code.isascii() or not bank_code.isdigit():
            raise FlutterwaveError(
                "Unable to verify the bank account details."
            )

        try:
            return self._request(
                "POST",
                "/accounts/resolve",
                json={
                    "account_number": account_number,
                    "account_bank": bank_code,
                },
            )

        except FlutterwaveUnknownError:
            raise

        except FlutterwaveError as exc:
            reason = _safe_provider_message(
                exc,
                account_number=account_number,
            )

            logger.warning(
                "FLW_ACCOUNT_RESOLUTION_REJECTION "
                "http_status=%s provider_code=%s reason=%s",
                exc.status_code,
                exc.provider_code,
                reason,
            )

            raise FlutterwaveError(
                "Unable to verify the bank account details.",
                status_code=exc.status_code,
                provider_code=exc.provider_code,
            ) from exc

    # =========================================================
    # BENEFICIARY LOOKUP
    # =========================================================

    def find_existing_beneficiary(
        self,
        *,
        account_number,
        bank_code,
    ):
        """
        Find an existing Flutterwave beneficiary by exact bank details.

        Flutterwave returns beneficiaries in paginated responses.
        Never reuse a beneficiary based on name alone.
        """
        account_number = str(account_number or "").strip()
        bank_code = str(bank_code or "").strip()

        if (
            len(account_number) != 10
            or not account_number.isascii()
            or not account_number.isdigit()
        ):
            raise FlutterwaveError(
                "Invalid beneficiary account number."
            )

        if not bank_code.isascii() or not bank_code.isdigit():
            raise FlutterwaveError(
                "Invalid beneficiary bank code."
            )

        page = 1
        total_pages = None

        while total_pages is None or page <= total_pages:
            result = self._request(
                "GET",
                "/beneficiaries",
                params={"page": page},
            )

            beneficiaries = result.get("data")

            if not isinstance(beneficiaries, list):
                raise FlutterwaveError(
                    "Flutterwave returned an invalid beneficiary list."
                )

            for beneficiary in beneficiaries:
                if not isinstance(beneficiary, dict):
                    continue

                existing_number = str(
                    beneficiary.get("account_number") or ""
                ).strip()

                existing_bank = str(
                    beneficiary.get("bank_code")
                    or beneficiary.get("account_bank")
                    or ""
                ).strip()

                if (
                    existing_number == account_number
                    and existing_bank == bank_code
                ):
                    logger.info(
                        "FLW_EXISTING_BENEFICIARY_EXACT_MATCH page=%s",
                        page,
                    )

                    return beneficiary

            meta = result.get("meta")

            if not isinstance(meta, dict):
                raise FlutterwaveError(
                    "Flutterwave returned invalid beneficiary pagination."
                )

            page_info = meta.get("page_info")

            if not isinstance(page_info, dict):
                raise FlutterwaveError(
                    "Flutterwave returned invalid beneficiary pagination."
                )

            try:
                reported_pages = int(
                    page_info.get("total_pages")
                )
            except (TypeError, ValueError):
                raise FlutterwaveError(
                    "Flutterwave returned invalid beneficiary pagination."
                )

            if reported_pages < 0 or reported_pages > 1000:
                raise FlutterwaveError(
                    "Flutterwave returned invalid beneficiary pagination."
                )

            total_pages = max(1, reported_pages)

            if page >= total_pages:
                break

            page += 1

        logger.info(
            "FLW_EXISTING_BENEFICIARY_NOT_FOUND"
        )

        return None

    # =========================================================
    # BENEFICIARY CREATION AND DUPLICATE RECOVERY
    # =========================================================

    def create_beneficiary(
        self,
        *,
        account_number,
        bank_code,
        beneficiary_name,
        bank_name,
    ):
        """
        Create a Flutterwave beneficiary or safely reuse an exact existing one.

        Existing beneficiaries are reused only when both the bank code and
        full account number match. The calling view must still validate the
        returned beneficiary ID, account number, bank code and account name.
        """
        account_number = str(account_number or "").strip()
        bank_code = str(bank_code or "").strip()
        beneficiary_name = str(beneficiary_name or "").strip()
        bank_name = str(bank_name or "").strip()

        if (
            len(account_number) != 10
            or not account_number.isascii()
            or not account_number.isdigit()
        ):
            raise FlutterwaveError(
                "Invalid beneficiary account number."
            )

        if not bank_code.isascii() or not bank_code.isdigit():
            raise FlutterwaveError(
                "Invalid beneficiary bank code."
            )

        if not beneficiary_name or not bank_name:
            raise FlutterwaveError(
                "Beneficiary name and bank name are required."
            )

        payload = {
            "account_bank": bank_code,
            "account_number": account_number,
            "beneficiary_name": beneficiary_name,
            "currency": "NGN",
            "bank_name": bank_name,
        }

        try:
            result = self._request(
                "POST",
                "/beneficiaries",
                json=payload,
            )

            logger.info(
                "FLW_BENEFICIARY_CREATION_ACCEPTED"
            )

            return result

        except FlutterwaveUnknownError:
            # An ambiguous POST must not be retried blindly.
            # The provider may already have created the beneficiary.
            logger.warning(
                "FLW_BENEFICIARY_CREATION_UNKNOWN"
            )
            raise

        except FlutterwaveError as exc:
            if not _is_duplicate_beneficiary_error(exc):
                reason = _safe_provider_message(
                    exc,
                    account_number=account_number,
                    beneficiary_name=beneficiary_name,
                )

                logger.warning(
                    "FLW_BENEFICIARY_REJECTION "
                    "http_status=%s provider_code=%s reason=%s",
                    exc.status_code,
                    exc.provider_code,
                    reason,
                )

                raise FlutterwaveError(
                    "Unable to complete payout beneficiary verification.",
                    status_code=exc.status_code,
                    provider_code=exc.provider_code,
                ) from exc

            logger.info(
                "FLW_DUPLICATE_BENEFICIARY_LOOKUP_STARTED"
            )

            try:
                existing = self.find_existing_beneficiary(
                    account_number=account_number,
                    bank_code=bank_code,
                )

            except FlutterwaveUnknownError:
                raise

            except FlutterwaveError as lookup_error:
                logger.warning(
                    "FLW_EXISTING_BENEFICIARY_LOOKUP_FAILED "
                    "http_status=%s",
                    lookup_error.status_code,
                )

                raise FlutterwaveError(
                    "Existing beneficiary could not be safely verified.",
                    status_code=lookup_error.status_code,
                    provider_code=lookup_error.provider_code,
                ) from lookup_error

            if existing is None:
                logger.warning(
                    "FLW_DUPLICATE_BENEFICIARY_NOT_FOUND"
                )

                raise FlutterwaveError(
                    "Flutterwave reported an existing beneficiary, "
                    "but no exact bank account match was found."
                ) from exc

            logger.info(
                "FLW_EXISTING_BENEFICIARY_REUSED"
            )

            return {
                "status": "success",
                "message": "Existing beneficiary reused.",
                "data": existing,
            }

    # =========================================================
    # TRANSFERS
    # =========================================================

    def create_transfer(
        self,
        *,
        amount_ngn,
        beneficiary_id,
        reference,
        narration,
        callback_url,
    ):
        payload = {
            "amount": _naira(amount_ngn),
            "beneficiary": _provider_id(beneficiary_id),
            "currency": "NGN",
            "reference": reference,
            "narration": narration,
            "callback_url": callback_url,
        }

        return self._request(
            "POST",
            "/transfers",
            json=payload,
        )

    def get_transfer(self, transfer_id):
        return self._request(
            "GET",
            f"/transfers/{_provider_id(transfer_id)}",
        )

    def get_transfer_by_reference(self, reference):
        return self._request(
            "GET",
            "/transfers",
            params={
                "reference": reference,
                "page": 1,
                "page_size": 10,
            },
        )

    # =========================================================
    # REFUNDS
    # =========================================================

    def refund_transaction(
        self,
        *,
        transaction_id,
        amount_kobo,
        comments,
        callback_url=None,
    ):
        payload = {
            "amount": int(amount_kobo // 100),
            "comments": comments,
        }

        if callback_url:
            payload["callbackurl"] = callback_url

        return self._request(
            "POST",
            f"/transactions/{_provider_id(transaction_id)}/refund",
            json=payload,
        )

    def find_refund(
        self,
        *,
        transaction_id,
        provider_ref,
        amount_kobo,
        minimum_date=None,
    ):
        start = minimum_date or timezone.now()

        params = {
            "from": start.date().isoformat(),
            "to": timezone.now().date().isoformat(),
            "currency": "NGN",
        }

        if transaction_id:
            params["id"] = str(transaction_id)

        if provider_ref:
            params["flw_ref"] = str(provider_ref)

        result = self._request(
            "GET",
            "/refunds",
            params=params,
        )

        raw = result.get("data") or []
        candidates = raw if isinstance(raw, list) else [raw]

        expected_amount = int(amount_kobo // 100)
        expected_tx = str(transaction_id or "")
        expected_ref = str(provider_ref or "")

        for item in candidates:
            if not isinstance(item, dict):
                continue

            item_tx = str(
                item.get("tx_id")
                or item.get("TransactionId")
                or item.get("transaction_id")
                or ""
            )

            item_ref = str(
                item.get("flw_ref")
                or item.get("FlwRef")
                or item.get("reference")
                or ""
            )

            item_amount = item.get("amount_refunded")

            if item_amount is None:
                item_amount = item.get("AmountRefunded")

            if item_amount is None:
                item_amount = item.get("amount")

            try:
                normalized_amount = int(Decimal(str(item_amount)))
            except (TypeError, ValueError, ArithmeticError):
                normalized_amount = None

            tx_matches = (
                not expected_tx or item_tx == expected_tx
            )

            ref_matches = (
                not expected_ref or item_ref == expected_ref
            )

            if (
                tx_matches
                and ref_matches
                and normalized_amount == expected_amount
            ):
                return {
                    "status": "success",
                    "data": item,
                }

        return {
            "status": "success",
            "data": None,
        }
