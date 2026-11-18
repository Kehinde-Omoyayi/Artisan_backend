"""OpenAPI integration: describe the custom JWT authenticator so the schema documents Bearer auth."""
from drf_spectacular.extensions import OpenApiAuthenticationExtension


class MarketplaceJWTAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "home.authentication.MarketplaceJWTAuthentication"
    name = "jwtAuth"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
