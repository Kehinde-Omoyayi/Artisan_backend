class SocialAuthError(Exception):
    """Safe, user-facing social-auth error. Provider secrets/codes are never included."""

    def __init__(self, message, *, status_code=400, action=None, fields=None):
        super().__init__(message)
        self.message = str(message)
        self.status_code = status_code
        self.action = action
        self.fields = fields or {}

    def as_payload(self):
        payload = {"error": self.message}
        if self.action:
            payload["action"] = self.action
        if self.fields:
            payload.update(self.fields)
        return payload
