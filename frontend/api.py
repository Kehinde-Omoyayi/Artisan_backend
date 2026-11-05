import json
import logging
import secrets
from importlib import import_module
from urllib.parse import parse_qsl, quote, urlsplit

import requests
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from django.urls import Resolver404, resolve

from .api_errors import flatten_errors, normalize_api_error

logger = logging.getLogger(__name__)

# Shared HTTP session for the "http" transport: TLS/TCP handshake and DNS cost
# otherwise repeat for every API call inside a single page render, which is the
# main per-page latency source when the API runs as a separate service.
_HTTP_SESSION = requests.Session()
_HTTP_SESSION.headers.update({"Connection": "keep-alive"})
adapter = requests.adapters.HTTPAdapter(
    pool_connections=4, pool_maxsize=16, max_retries=0
)
_HTTP_SESSION.mount("http://", adapter)
_HTTP_SESSION.mount("https://", adapter)


class APIError(Exception):
    def __init__(self, status_code, payload=None, text="", *, network_error=False):
        self.status_code = status_code
        self.payload = payload
        # Raw response text may contain traces, IDs or internal implementation details.
        self.text = ""
        self.network_error = network_error
        self.user_message = normalize_api_error(
            payload,
            status_code,
            network_error=network_error,
        )
        super().__init__(self.user_message)


def replace_path(template, **kwargs):
    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", quote(str(value), safe=""))
    return result


def extract_items(payload):
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return payload["results"]
    if isinstance(payload, list):
        return payload
    return []


