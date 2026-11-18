# Google and Apple authentication in the Artisan Market API

`social_auth` is installed and its API routes are mounted additively in this project. Provider code exchange, ID-token verification, identity persistence, account creation, OTP setup, and JWT issuance are performed by `artisan_service`; the frontend is a client of the new API routes. Existing `/api/v1/register/`, `/api/v1/login/`, OTP, account, profile, permission, and business endpoint contracts remain unchanged.

The implementation supports:

- Native authorization-code sign-in for Google and Apple iOS (Apple AuthenticationServices).
- Browser Google and Sign in with Apple redirects for the separately runnable `artisan_frontent` project. Its small server-side bridge forwards codes to this API; it does not validate provider credentials or create marketplace identities.
- New social registration followed by the existing email OTP verification; a missing first or last name is collected before the OTP step.
- Subsequent sign-in only through an already linked provider subject. Matching email alone does not link an account.

No Google/Apple client IDs, secrets, bundle IDs, Team IDs, Key IDs, private keys, or redirect registrations are supplied. Provider sign-in returns a configuration error until real provider-console values are supplied.

## Installed integration

The actual project now includes:

- `social_auth.apps.SocialAuthConfig` in `INSTALLED_APPS`.
- `path("api/v1/social-auth/", include("social_auth.api_urls"))` in the root URL configuration.
- `social_auth/migrations/0001_initial.py` for the unique verified provider-identity mapping.
- `frontend.social_views` as a thin page bridge. The integrated UI uses the same API web-start/web-complete contract in-process by default; the standalone frontend uses it over HTTP.

Apply the migration:

```shell
python manage.py migrate social_auth
```

Install the added requirement from `requirements.txt`: `PyJWT[crypto]` (the project already includes `cryptography` and `requests`). Production and multi-worker deployments must use a shared atomic cache—this project's Redis cache is used for provider state, PKCE verifier, nonce, pending names, and JWKS. A single-process local development cache is sufficient only for local tests.

## API-side provider configuration

Set these values only in the API server's environment/secret manager. Keep values blank or unset until you have the real provider values. Do not copy secrets to the frontend, JavaScript, iOS app, ZIP, or source control.

```text
SOCIAL_AUTH_HTTP_TIMEOUT_SECONDS=8

SOCIAL_AUTH_GOOGLE_WEB_CLIENT_ID=
SOCIAL_AUTH_GOOGLE_WEB_CLIENT_SECRET=
SOCIAL_AUTH_GOOGLE_WEB_REDIRECT_URI=
SOCIAL_AUTH_GOOGLE_NATIVE_CLIENT_ID=
SOCIAL_AUTH_GOOGLE_NATIVE_CLIENT_SECRET=
SOCIAL_AUTH_GOOGLE_NATIVE_REDIRECT_URI=

SOCIAL_AUTH_APPLE_WEB_CLIENT_ID=
SOCIAL_AUTH_APPLE_WEB_REDIRECT_URI=
SOCIAL_AUTH_APPLE_NATIVE_CLIENT_ID=
SOCIAL_AUTH_APPLE_NATIVE_REDIRECT_URI=
SOCIAL_AUTH_APPLE_TEAM_ID=
SOCIAL_AUTH_APPLE_KEY_ID=
SOCIAL_AUTH_APPLE_PRIVATE_KEY=
```

`SOCIAL_AUTH_APPLE_PRIVATE_KEY` is the actual `.p8` file contents with newlines (or escaped `\\n`) and is server-only. The API signs a short-lived Apple client-secret JWT at exchange time. The frontend's `ARTISAN_API_BASE_URL` is configured separately in `artisan_frontent/.env`.

### Web redirect registration

The web redirect URI values on the API must exactly match the URLs registered with Google and Apple. For the separate frontend, the provider returns the browser to the frontend callback (not an API JSON endpoint):

```text
https://<your-frontend-host>/auth/social/google/callback/
https://<your-frontend-host>/auth/social/apple/callback/
```

Set `SOCIAL_AUTH_GOOGLE_WEB_REDIRECT_URI` and `SOCIAL_AUTH_APPLE_WEB_REDIRECT_URI` to the corresponding exact registered URL. Google web authorization uses a server-side code exchange, nonce, state, and PKCE. Apple web authorization uses `response_mode=form_post`; the frontend callback is HTTPS in production, and its session cookie is `Secure; SameSite=None` so the provider POST can be bound to the original frontend session. Configure reverse-proxy HTTPS detection correctly.

For an all-in-one deployment, use that deployment's own public callback URLs and register those exact values instead. Do not set provider callback URLs to an unregistered placeholder.

## Browser API contract (used by `artisan_frontent`)

The standalone frontend's registration/login controls call these API endpoints through its server-side `APIClient`; provider client secrets never reach the browser.

