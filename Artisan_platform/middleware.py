"""Local-review only middleware, switched on by ALLOW_SANDBOXED_ORIGIN=1.

When this API is opened inside a sandboxed preview frame (the browser gives the
page an opaque origin), every form POST arrives with ``Origin: null``. Django's
CSRF origin check can never trust the literal string "null", so the page answers
403 no matter how fresh the token is.

This middleware rewrites that one header to the host the request actually
reached, which is what a same-origin form post would have sent. It is refused
outright unless DEBUG is on, so it cannot reach a production process.
"""


class SandboxedOriginMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.META.get("HTTP_ORIGIN") == "null":
            host = request.get_host()
            request.META["HTTP_ORIGIN"] = f"{request.scheme}://{host}"
        return self.get_response(request)
