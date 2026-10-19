"""Server-side Google and Sign in with Apple authorization-code helpers.

All provider credentials are read from Django settings. Authorization codes are
short-lived and exchanged server-side; they are never written to logs or storage.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
from urllib.parse import urlencode

import jwt
import requests
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from .exceptions import SocialAuthError

logger = logging.getLogger(__name__)

PROVIDERS = ("google", "apple")
PLATFORMS = ("web", "native")
INTENTS = ("register", "login")
STATE_TTL_SECONDS = 600
PENDING_CACHE_PREFIX = "social_auth:state:"

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105  # nosec B105: public OAuth endpoint, not a credential
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
APPLE_AUTHORIZE_URL = "https://appleid.apple.com/auth/authorize"
APPLE_TOKEN_URL = "https://appleid.apple.com/auth/token"  # noqa: S105  # nosec B105: public OAuth endpoint, not a credential
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"


def _state_key(state):
    digest = hashlib.sha256(state.encode("utf-8")).hexdigest()
    return f"{PENDING_CACHE_PREFIX}{digest}"


def code_challenge(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def create_transaction(*, provider, platform, intent, role=None, first_name="", last_name="", next_path="", browser_binding=""):
    """Create short-lived, server-held OAuth state and nonce for one attempt."""
    if provider not in PROVIDERS or platform not in PLATFORMS or intent not in INTENTS:
        raise SocialAuthError("That social sign-in option is not supported.")
    if platform == "web" and (not isinstance(browser_binding, str) or not browser_binding or len(browser_binding) > 512):
        raise SocialAuthError("Please restart social sign-in in this browser.")
    if intent == "register" and role not in ("customer", "artisan"):
        raise SocialAuthError("Choose customer or artisan before continuing.", fields={"role": ["Select a role first."]})

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64) if provider == "google" else ""
    transaction = {
        "state": state,
        "provider": provider,
        "platform": platform,
        "intent": intent,
        "role": role if intent == "register" else "",
        "first_name": (first_name or "").strip()[:40],
        "last_name": (last_name or "").strip()[:40],
        "next_path": next_path or "",
        "browser_binding": browser_binding if platform == "web" else "",
        "nonce": nonce,
        "code_verifier": verifier,
    }
    if not cache.add(_state_key(state), transaction, timeout=STATE_TTL_SECONDS):
        raise SocialAuthError("Please try social sign-in again.", status_code=503)
    return transaction


def consume_transaction(*, state, provider, platform, browser_binding=""):
    """Atomically claim one state record so callbacks cannot reuse an attempt."""
    if not isinstance(state, str) or not state or len(state) > 512:
        raise SocialAuthError("The social sign-in could not be verified. Please try again.")
    key = _state_key(state)
    lock_key = f"{key}:processing"
    if not cache.add(lock_key, True, timeout=30):
        raise SocialAuthError("This social sign-in attempt has already been used.")
    try:
        transaction = cache.get(key)
        if (
            not isinstance(transaction, dict)
            or transaction.get("provider") != provider
            or transaction.get("platform") != platform
            or not hmac.compare_digest(str(transaction.get("state", "")), state)
            or (
                platform == "web"
                and (
                    not isinstance(browser_binding, str)
                    or not browser_binding
                    or not hmac.compare_digest(str(transaction.get("browser_binding", "")), browser_binding)
                )
            )
        ):
            raise SocialAuthError("The social sign-in expired or could not be verified. Please start again.")
        cache.delete(key)
        return transaction
    finally:
        cache.delete(lock_key)


def provider_config(provider, platform):
    if provider not in PROVIDERS or platform not in PLATFORMS:
        raise SocialAuthError("That social sign-in option is not supported.")
    prefix = f"SOCIAL_AUTH_{provider.upper()}_{platform.upper()}"
    client_id = getattr(settings, f"{prefix}_CLIENT_ID", "") or ""
    client_secret = getattr(settings, f"{prefix}_CLIENT_SECRET", "") or ""
    redirect_uri = getattr(settings, f"{prefix}_REDIRECT_URI", "") or ""
    if not client_id:
        raise SocialAuthError(
            f"{provider.title()} sign-in is not configured yet.",
            status_code=503,
        )
    if provider == "google" and not redirect_uri:
        raise SocialAuthError("Google authorization-code settings are incomplete.", status_code=503)
    if provider == "google" and platform == "web" and not client_secret:
        raise SocialAuthError("Google web sign-in requires a server-side client secret.", status_code=503)
    if provider == "apple" and platform == "web" and not redirect_uri.startswith("https://"):
        raise SocialAuthError("Apple web sign-in requires its registered HTTPS return URL.", status_code=503)
    if provider == "google" and platform == "web" and getattr(settings, "ENVIRONMENT", "") == "production" and not redirect_uri.startswith("https://"):
        raise SocialAuthError("Google web sign-in requires an HTTPS return URL in production.", status_code=503)
    return {"client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect_uri}


def _apple_client_secret(client_id):
    team_id = getattr(settings, "SOCIAL_AUTH_APPLE_TEAM_ID", "") or ""
    key_id = getattr(settings, "SOCIAL_AUTH_APPLE_KEY_ID", "") or ""
    private_key = getattr(settings, "SOCIAL_AUTH_APPLE_PRIVATE_KEY", "") or ""
    if not team_id or not key_id or not private_key:
        raise SocialAuthError("Apple sign-in credentials are not configured on the server.", status_code=503)
    private_key = private_key.replace("\\n", "\n")
    try:
        import time

        now = int(time.time())
        return jwt.encode(
            {"iss": team_id, "iat": now, "exp": now + 300, "aud": "https://appleid.apple.com", "sub": client_id},
            private_key,
            algorithm="ES256",
            headers={"kid": key_id},
        )
    except Exception as exc:
        logger.exception("apple_client_secret_generation_failed")
        raise SocialAuthError("Apple sign-in is temporarily unavailable.", status_code=503) from exc


def authorization_url(*, provider, platform, transaction):
    """Build a redirect URL for browser flows and Google native code flows.

    Apple native iOS apps should use AuthenticationServices with the returned
    state and nonce; Apple web uses its form_post callback.
    """
    config = provider_config(provider, platform)
    state = transaction["state"]
    nonce = transaction["nonce"]
    if provider == "apple" and platform == "native":
        return ""
    if provider == "google":
        params = {
            "client_id": config["client_id"],
            "redirect_uri": config["redirect_uri"],
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "prompt": "select_account",
            "code_challenge": code_challenge(transaction["code_verifier"]),
            "code_challenge_method": "S256",
        }
        return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"
    params = {
        "client_id": config["client_id"],
        "redirect_uri": config["redirect_uri"],
        "response_type": "code",
        "response_mode": "form_post",
        "scope": "name email",
        "state": state,
        "nonce": nonce,
    }
    return f"{APPLE_AUTHORIZE_URL}?{urlencode(params)}"


def _http_timeout():
    try:
        configured = int(getattr(settings, "SOCIAL_AUTH_HTTP_TIMEOUT_SECONDS", 8))
    except (TypeError, ValueError):
        configured = 8
    return max(1, min(configured, 30))


def _fetch_jwks(provider):
    url = GOOGLE_JWKS_URL if provider == "google" else APPLE_JWKS_URL
    cache_key = f"social_auth:jwks:{provider}"
    keys = cache.get(cache_key)
    if isinstance(keys, list) and keys:
        return keys
    timeout = _http_timeout()
    try:
        response = requests.get(url, headers={"Accept": "application/json"}, timeout=timeout)
        if response.status_code != 200:
            raise ValueError("Provider key endpoint returned an unsuccessful response.")
        payload = response.json()
        keys = payload.get("keys") if isinstance(payload, dict) else None
        if not isinstance(keys, list) or not keys:
            raise ValueError("Provider key response was incomplete.")
        cache.set(cache_key, keys, timeout=3600)
        return keys
    except (requests.RequestException, ValueError) as exc:
        logger.warning("social_jwks_fetch_failed provider=%s", provider)
        raise SocialAuthError("The identity provider is temporarily unavailable. Please try again.", status_code=503) from exc


def _signing_key(provider, token):
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise SocialAuthError("The identity provider returned an invalid identity token.") from exc
    if header.get("alg") != "RS256" or not header.get("kid"):
        raise SocialAuthError("The identity provider returned an unsupported identity token.")
    kid = header["kid"]
    keys = _fetch_jwks(provider)
    def matching_key(candidates):
        return next(
            (
                item
                for item in candidates
                if isinstance(item, dict)
                and item.get("kid") == kid
                and item.get("kty") == "RSA"
                and item.get("alg") in (None, "RS256")
                and item.get("use") in (None, "sig")
            ),
            None,
        )

    jwk = matching_key(keys)
    if jwk is None:
        cache.delete(f"social_auth:jwks:{provider}")
        keys = _fetch_jwks(provider)
        jwk = matching_key(keys)
    if jwk is None:
        raise SocialAuthError("The identity provider returned an unrecognized signing key.")
    try:
        return jwt.PyJWK.from_dict(jwk, algorithm="RS256").key
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise SocialAuthError("The identity provider returned an invalid signing key.") from exc


def verify_identity_token(*, provider, id_token, client_id, expected_nonce):
    if provider not in PROVIDERS or not isinstance(id_token, str) or len(id_token) > 16000:
        raise SocialAuthError("The identity provider returned an invalid identity token.")
    signing_key = _signing_key(provider, id_token)
    try:
        claims = jwt.decode(
            id_token,
            signing_key,
            algorithms=["RS256"],
            audience=client_id,
            options={"require": ["iss", "sub", "aud", "exp", "iat", "email"]},
        )
    except jwt.PyJWTError as exc:
        raise SocialAuthError("The identity provider could not verify this account.") from exc

    issuer = claims.get("iss")
    allowed_issuers = ("https://accounts.google.com", "accounts.google.com") if provider == "google" else ("https://appleid.apple.com",)
    if issuer not in allowed_issuers:
        raise SocialAuthError("The identity provider returned an invalid issuer.")
    audience = claims.get("aud")
    authorized_party = claims.get("azp")
    if (isinstance(audience, list) and len(audience) > 1 and authorized_party != client_id) or (
        authorized_party and authorized_party != client_id
    ):
        raise SocialAuthError("The identity provider returned an invalid client audience.")
    token_nonce = claims.get("nonce")
    if not isinstance(expected_nonce, str) or not expected_nonce or not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, expected_nonce):
        raise SocialAuthError("The identity provider response could not be linked to this sign-in attempt.")

    verified = claims.get("email_verified")
    if not (verified is True or (isinstance(verified, str) and verified.lower() == "true")):
        raise SocialAuthError("The identity provider did not confirm this email address.")
    email = claims.get("email")
    try:
        validate_email(email)
    except (ValidationError, TypeError) as exc:
        raise SocialAuthError("The identity provider did not return a valid email address.") from exc
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise SocialAuthError("The identity provider returned an invalid account identifier.")

    return {
        "provider": provider,
        "subject": subject,
        "email": email.strip().lower(),
        "email_verified": True,
        "first_name": str(claims.get("given_name") or "").strip()[:40],
        "last_name": str(claims.get("family_name") or "").strip()[:40],
    }


def exchange_authorization_code(*, provider, platform, code, transaction):
    """Exchange a one-time code and validate the resulting ID token."""
    if provider not in PROVIDERS or platform not in PLATFORMS or not isinstance(code, str) or not code or len(code) > 4096:
        raise SocialAuthError("The authorization code was missing or invalid.")
    config = provider_config(provider, platform)
    token_url = GOOGLE_TOKEN_URL if provider == "google" else APPLE_TOKEN_URL
    if provider == "google":
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": config["client_id"],
            "redirect_uri": config["redirect_uri"],
            "code_verifier": transaction.get("code_verifier", ""),
        }
        if config["client_secret"]:
            data["client_secret"] = config["client_secret"]
    else:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": config["client_id"],
            "client_secret": _apple_client_secret(config["client_id"]),
        }
        if config["redirect_uri"]:
            data["redirect_uri"] = config["redirect_uri"]

    timeout = _http_timeout()
    try:
        response = requests.post(
            token_url,
            data=data,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        if response.status_code != 200:
            logger.info("social_code_exchange_rejected provider=%s platform=%s status=%s", provider, platform, response.status_code)
            raise SocialAuthError("The identity provider rejected this sign-in attempt. Please try again.")
        payload = response.json()
    except SocialAuthError:
        raise
    except (requests.RequestException, ValueError) as exc:
        logger.warning("social_code_exchange_failed provider=%s platform=%s", provider, platform)
        raise SocialAuthError("The identity provider is temporarily unavailable. Please try again.", status_code=503) from exc
    id_token = payload.get("id_token") if isinstance(payload, dict) else None
    if not id_token:
        raise SocialAuthError("The identity provider did not return a verifiable account identity.")
    identity = verify_identity_token(
        provider=provider,
        id_token=id_token,
        client_id=config["client_id"],
        expected_nonce=transaction.get("nonce", ""),
    )
    return identity


def apple_user_names(raw_user):
    """Parse Apple’s one-time web `user` form field for display-name fallback only."""
    if isinstance(raw_user, str):
        try:
            raw_user = json.loads(raw_user)
        except (TypeError, ValueError):
            return "", ""
    if not isinstance(raw_user, dict):
        return "", ""
    name = raw_user.get("name")
    if not isinstance(name, dict):
        return "", ""
    return (
        str(name.get("firstName") or "").strip()[:40],
        str(name.get("lastName") or "").strip()[:40],
    )