def payload_message(payload):
    if isinstance(payload, dict):
        for key in ("message", "detail", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


API_PREFIX = "/api/v1/"
# Operational endpoints live at the site root, not under the versioned API.
ROOT_PREFIXES = ("/health/",)


def _client_ip(request):
    from risk.network import get_client_ip

    return get_client_ip(request) or request.META.get("REMOTE_ADDR") or "127.0.0.1"


class APIClient:
    """Server-side client used by the rendered pages to talk to the REST API.

    The default transport (``ARTISAN_API_TRANSPORT=inprocess``) resolves the API route and calls the DRF view
    inside the current worker. It needs no network hop, cannot deadlock a small worker pool, keeps the real
    visitor's IP for throttling/risk/audit, and still goes through DRF authentication, permissions,
    throttling and serializers exactly like an external client. ``ARTISAN_API_TRANSPORT=http`` keeps the
    original behaviour for deployments where the API runs as a separate service.
    """

    def __init__(self, request):
        self.request = request
        self.base = settings.ARTISAN_API_BASE_URL.rstrip("/")
        self.timeout = settings.ARTISAN_API_TIMEOUT
        self.transport = getattr(settings, "ARTISAN_API_TRANSPORT", "inprocess")

    def _headers(self):
        headers = {"Accept": "application/json"}
        token = self.request.session.get("api_access")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _save_tokens(self, access=None, refresh=None):
        if access:
            self.request.session["api_access"] = access
        if refresh:
            self.request.session["api_refresh"] = refresh
        self.request.session.modified = True

    def clear_session(self):
        for key in (
            "api_access",
            "api_refresh",
            "api_user",
            "api_role",
            "reset_token",
            "reset_email",
            "staff_reset_token",
            "staff_reset_email",
            "staff_security_gate",
            "staff_security_email",
            "manager_values_reveal_once",
            "staff_mfa_challenge_id",
            "staff_mfa_masked_email",
            "staff_mfa_resend_after",
        ):
            self.request.session.pop(key, None)
        self.request.session.modified = True

    # ------------------------------------------------------------------ transports
    def _send_http(self, method, path, *, params, data, files, headers, raw=False):
        if path.startswith(ROOT_PREFIXES):
            url = f"{settings.ARTISAN_API_ROOT_URL}{path}"
        else:
            url = path if path.startswith("http") else f'{self.base}/{path.lstrip("/")}'
        try:
            if files:
                response = _HTTP_SESSION.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    files=files,
                    headers=headers,
                    timeout=self.timeout,
                )
            else:
                response = _HTTP_SESSION.request(
                    method,
                    url,
                    params=params,
                    json=data if data is not None else None,
                    headers={**headers, "Content-Type": "application/json"},
                    timeout=self.timeout,
                )
        except requests.RequestException as exc:
            raise APIError(503, network_error=True) from exc
        return (
            response.status_code,
            response.content if raw else response.text,
            response.headers,
        )

    def _send_in_process(
        self, method, path, *, params, data, files, headers, raw=False
    ):
        split = urlsplit(path)
        route = (
            split.path
            if split.path.startswith((API_PREFIX, *ROOT_PREFIXES))
            else f'{API_PREFIX}{split.path.lstrip("/")}'
        )
        query = dict(parse_qsl(split.query))
        query.update(params or {})
        try:
            match = resolve(route)
        except Resolver404 as exc:
            raise APIError(404, {"detail": "Not found."}, "Not found.") from exc
        outer = self.request
        extra = {
            "HTTP_ACCEPT": "application/json",
            "HTTP_HOST": outer.get_host(),
            "REMOTE_ADDR": _client_ip(outer),
        }
        for name, value in headers.items():
            if name.lower() not in ("accept", "content-type"):
                extra["HTTP_" + name.upper().replace("-", "_")] = value
        factory = RequestFactory()
        secure = outer.is_secure()
        method = method.upper()
        if files:
            fields = {
                key: ("" if value is None else str(value))
                for key, value in (data or {}).items()
                if value is not None
            }
            if isinstance(files, dict):
                items = list(files.items())
            else:
                items = []
                for item in files:
                    if len(item) == 2 and isinstance(item[1], (tuple, list)):
                        items.append((item[0], item[1]))
                    else:
                        items.append((item[0], item[1:]))
            for field, spec in items:
                if len(spec) == 3 and not isinstance(spec, (bytes, str)):
                    filename, fileobj, content_type = spec
                else:
                    fileobj, content_type = spec[0], spec[1]
                    filename = getattr(fileobj, "name", "upload")
                if hasattr(fileobj, "seek"):
                    fileobj.seek(0)
                upload = SimpleUploadedFile(
                    str(filename),
                    fileobj.read(),
                    content_type=content_type or "application/octet-stream",
                )
                if field in fields and not isinstance(fields[field], list):
                    fields[field] = [fields[field]]
                if isinstance(fields.get(field), list):
                    fields[field].append(upload)
                else:
                    fields[field] = upload
            request = factory.generic(
                method,
                route,
                data=encode_multipart(BOUNDARY, fields),
                content_type=MULTIPART_CONTENT,
                secure=secure,
                **extra,
            )
        elif method == "GET":
            request = factory.get(route, data=query, secure=secure, **extra)
        elif data is not None:
            request = factory.generic(
                method,
                route,
                data=json.dumps(data),
                content_type="application/json",
                secure=secure,
                **extra,
            )
        else:
            request = factory.generic(method, route, secure=secure, **extra)
        request.request_id = getattr(outer, "request_id", "")
        try:
            response = match.func(request, *match.args, **match.kwargs)
            if hasattr(response, "render"):
                response.render()
            if getattr(response, "streaming", False):
                body = b"".join(response.streaming_content)
                response.close()
            else:
                body = response.content
        except (
            Exception
        ) as exc:  # the wrapped view is outside Django's exception middleware here
            logger.exception(
                "in_process_api_call_failed method=%s route=%s", method, route
            )
            raise APIError(
                500,
                {"detail": "The marketplace API encountered an unexpected error."},
                str(type(exc).__name__),
            ) from exc
        return (
            response.status_code,
            body if raw else body.decode("utf-8", errors="replace"),
            response,
        )

    def _send(
        self,
        method,
        path,
        *,
        params=None,
        data=None,
        files=None,
        headers=None,
        raw=False,
    ):
        headers = headers or self._headers()
        if self.transport == "http":
            return self._send_http(
                method,
                path,
                params=params,
                data=data,
                files=files,
                headers=headers,
                raw=raw,
            )
        return self._send_in_process(
            method,
            path,
            params=params,
            data=data,
            files=files,
            headers=headers,
            raw=raw,
        )

    # ------------------------------------------------------------------ token lifecycle
    def _tokens_refreshed_elsewhere(self, used_access):
        """True when a concurrent request already rotated the tokens stored for this session."""
        session = self.request.session
        if not session.session_key:
            return False
        try:
            fresh = (
                import_module(settings.SESSION_ENGINE)
                .SessionStore(session_key=session.session_key)
                .load()
            )
        except Exception:
            return False
        stored_access = fresh.get("api_access")
        if stored_access and stored_access != used_access:
            self._save_tokens(access=stored_access, refresh=fresh.get("api_refresh"))
            return True
        return False

    def refresh(self):
        refresh = self.request.session.get("api_refresh")
        used_access = self.request.session.get("api_access")
        if not refresh:
            return False
        try:
            status_code, text, _ = self._send(
                "POST",
                "/token/refresh/",
                data={"refresh": refresh},
                headers={"Accept": "application/json"},
            )
        except APIError:
            return False
        if status_code >= 400:
            # Refresh rotation blacklists the old token. If a parallel request won the race, reuse its tokens.
            return self._tokens_refreshed_elsewhere(used_access)
        try:
            data = json.loads(text)
        except ValueError:
            return False
        access = data.get("access") if isinstance(data, dict) else None
        if not access:
            return False
        # ROTATE_REFRESH_TOKENS is enabled, so the new refresh token must replace the blacklisted one.
        self._save_tokens(access=access, refresh=data.get("refresh"))
        return True

    def _call(
        self,
        method,
        path,
        *,
        params=None,
        data=None,
        files=None,
        retry=True,
        idempotency_key=None,
        headers=None,
    ):
        extra_headers = headers or {}
        headers = self._headers()
        headers.update({str(key): str(value) for key, value in extra_headers.items()})
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        status_code, text, response = self._send(
            method, path, params=params, data=data, files=files, headers=headers
        )

        if status_code == 401 and retry and self.request.session.get("api_refresh"):
            if self.refresh():
                return self._call(
                    method,
                    path,
                    params=params,
                    data=data,
                    files=files,
                    retry=False,
                    idempotency_key=idempotency_key,
                    headers=extra_headers,
                )
            self.clear_session()

        try:
            payload = json.loads(text) if text else {}
        except (TypeError, ValueError):
            payload = None
            headers_out = getattr(response, "headers", {})
            request_id = (
                headers_out.get("X-Request-ID", "")
                if hasattr(headers_out, "get")
                else ""
            )
            logger.warning(
                "frontend_api_invalid_json status=%s path=%s request_id=%s",
                status_code,
                path,
                request_id,
            )
        if status_code >= 400:
            code = (
                str(payload.get("code") or "").upper()
                if isinstance(payload, dict)
                else ""
            )
            if code in {"PASSWORD_CHANGE_REQUIRED", "STAFF_SECURITY_SETUP_REQUIRED"}:
                self.request.session["staff_security_gate"] = code
                self.request.session.modified = True
            raise APIError(status_code, payload, text)
        if payload is None:
            raise APIError(502, None)
        return status_code, payload, getattr(response, "headers", response)

    def get(self, path, params=None):
        return self._call("GET", path, params=params)

    def get_file(self, path):
        """Fetch a private API file for a session-authenticated frontend proxy view."""
        status_code, body, response = self._send("GET", path, raw=True)
        if status_code == 401 and self.request.session.get("api_refresh"):
            if self.refresh():
                status_code, body, response = self._send("GET", path, raw=True)
            else:
                self.clear_session()
        if status_code >= 400:
            text = (
                body.decode("utf-8", errors="replace")
                if isinstance(body, bytes)
                else str(body or "")
            )
            try:
                payload = json.loads(text) if text else {}
            except ValueError:
                payload = None
            raise APIError(status_code, payload, text)
        headers = getattr(
            response, "headers", response if hasattr(response, "get") else {}
        )
        return body, headers.get("Content-Type", "application/octet-stream"), headers

    def post(self, path, data=None, files=None, idempotency_key=None, headers=None):
        return self._call(
            "POST",
            path,
            data=data,
            files=files,
            idempotency_key=idempotency_key,
            headers=headers,
        )

    def put(self, path, data=None, files=None, idempotency_key=None):
        return self._call(
            "PUT", path, data=data, files=files, idempotency_key=idempotency_key
        )

    def patch(self, path, data=None, files=None, idempotency_key=None):
        return self._call(
            "PATCH", path, data=data, files=files, idempotency_key=idempotency_key
        )

    def delete(self, path):
        return self._call("DELETE", path)

    def logout(self):
        try:
            if self.request.session.get("api_access"):
                self.post("/logout/")
        except APIError:
            pass
        self.clear_session()


def new_idempotency_key(prefix="web"):
    return f"{prefix}-{secrets.token_urlsafe(18)}"
