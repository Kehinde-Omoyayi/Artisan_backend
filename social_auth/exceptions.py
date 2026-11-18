class SocialAuthError(Exception):
    """Safe, user-facing social-auth error. Provider secrets/codes are never included.

    ``reason`` is a short, stable machine code (``state_missing``,
    ``token_expired`` ...) that goes into the API payload and the logs. The
    message is what a person reads; the reason is what tells an operator which
    of the many ways a provider handshake can fail actually happened -- without
    echoing a token, a code or a secret back to the browser.
    """

    def __init__(self, message, *, status_code=400, action=None, fields=None, reason=""):
        super().__init__(message)
        self.message = str(message)
        self.status_code = status_code
        self.action = action
        self.fields = fields or {}
        self.reason = str(reason or "").strip()

    def as_payload(self):
        payload = {"error": self.message}
        if self.reason:
            payload["reason"] = self.reason
        if self.action:
            payload["action"] = self.action
        if self.fields:
            payload.update(self.fields)
        return payload
