from django.http import JsonResponse

from .network import get_client_ip, hash_ip, is_network_blocked


class CustomerNetworkRiskMiddleware:
    """Defense-in-depth network control for suspended customer traffic.

    IP blocking is deliberately limited to high-impact API surfaces. It does not
    replace account authentication and does not block shared networks that fail
    the confidence criteria in the risk engine.
    """

    PROTECTED_PREFIXES = (
        "/api/v1/register/",
        "/api/v1/login/",
        "/api/v1/verify-otp/",
        "/api/v1/request-otp/",
        "/api/v1/forgot-password/",
        "/api/v1/verify-reset-otp/",
        "/api/v1/reset-password/",
        "/api/v1/token/refresh/",
        "/api/v1/booking/",
        "/api/v1/payments/booking/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        protected = request.path.startswith(self.PROTECTED_PREFIXES)
        if protected:
            # Do not let an IP block interfere with harmless read-only catalog or
            # booking reads. Enforce the network control on authentication and
            # state-changing booking/payment operations.
            is_state_changing = request.method in {"POST", "PUT", "PATCH", "DELETE"}
            if request.path.startswith("/api/v1/booking/") and not is_state_changing:
                protected = False
            if request.path.startswith("/api/v1/payments/booking/") and request.method != "POST":
                protected = False

        if protected:
            ip_hash = hash_ip(get_client_ip(request))
            if is_network_blocked(ip_hash):
                return JsonResponse(
                    {
                        "error": "This network is temporarily restricted from high-risk account and booking actions.",
                        "action": "contact_support",
                    },
                    status=403,
                )
        return self.get_response(request)
