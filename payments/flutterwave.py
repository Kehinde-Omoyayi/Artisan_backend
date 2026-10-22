from decimal import Decimal

import requests

from django.conf import settings
from django.utils import timezone

class FlutterwaveError(Exception):
    pass

class FlutterwaveUnknownError(Exception):
    pass

def _naira(value):
    """JSON amount in NGN with kobo precision: whole naira stay integers, fractions are kept exactly."""
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def _provider_id(value):
    """Provider identifiers are numeric; anything else must never be interpolated into an API path."""
    text = str(value).strip()
    if not text.isascii() or not text.isdigit():
        raise FlutterwaveError("Invalid provider identifier.")
    return int(text)


class FlutterwaveClient:
    def __init__(self):
        self.base_url = settings.FLW_API_BASE_URL.rstrip("/")
        self.secret_key = getattr(settings, "FLW_SECRET_KEY", "")
        if not self.secret_key:
            raise FlutterwaveError("Flutterwave is not configured.")
        self.timeout = max(3, min(int(getattr(settings, "FLW_TIMEOUT_SECONDS", 15)), 30))
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json",
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
            raise FlutterwaveUnknownError("Flutterwave request outcome is unknown.") from exc
        except requests.RequestException as exc:
            raise FlutterwaveError("Unable to connect to Flutterwave.") from exc

        if response.status_code == 408 or response.status_code >= 500:
            raise FlutterwaveUnknownError("Flutterwave server error; outcome is unknown.")

        try:
            payload = response.json()
        except ValueError as exc:
            raise FlutterwaveError("Flutterwave returned an invalid response.") from exc

        if response.status_code == 429:
            raise FlutterwaveUnknownError("Flutterwave rate limit reached; payment outcome must be reconciled before retrying.")
        if not response.ok or payload.get("status") != "success":
            raise FlutterwaveError(payload.get("message", "Flutterwave request failed."))
        return payload

    def initialize_payment(self, *, reference, amount_ngn, customer, booking_id, addon_order_id=None):
        is_addon = addon_order_id is not None
        payload = {
            "tx_ref": reference,
            "amount": _naira(amount_ngn),
            "currency": "NGN",
            "redirect_url": settings.FLW_REDIRECT_URL,
            "customer": {
                "email": customer.email,
                "name": customer.get_full_name() or customer.email,
            },
            "meta": {
                "booking_id": str(booking_id),
                "payment_reference": reference,
                **({"additional_order_id": str(addon_order_id)} if is_addon else {}),
            },
            "customizations": {
                "title": settings.FLW_PAYMENT_TITLE,
                "description": (f"Payment for additional order {addon_order_id} on booking {booking_id}" if is_addon else f"Payment for booking {booking_id}"),
            },
        }
        return self._request("POST", "/payments", json=payload)

    def initialize_promotion_payment(self, *, reference, amount_ngn, customer, promotion_payment_id, campaign_id, promotion_type, target_key, target_label):
        """Initialize a separate platform promotion fee; it is not a booking escrow payment."""
        payload = {
            "tx_ref": reference,
            "amount": _naira(amount_ngn),
            "currency": "NGN",
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
                "description": f"{promotion_type.replace('_', ' ').title()} promotion for {target_label}",
            },
        }
        return self._request("POST", "/payments", json=payload)

    def verify_transaction(self, transaction_id):
        return self._request("GET", f"/transactions/{_provider_id(transaction_id)}/verify")

    def list_transactions(self, *, tx_ref, from_date, to_date):
        return self._request("GET", "/transactions", params={"tx_ref": tx_ref, "from": from_date, "to": to_date})

    def list_banks(self, country="NG"):
        country = str(country or "NG").upper()
        if country != "NG":
            raise FlutterwaveError("Only Nigerian bank accounts are supported.")
        try:
            return self._request(
                "GET",
                "/banks/NG",
                params={"include_provider_type": 1},
            )
        except FlutterwaveUnknownError:
            raise
        except FlutterwaveError as exc:
            raise FlutterwaveError("Unable to retrieve supported banks from Flutterwave.") from exc

    def resolve_account(self, *, account_number, bank_code):
        account_number = str(account_number or "")
        bank_code = str(bank_code or "")
        if len(account_number) != 10 or not account_number.isascii() or not account_number.isdigit():
            raise FlutterwaveError("Unable to verify the bank account details.")
        if not bank_code.isascii() or not bank_code.isdigit():
            raise FlutterwaveError("Unable to verify the bank account details.")
        try:
            return self._request(
                "POST",
                "/accounts/resolve",
                json={"account_number": account_number, "account_bank": bank_code},
            )
        except FlutterwaveUnknownError:
            raise
        except FlutterwaveError as exc:
            # Provider messages can contain submitted account details; do not propagate them.
            # Log real provider message for debugging (never expose raw to client in prod)
            import logging; logging.getLogger(__name__).error("Flutterwave resolve failed: %s", exc)
            raise FlutterwaveError("Unable to verify the bank account details.") from exc

    def create_beneficiary(self, *, account_number, bank_code, beneficiary_name, bank_name):
        payload = {
            "account_bank": str(bank_code),
            "account_number": str(account_number),
            "beneficiary_name": str(beneficiary_name),
            "currency": "NGN",
            "bank_name": str(bank_name),
        }
        try:
            return self._request("POST", "/beneficiaries", json=payload)
        except FlutterwaveUnknownError:
            raise
        except FlutterwaveError as exc:
            # Never surface an untrusted provider response that may echo private fields.
            raise FlutterwaveError("Unable to complete payout beneficiary verification.") from exc

    def create_transfer(self, *, amount_ngn, beneficiary_id, reference, narration, callback_url):
        payload = {
            "amount": _naira(amount_ngn),
            "beneficiary": int(beneficiary_id),
            "currency": "NGN",
            "reference": reference,
            "narration": narration,
            "callback_url": callback_url,
        }
        return self._request("POST", "/transfers", json=payload)

    def get_transfer(self, transfer_id):
        return self._request("GET", f"/transfers/{_provider_id(transfer_id)}")

    def get_transfer_by_reference(self, reference):
        return self._request(
            "GET",
            "/transfers",
            params={"reference": reference, "page": 1, "page_size": 10},
        )

    def refund_transaction(self, *, transaction_id, amount_kobo, comments, callback_url=None):
        payload = {
            "amount": int(amount_kobo // 100),
            "comments": comments,
        }
        if callback_url:
            payload["callbackurl"] = callback_url
        return self._request(
            "POST",
            f"/transactions/{int(transaction_id)}/refund",
            json=payload,
        )

    def find_refund(self, *, transaction_id, provider_ref, amount_kobo, minimum_date=None):
        start = minimum_date or timezone.now()
        params = {
            "from": start.date().isoformat(),
            "to": timezone.now().date().isoformat(),
            "currency": "NGN",
        }
        if transaction_id:
            params["id"] = str(transaction_id)
        if provider_ref:
            params["flw_ref"] = provider_ref

        result = self._request("GET", "/refunds", params=params)
        raw = result.get("data") or []
        candidates = raw if isinstance(raw, list) else [raw]
        expected_amount = int(amount_kobo // 100)
        expected_tx = str(transaction_id or "")
        expected_ref = str(provider_ref or "")

        for item in candidates:
            item_tx = str(
                item.get("tx_id")
                or item.get("TransactionId")
                or item.get("transaction_id")
                or ""
            )
            item_ref = str(item.get("flw_ref") or item.get("FlwRef") or item.get("reference") or "")
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
                not expected_tx
                or (bool(item_tx) and item_tx == expected_tx)
            )
            ref_matches = (
                not expected_ref
                or (bool(item_ref) and item_ref == expected_ref)
            )

            if tx_matches and ref_matches and normalized_amount == expected_amount:
                return {"status": "success", "data": item}

        return {"status": "success", "data": None}
