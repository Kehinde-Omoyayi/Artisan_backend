import requests

from django.conf import settings


class MonoVerificationService:
    def __init__(self):
        self.base_url = settings.MONO_BASE_URL.rstrip("/")
        self.headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "mono-sec-key": settings.MONO_SECRET_KEY,
        }

    def _post(self, endpoint, payload, session_id=None):
        headers = self.headers.copy()

        if session_id:
            headers["x-session-id"] = session_id

        try:
            response = requests.post(
                f"{self.base_url}{endpoint}",
                headers=headers,
                json=payload,
                timeout=10,
            )
        except requests.RequestException as exc:
            raise ConnectionError(
                "Mono service unavailable."
            ) from exc

        try:
            data = response.json()
        except ValueError:
            data = {
                "status": "failed",
                "message": (
                    response.text.strip()
                    or "Mono returned an empty response."
                ),
            }

        if not isinstance(data, dict):
            data = {
                "status": "failed",
                "message": "Mono returned an invalid response.",
            }

        data["_http_status"] = response.status_code
        return data

    def lookup_nin(self, nin):
        return self._post(
            "/v3/lookup/nin",
            {"nin": nin},
        )

    def initiate_bvn(self, bvn):
        return self._post(
            "/v2/lookup/bvn/initiate",
            {
                "bvn": bvn,
                "scope": "identity",
            },
        )

    def verify_bvn(self, session_id):
        return self._post(
            "/v2/lookup/bvn/verify",
            {"method": "email"},
            session_id=session_id,
        )

    def fetch_bvn_details(self, session_id, otp):
        return self._post(
            "/v2/lookup/bvn/details",
            {"otp": otp},
            session_id=session_id,
        )