### Start

`POST /api/v1/social-auth/<provider>/web/start/` where `<provider>` is `google` or `apple`.

Registration request:

```json
{
  "intent": "register",
  "role": "customer",
  "first_name": "Optional",
  "last_name": "Optional",
  "browser_binding": "random value kept in the frontend server-side session"
}
```

Login request:

```json
{
  "intent": "login",
  "browser_binding": "random value kept in the frontend server-side session"
}
```

The API returns `provider`, `intent`, one-use `state`, `authorization_url`, and `expires_in`. The API stores the binding, nonce, and (for Google) PKCE verifier server-side. The browser binding is not returned by the API and must remain in the frontend's server-side session.

### Complete

After the provider returns to the frontend callback, the frontend forwards the authorization code and its session-bound values to:

`POST /api/v1/social-auth/<provider>/web/complete/`

```json
{
  "state": "state-from-start",
  "authorization_code": "one-time-provider-code",
  "browser_binding": "the same server-side value sent to web/start"
}
```

Apple may include its first-authorization `user` form value. The frontend can forward that optional value for name extraction only; the API still derives the identity email and subject from the verified ID token. The API consumes state once, exchanges the code, verifies signing key, signature, issuer, audience, expiry, nonce, verified email, and subject, then returns the registration result or access/refresh-token response. API responses containing auth results are marked `Cache-Control: no-store`.

If a new provider profile lacks names, the API returns `202` with `action: "collect_profile_details"` and a short-lived `registration_token`. Submit the missing values to:

`POST /api/v1/social-auth/complete-profile/`

```json
{"registration_token":"...","first_name":"...","last_name":"..."}
```

The original `/api/v1/social-auth/native/complete-profile/` route remains as a compatibility route for native clients. Once account creation starts, the API creates an inactive user and the existing email OTP flow remains authoritative: submit the code to the existing `/api/v1/verify-otp/`. The frontend stores API bearer tokens only in its own server-side session after a successful API login.

## Native iOS API contract

### Apple Sign in with Apple (iOS)

1. Call `POST /api/v1/social-auth/apple/native/start/` with `{"intent":"register","role":"customer"}` or `{"intent":"login"}`.
2. For AuthenticationServices, set the returned `state` and `nonce` on the `ASAuthorizationAppleIDRequest`; request `.email` and `.fullName` for registration. The response supplies a one-time authorization code and may supply names only on first authorization.
3. Submit the code, exact returned `state`, and available names to `POST /api/v1/social-auth/apple/native/complete/`.
4. The API exchanges the code using the configured Apple native client ID/bundle ID and server-signed client-secret JWT. The iOS app never receives the `.p8` private key.

### Google native/iOS authorization-code + PKCE

1. Call `POST /api/v1/social-auth/google/native/start/` with `{"intent":"register","role":"artisan"}` or `{"intent":"login"}`.
2. The response contains `authorization_url`, `state`, `nonce`, `code_challenge`, `code_challenge_method: "S256"`, and the configured public `client_id`. Open the URL in the system browser/auth session and handle the registered native redirect.
3. Submit the one-time authorization code and returned `state` to `POST /api/v1/social-auth/google/native/complete/`. The API redeems the code with the server-held PKCE verifier and verifies the ID token.

This API implements the authorization-code/PKCE flow. It does not accept the separate Google iOS SDK `serverAuthCode` flow unless that client is separately engineered and tested. Do not put a client secret in a native app. For both providers, state can be completed only once; restart if it expires or the provider rejects the code.

### Registration response and account rules

- Complete names: `201` with `action: "redirect_to_otp_verification"`, email, and message.
- Missing name(s): `202` with `action: "collect_profile_details"` and a short-lived token.
- Existing password account with the same email: `409`; there is no automatic email-based provider linking.
- Inactive linked user: provider login remains blocked until the existing OTP step succeeds.
- Locked user remains blocked. Successful provider login returns the existing marketplace JWT access/refresh format and `token_version` claim.

## Security and operational notes

- Native API endpoints and browser API endpoints retain the anonymous-only boundary and use DRF anonymous throttling. An authenticated client cannot use them to create or switch accounts.
- State, PKCE verifier, nonce, and browser binding expire after ten minutes. Keep a shared cache configured for multiple workers.
- Google callback authorization codes may appear in the browser callback query URL; redact `code` and `state` in proxy/access logs. The frontend marks the callback response no-store and no-referrer.
- Do not trust names, email addresses, provider IDs, or ID tokens sent by the client; only the verified token and Apple's one-time `user` name field (as a name fallback) are used.
- Account matching uses `(provider, subject)` only. Email equality never links an identity.
- Live Google/Apple OAuth exchanges have not been performed because real provider credentials and callback registrations were not supplied.
