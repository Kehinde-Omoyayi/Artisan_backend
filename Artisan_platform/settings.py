import os
from datetime import timedelta
from pathlib import Path

from decouple import Csv, config
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

AUTH_USER_MODEL = "home.CustomUser"

ENVIRONMENT = config("ENVIRONMENT")
DEBUG = config("DEBUG", cast=bool)
SECRET_KEY = config("SECRET_KEY")
ALLOWED_HOSTS = config("ALLOWED_HOSTS", cast=Csv())
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", cast=Csv())
PUBLIC_API_DOCS = config(
    "PUBLIC_API_DOCS", default=ENVIRONMENT != "production", cast=bool
)

# Public HTTPS origin of the web frontend, used to build absolute links in emails (no trailing slash).
SITE_URL = config(
    "SITE_URL", default=(CSRF_TRUSTED_ORIGINS[0] if CSRF_TRUSTED_ORIGINS else "")
).rstrip("/")

if ENVIRONMENT == "production":
    if DEBUG:
        raise ImproperlyConfigured("DEBUG must be False in production.")
    if not ALLOWED_HOSTS:
        raise ImproperlyConfigured("ALLOWED_HOSTS must be configured in production.")
    if not CSRF_TRUSTED_ORIGINS:
        raise ImproperlyConfigured(
            "CSRF_TRUSTED_ORIGINS must be configured in production."
        )
    _weak_secret_markers = (
        "replace-me",
        "changeme",
        "change-me",
        "insecure",
        "secret",
        "django-insecure",
    )
    if (
        len(SECRET_KEY) < 50
        or len(set(SECRET_KEY)) < 10
        or any(marker in SECRET_KEY.lower() for marker in _weak_secret_markers)
    ):
        raise ImproperlyConfigured(
            "SECRET_KEY must be a unique random value of at least 50 characters in production."
        )
    if "*" in ALLOWED_HOSTS:
        raise ImproperlyConfigured("ALLOWED_HOSTS must not contain '*' in production.")
    if not SITE_URL.startswith("https://"):
        raise ImproperlyConfigured(
            "SITE_URL (or the first CSRF_TRUSTED_ORIGINS entry) must be an https URL in production."
        )

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "home.apps.HomeConfig",
    "services",
    "booking",
    "profiles",
    "payments",
    "notifications",
    "reviews",
    "support",
    "promotions.apps.PromotionsConfig",
    "risk",
    "conversations",
    "frontend",
    "social_auth.apps.SocialAuthConfig",
    "rest_framework",
    "drf_spectacular",
    "django_filters",
    "core.apps.CoreConfig",
    "rest_framework_simplejwt.token_blacklist",
]

MIDDLEWARE = [
    "core.middleware.RequestContextMiddleware",
    "core.middleware.RequestBodyGuardMiddleware",
    "core.middleware.SecurityObservabilityMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "risk.middleware.CustomerNetworkRiskMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.StaffAdminPasswordMiddleware",
    "core.middleware.StaffAdminMFAMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

ROOT_URLCONF = "Artisan_platform.urls"
WSGI_APPLICATION = "Artisan_platform.wsgi.application"

DB_ENGINE = config("DB_ENGINE", default="sqlite")

if ENVIRONMENT == "production" and DB_ENGINE != "postgres":
    raise ImproperlyConfigured("Production must use PostgreSQL. SQLite is not allowed.")

if DB_ENGINE == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": config("DB_NAME"),
            "USER": config("DB_USER"),
            "PASSWORD": config("DB_PASSWORD"),
            "HOST": config("DB_HOST"),
            "PORT": config("DB_PORT"),
            "CONN_MAX_AGE": config("DB_CONN_MAX_AGE", default=60, cast=int),
            "OPTIONS": {
                "sslmode": config(
                    "DB_SSLMODE",
                    default="require" if ENVIRONMENT == "production" else "prefer",
                )
            },
        }
    }

else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

REDIS_URL = config("REDIS_URL")
if ENVIRONMENT == "production" and not REDIS_URL:
    raise ImproperlyConfigured("REDIS_URL must be configured in production.")

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            # JSON instead of the default pickle: a cache value read back from Redis must never be able to
            # execute code, even if Redis is reachable by an attacker.
            "SERIALIZER": "django_redis.serializers.json.JSONSerializer",
            "SOCKET_CONNECT_TIMEOUT": 3,
            "SOCKET_TIMEOUT": 3,
        },
    }
}

REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "core.exceptions.handler",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "home.authentication.MarketplaceJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ],
}

# for faster registration time rate
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

# Number of trusted reverse proxies in front of the app. 0 = ignore X-Forwarded-For (client IP = socket peer).
# Without this DRF keys throttles on the raw header, which any client can rotate to bypass every rate limit.
REST_FRAMEWORK["NUM_PROXIES"] = config(
    "DRF_NUM_PROXIES", default=1 if ENVIRONMENT == "production" else 0, cast=int
)

REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {
    "anon": config("DRF_ANON_RATE", default="60/min"),
    "user": config("DRF_USER_RATE", default="300/min"),
    "auth_register": config("THROTTLE_AUTH_REGISTER", default="10/hour"),
    "auth_verify_otp": config("THROTTLE_AUTH_VERIFY_OTP", default="10/hour"),
    "auth_request_otp": config("THROTTLE_AUTH_REQUEST_OTP", default="5/hour"),
    "auth_login": config("THROTTLE_AUTH_LOGIN", default="10/min"),
    "auth_forgot_password": config("THROTTLE_AUTH_FORGOT_PASSWORD", default="5/hour"),
    "auth_verify_reset": config("THROTTLE_AUTH_VERIFY_RESET", default="10/hour"),
    "auth_reset_password": config("THROTTLE_AUTH_RESET_PASSWORD", default="10/hour"),
    "verification_nin": config("THROTTLE_VERIFICATION_NIN", default="5/hour"),
    "verification_bvn_initiate": config(
        "THROTTLE_VERIFICATION_BVN_INITIATE", default="5/hour"
    ),
    "verification_bvn_method": config(
        "THROTTLE_VERIFICATION_BVN_METHOD", default="10/hour"
    ),
    "verification_bvn_details": config(
        "THROTTLE_VERIFICATION_BVN_DETAILS", default="10/hour"
    ),
    "payment_initialize": config("THROTTLE_PAYMENT_INITIALIZE", default="10/hour"),
    "payment_callback": config("THROTTLE_PAYMENT_CALLBACK", default="30/min"),
    "support_create": config("THROTTLE_SUPPORT_CREATE", default="20/hour"),
    "support_message": config("THROTTLE_SUPPORT_MESSAGE", default="30/hour"),
    "support_refund_request": config(
        "THROTTLE_SUPPORT_REFUND_REQUEST", default="20/hour"
    ),
    "support_refund_approve": config(
        "THROTTLE_SUPPORT_REFUND_APPROVE", default="20/hour"
    ),
    "support_refund_reject": config(
        "THROTTLE_SUPPORT_REFUND_REJECT", default="20/hour"
    ),
    "support_refund_execute": config(
        "THROTTLE_SUPPORT_REFUND_EXECUTE", default="10/hour"
    ),
    "support_refund_reconcile": config(
        "THROTTLE_SUPPORT_REFUND_RECONCILE", default="30/hour"
    ),
    "conversation_message": config("THROTTLE_CONVERSATION_MESSAGE", default="120/min"),
    "conversation_offer": config("THROTTLE_CONVERSATION_OFFER", default="30/hour"),
    "conversation_counter": config("THROTTLE_CONVERSATION_COUNTER", default="60/hour"),
    "conversation_dispute": config("THROTTLE_CONVERSATION_DISPUTE", default="10/hour"),
    "conversation_start": config("THROTTLE_CONVERSATION_START", default="30/hour"),
    "conversation_read": config("THROTTLE_CONVERSATION_READ", default="120/min"),
    "conversation_request": config("THROTTLE_CONVERSATION_REQUEST", default="60/hour"),
    "media_upload": config("THROTTLE_MEDIA_UPLOAD", default="30/hour"),
    "notification_read": config("THROTTLE_NOTIFICATION_READ", default="240/min"),
    "promotion_performance": config(
        "THROTTLE_PROMOTION_PERFORMANCE", default="120/min"
    ),
    "staff_password_change": config(
        "THROTTLE_STAFF_PASSWORD_CHANGE", default="10/hour"
    ),
    "staff_security_questions": config(
        "THROTTLE_STAFF_SECURITY_QUESTIONS", default="10/hour"
    ),
    "staff_recovery_init": config("THROTTLE_STAFF_RECOVERY_INIT", default="10/hour"),
    "staff_recovery_verify": config(
        "THROTTLE_STAFF_RECOVERY_VERIFY", default="10/hour"
    ),
    "staff_mfa_verify": config("THROTTLE_STAFF_MFA_VERIFY", default="10/hour"),
    "staff_mfa_resend": config("THROTTLE_STAFF_MFA_RESEND", default="5/hour"),
}

MARKETPLACE_SESSION_MAX_AGE_SECONDS = config(
    "MARKETPLACE_SESSION_MAX_AGE_SECONDS", default=30 * 24 * 60 * 60, cast=int
)
STAFF_MFA_MAX_AGE_SECONDS = config(
    "STAFF_MFA_MAX_AGE_SECONDS", default=8 * 60 * 60, cast=int
)
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Nigeria Artisan Marketplace API",
    "DESCRIPTION": "API for customers, artisans, services, bookings, payments, verification, reviews and marketplace operations.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "REDOC_DIST": "https://cdn.redoc.ly/redoc/v2.5.3",
    "COMPONENT_SPLIT_REQUEST": True,
    "SORT_OPERATIONS": False,
    "ENUM_NAME_OVERRIDES": {
        "BookingStatusEnum": "booking.models.BOOKING_STATUS_CHOICES",
        "PromotionCampaignStatusEnum": "promotions.models.PROMOTION_CAMPAIGN_STATUS_CHOICES",
        "SupportCaseStatusEnum": "support.models.SUPPORT_CASE_STATUS_CHOICES",
        "SupportFinancialActionStatusEnum": "support.models.SUPPORT_FINANCIAL_ACTION_STATUS_CHOICES",
        "PromotionApplicationStatusEnum": "promotions.models.PROMOTION_APPLICATION_STATUS_CHOICES",
        "ServiceRequestUrgencyEnum": "services.models.SERVICE_REQUEST_URGENCY_CHOICES",
        "SocialMediaPlatformEnum": "home.profile_links.SOCIAL_MEDIA_PLATFORM_CHOICES",
    },
    "SWAGGER_UI_SETTINGS": {
        "deepLinking": True,
        "persistAuthorization": False,
        "displayOperationId": True,
    },
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

STATIC_URL = "/static/"
STATICFILES_DIRS = [os.path.join(BASE_DIR, "static")]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = os.path.join(BASE_DIR, "media")
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

# Conversation uploads are constrained further at the serializer/storage layer.
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024

USE_S3 = config("USE_S3", cast=bool)
if ENVIRONMENT == "production" and not USE_S3:
    raise ImproperlyConfigured(
        "USE_S3 must be enabled in production for persistent media storage."
    )

if USE_S3:
    AWS_ACCESS_KEY_ID = config("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = config("AWS_SECRET_ACCESS_KEY")
    AWS_STORAGE_BUCKET_NAME = config("AWS_STORAGE_BUCKET_NAME")
    AWS_S3_REGION_NAME = config("AWS_S3_REGION_NAME", default=None)
    AWS_S3_ENDPOINT_URL = config("AWS_S3_ENDPOINT_URL", default=None)
    AWS_DEFAULT_ACL = None
    AWS_QUERYSTRING_AUTH = config("AWS_QUERYSTRING_AUTH", default=True, cast=bool)
    AWS_QUERYSTRING_EXPIRE = config("AWS_QUERYSTRING_EXPIRE", default=300, cast=int)
    AWS_S3_FILE_OVERWRITE = False
    STORAGES = {
        "default": {"BACKEND": "storages.backends.s3.S3Storage"},
        "staticfiles": {
            "BACKEND": (
                "whitenoise.storage.CompressedManifestStaticFilesStorage"
                if not DEBUG
                else "django.contrib.staticfiles.storage.StaticFilesStorage"
            )
        },
    }
else:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": (
                "whitenoise.storage.CompressedManifestStaticFilesStorage"
                if not DEBUG
                else "django.contrib.staticfiles.storage.StaticFilesStorage"
            )
        },
    }

# Transport security is secure by default in production. The proxy header is safe only when
# the app cannot be reached directly around the trusted TLS-terminating proxy.
SECURE_SSL_REDIRECT = config(
    "SECURE_SSL_REDIRECT", default=ENVIRONMENT == "production", cast=bool
)
SECURE_REDIRECT_EXEMPT = [r"^health/"]
TRUST_PROXY_SSL_HEADER = config(
    "TRUST_PROXY_SSL_HEADER", default=ENVIRONMENT == "production", cast=bool
)
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https") if TRUST_PROXY_SSL_HEADER else None
)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = config(
    "SESSION_COOKIE_SECURE", default=ENVIRONMENT == "production", cast=bool
)
SESSION_COOKIE_SAMESITE = config(
    "SESSION_COOKIE_SAMESITE",
    default="None" if ENVIRONMENT == "production" else "Lax",
)
CSRF_COOKIE_SECURE = config(
    "CSRF_COOKIE_SECURE", default=ENVIRONMENT == "production", cast=bool
)
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_HSTS_SECONDS = config(
    "SECURE_HSTS_SECONDS",
    default=0,
    cast=int,
)
SECURE_HSTS_INCLUDE_SUBDOMAINS = config(
    "SECURE_HSTS_INCLUDE_SUBDOMAINS", default=False, cast=bool
)
SECURE_HSTS_PRELOAD = config("SECURE_HSTS_PRELOAD", default=False, cast=bool)
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_RESOURCE_POLICY = "same-origin"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

if ENVIRONMENT == "production":
    if not SECURE_SSL_REDIRECT:
        raise ImproperlyConfigured(
            "SECURE_SSL_REDIRECT must remain enabled in production."
        )
    if not SESSION_COOKIE_SECURE or not CSRF_COOKIE_SECURE:
        raise ImproperlyConfigured(
            "Production session and CSRF cookies must be Secure."
        )
    # Apple web callbacks use cross-site form_post; these cookies still require HTTPS and CSRF/state checks.
    if SESSION_COOKIE_SAMESITE.lower() != "none":
        raise ImproperlyConfigured(
            "Production Apple web callbacks require SESSION_COOKIE_SAMESITE=None."
        )
    if SECURE_HSTS_SECONDS < 1:
        raise ImproperlyConfigured(
            "SECURE_HSTS_SECONDS must be enabled in production after HTTPS is verified."
        )

EMAIL_BACKEND = config(
    "EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend"
)
EMAIL_HOST = config("EMAIL_HOST", default="smtp.gmail.com")
EMAIL_PORT = config("EMAIL_PORT", default=465, cast=int)
EMAIL_USE_SSL = config("EMAIL_USE_SSL", default=True, cast=bool)
EMAIL_USE_TLS = config("EMAIL_USE_TLS", default=False, cast=bool)
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
DEFAULT_FROM_EMAIL = config(
    "DEFAULT_FROM_EMAIL", default=EMAIL_HOST_USER or "no-reply@example.com"
)
EMAIL_TIMEOUT = config("EMAIL_TIMEOUT", default=10, cast=float)

if ENVIRONMENT == "production" and EMAIL_BACKEND.endswith("smtp.EmailBackend"):
    if not EMAIL_HOST_USER or not EMAIL_HOST_PASSWORD:
        raise ImproperlyConfigured(
            "Production SMTP email requires EMAIL_HOST_USER and EMAIL_HOST_PASSWORD. For Gmail use a 16-character App Password."
        )

if EMAIL_USE_SSL and EMAIL_USE_TLS:
    raise ImproperlyConfigured(
        "EMAIL_USE_SSL and EMAIL_USE_TLS cannot both be enabled."
    )

if EMAIL_TIMEOUT <= 0:
    raise ImproperlyConfigured("EMAIL_TIMEOUT must be greater than zero.")

CELERY_BROKER_URL = config("CELERY_BROKER_URL", default=REDIS_URL)
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default=REDIS_URL)
if ENVIRONMENT == "production" and not CELERY_BROKER_URL:
    raise ImproperlyConfigured("CELERY_BROKER_URL must be configured in production.")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_ALWAYS_EAGER = False
CELERY_TASK_IGNORE_RESULT = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
# Hard limits so a hung provider call or SMTP socket can never wedge a worker forever.
CELERY_TASK_SOFT_TIME_LIMIT = config(
    "CELERY_TASK_SOFT_TIME_LIMIT", default=120, cast=int
)
CELERY_TASK_TIME_LIMIT = config("CELERY_TASK_TIME_LIMIT", default=180, cast=int)
# With acks_late on Redis, an unacknowledged task is redelivered after this many seconds; keep it well above
# the longest ETA/countdown used by retries so retries are not executed twice.
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "visibility_timeout": config("CELERY_VISIBILITY_TIMEOUT", default=7200, cast=int)
}
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
PROMOTION_DEFAULT_TARGET_CAPACITY = config(
    "PROMOTION_DEFAULT_TARGET_CAPACITY", default=20, cast=int
)
PROMOTION_HOMEPAGE_CAPACITY = config("PROMOTION_HOMEPAGE_CAPACITY", default=3, cast=int)
PROMOTION_HOMEPAGE_DISPLAY_LIMIT = config(
    "PROMOTION_HOMEPAGE_DISPLAY_LIMIT", default=3, cast=int
)
PROMOTION_DISCOVERY_DISPLAY_LIMIT = config(
    "PROMOTION_DISCOVERY_DISPLAY_LIMIT", default=3, cast=int
)
PROMOTION_MARKET_PICKS_DISPLAY_LIMIT = config(
    "PROMOTION_MARKET_PICKS_DISPLAY_LIMIT", default=6, cast=int
)
PROMOTION_PERFORMANCE_DEDUPE_DAYS = config(
    "PROMOTION_PERFORMANCE_DEDUPE_DAYS", default=2, cast=int
)
PROMOTION_PAYMENT_EXPIRY_MINUTES = config(
    "PROMOTION_PAYMENT_EXPIRY_MINUTES", default=30, cast=int
)

CELERY_TASK_ROUTES = {
    # Authentication and email lifecycle.
    "home.tasks.send_verification_email": {"queue": "notifications"},
    "home.tasks.send_password_reset_email": {"queue": "notifications"},
    "home.tasks.send_otp_email": {"queue": "notifications"},
    "home.tasks.delete_unverified_accounts": {"queue": "maintenance"},
    "home.tasks.purge_expired_staff_security_challenges": {"queue": "maintenance"},
    # KYC and verification.
    "services.tasks.verify_nin_async": {"queue": "verification"},
    "services.tasks.initiate_bvn_async": {"queue": "verification"},
    "services.tasks.request_bvn_otp_async": {"queue": "verification"},
    "services.tasks.complete_bvn_async": {"queue": "verification"},
    # Conversation automation and media.
    "conversations.tasks.expire_conversation_offers": {"queue": "maintenance"},
    "conversations.tasks.send_conversation_reminders": {"queue": "notifications"},
    "conversations.signals_tasks.scan_conversation_attachment": {"queue": "media"},
    # Notifications.
    "notifications.tasks.send_email_notification": {"queue": "notifications"},
    "notifications.tasks.fanout_service_request": {"queue": "notifications"},
    # Payments, escrow and payout reconciliation.
    "payments.tasks.initialize_prepared_payment": {"queue": "payments"},
    "payments.tasks.verify_flutterwave_payment": {"queue": "payments"},
    "payments.tasks.release_artisan_payout": {"queue": "payments"},
    "payments.tasks.reconcile_artisan_payout": {"queue": "payments"},
    "payments.tasks.reconcile_payment_initialization": {"queue": "payments"},
    "payments.tasks.reconcile_stale_payments": {"queue": "maintenance"},
    "payments.tasks.reconcile_financial_ledger": {"queue": "maintenance"},
    "support.tasks.reconcile_support_refund": {"queue": "payments"},
    "promotions.tasks.initialize_promotion_payment": {"queue": "payments"},
    "promotions.tasks.reconcile_promotion_payment_initialization": {
        "queue": "payments"
    },
    "promotions.tasks.verify_promotion_payment": {"queue": "payments"},
    "promotions.tasks.reconcile_promotion_campaigns": {"queue": "maintenance"},
    "promotions.maintenance_tasks.purge_performance_dedupe_rows": {
        "queue": "maintenance"
    },
    # Durable outbox and retention maintenance.
    "core.tasks.dispatch_outbox_events": {"queue": "maintenance"},
    "core.tasks.purge_core_retention": {"queue": "maintenance"},
    "core.tasks.purge_conversation_data": {"queue": "maintenance"},
    "conversations.tasks.purge_finalized_conversation_images": {"queue": "maintenance"},
    "core.tasks.replay_dead_letter_outbox": {"queue": "maintenance"},
}


NOTIFICATION_REDIS_URL = config("NOTIFICATION_REDIS_URL", default=REDIS_URL)
NOTIFICATION_SENT_TTL = config("NOTIFICATION_SENT_TTL", default=2592000, cast=int)
NOTIFICATION_LOCK_TTL = config("NOTIFICATION_LOCK_TTL", default=300, cast=int)


# Fraud and abuse controls
CANCELLATION_RISK_WINDOW_DAYS = config(
    "CANCELLATION_RISK_WINDOW_DAYS", default=90, cast=int
)
CANCELLATION_SUSPENSION_SCORE = config(
    "CANCELLATION_SUSPENSION_SCORE", default=5, cast=int
)
CANCELLATION_REVIEW_SCORE = config("CANCELLATION_REVIEW_SCORE", default=4, cast=int)
NETWORK_OBSERVATION_DAYS = config("NETWORK_OBSERVATION_DAYS", default=30, cast=int)
NETWORK_BLOCK_FIRST_HOURS = config("NETWORK_BLOCK_FIRST_HOURS", default=24, cast=int)
NETWORK_BLOCK_ESCALATED_DAYS = config(
    "NETWORK_BLOCK_ESCALATED_DAYS", default=7, cast=int
)
NETWORK_BLOCK_SHARED_CUSTOMER_LIMIT = config(
    "NETWORK_BLOCK_SHARED_CUSTOMER_LIMIT", default=3, cast=int
)
NETWORK_MULTI_ACCOUNT_ESCALATION = config(
    "NETWORK_MULTI_ACCOUNT_ESCALATION", default=2, cast=int
)
TRUSTED_PROXY_IPS = config("TRUSTED_PROXY_IPS", default="", cast=Csv())
# Number of platform proxy hops in front of the app whose X-Forwarded-For entries are trusted (client IP is that
# many entries from the right). Defaults to the same value DRF uses for throttling.
TRUSTED_PROXY_COUNT = config(
    "TRUSTED_PROXY_COUNT", default=REST_FRAMEWORK["NUM_PROXIES"], cast=int
)

NOTIFICATION_SUPPORT_EMAILS = [
    email.strip()
    for email in config("NOTIFICATION_SUPPORT_EMAILS", default="").split(",")
    if email.strip()
]

MONO_SECRET_KEY = config("MONO_SECRET_KEY")
MONO_BASE_URL = config("MONO_BASE_URL", default="https://api.withmono.com")

KYC_ENCRYPTION_KEY = config("KYC_ENCRYPTION_KEY")

FLW_API_BASE_URL = config("FLW_API_BASE_URL", default="https://api.flutterwave.com/v3")
FLW_SECRET_KEY = config("FLW_SECRET_KEY")
FLW_PUBLIC_KEY = config("FLW_PUBLIC_KEY")
FLW_SECRET_HASH = config("FLW_SECRET_HASH")
FLW_REDIRECT_URL = config("FLW_REDIRECT_URL")
FLW_TRANSFER_CALLBACK_URL = config("FLW_TRANSFER_CALLBACK_URL")
FLW_PAYMENT_TITLE = config("FLW_PAYMENT_TITLE", default="Artisan Marketplace")
FLW_TIMEOUT_SECONDS = config("FLW_TIMEOUT_SECONDS", default=15, cast=int)
MAX_SERVICE_AMOUNT_KOBO = config(
    "MAX_SERVICE_AMOUNT_KOBO", default=100_000_000_000, cast=int
)
AUDIT_LOG_RETENTION_DAYS = config("AUDIT_LOG_RETENTION_DAYS", default=365, cast=int)
OUTBOX_RETENTION_DAYS = config("OUTBOX_RETENTION_DAYS", default=30, cast=int)
IDEMPOTENCY_RETENTION_DAYS = config("IDEMPOTENCY_RETENTION_DAYS", default=2, cast=int)
# Whole-conversation purging is retired; keep the legacy setting readable but unused.
CONVERSATION_RETENTION_DAYS = config(
    "CONVERSATION_RETENTION_DAYS",
    default=365 if ENVIRONMENT == "production" else 0,
    cast=int,
)
CONVERSATION_IMAGE_RETENTION_DAYS = config(
    "CONVERSATION_IMAGE_RETENTION_DAYS", default=7, cast=int
)
REQUIRE_MEDIA_SCAN = config(
    "REQUIRE_MEDIA_SCAN", default=ENVIRONMENT == "production", cast=bool
)
CLAMAV_SCAN_URL = config("CLAMAV_SCAN_URL", default="")
MEDIA_SCAN_TIMEOUT_SECONDS = config("MEDIA_SCAN_TIMEOUT_SECONDS", default=15, cast=int)
if not 1 <= MEDIA_SCAN_TIMEOUT_SECONDS <= 120:
    raise ImproperlyConfigured(
        "MEDIA_SCAN_TIMEOUT_SECONDS must be between 1 and 120 seconds."
    )
REQUEST_TIMEOUT_LOG_SLOW_MS = config(
    "REQUEST_TIMEOUT_LOG_SLOW_MS", default=200, cast=int
)

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

ARTISAN_API_BASE_URL = config(
    "ARTISAN_API_BASE_URL", default="https://127.0.0.1:8000/api/v1"
).rstrip("/")
ARTISAN_API_ROOT_URL = config(
    "ARTISAN_API_ROOT_URL", default="https://127.0.0.1:8000"
).rstrip("/")
ARTISAN_API_TIMEOUT = config("ARTISAN_API_TIMEOUT", default=20, cast=int)
# "inprocess" calls the DRF views inside the same worker (no loopback HTTP, no worker-pool deadlock);
# "http" talks to ARTISAN_API_BASE_URL when the frontend runs as a separate service.
ARTISAN_API_TRANSPORT = config("ARTISAN_API_TRANSPORT", default="inprocess")
if ARTISAN_API_TRANSPORT not in {"inprocess", "http"}:
    raise ValueError("ARTISAN_API_TRANSPORT must be 'inprocess' or 'http'.")

# Social provider settings are deliberately empty by default. Supply real provider-console values
# through the API server environment; never place these values in the frontend or mobile clients.
SOCIAL_AUTH_HTTP_TIMEOUT_SECONDS = config(
    "SOCIAL_AUTH_HTTP_TIMEOUT_SECONDS", default=8, cast=int
)
SOCIAL_AUTH_GOOGLE_WEB_CLIENT_ID = config(
    "SOCIAL_AUTH_GOOGLE_WEB_CLIENT_ID", default=""
)
SOCIAL_AUTH_GOOGLE_WEB_CLIENT_SECRET = config(
    "SOCIAL_AUTH_GOOGLE_WEB_CLIENT_SECRET", default=""
)
SOCIAL_AUTH_GOOGLE_WEB_REDIRECT_URI = config(
    "SOCIAL_AUTH_GOOGLE_WEB_REDIRECT_URI", default=""
)
SOCIAL_AUTH_GOOGLE_NATIVE_CLIENT_ID = config(
    "SOCIAL_AUTH_GOOGLE_NATIVE_CLIENT_ID", default=""
)
SOCIAL_AUTH_GOOGLE_NATIVE_CLIENT_SECRET = config(
    "SOCIAL_AUTH_GOOGLE_NATIVE_CLIENT_SECRET", default=""
)
SOCIAL_AUTH_GOOGLE_NATIVE_REDIRECT_URI = config(
    "SOCIAL_AUTH_GOOGLE_NATIVE_REDIRECT_URI", default=""
)
SOCIAL_AUTH_APPLE_WEB_CLIENT_ID = config("SOCIAL_AUTH_APPLE_WEB_CLIENT_ID", default="")
SOCIAL_AUTH_APPLE_WEB_REDIRECT_URI = config(
    "SOCIAL_AUTH_APPLE_WEB_REDIRECT_URI", default=""
)
SOCIAL_AUTH_APPLE_NATIVE_CLIENT_ID = config(
    "SOCIAL_AUTH_APPLE_NATIVE_CLIENT_ID", default=""
)
SOCIAL_AUTH_APPLE_NATIVE_REDIRECT_URI = config(
    "SOCIAL_AUTH_APPLE_NATIVE_REDIRECT_URI", default=""
)
SOCIAL_AUTH_APPLE_TEAM_ID = config("SOCIAL_AUTH_APPLE_TEAM_ID", default="")
SOCIAL_AUTH_APPLE_KEY_ID = config("SOCIAL_AUTH_APPLE_KEY_ID", default="")
SOCIAL_AUTH_APPLE_PRIVATE_KEY = config("SOCIAL_AUTH_APPLE_PRIVATE_KEY", default="")

IDEMPOTENCY_TTL_SECONDS = config("IDEMPOTENCY_TTL_SECONDS", default=86400, cast=int)
JSON_BODY_LIMIT_BYTES = config("JSON_BODY_LIMIT_BYTES", default=262144, cast=int)
SLOW_REQUEST_THRESHOLD_MS = config("SLOW_REQUEST_THRESHOLD_MS", default=200, cast=int)
SECURE_CSP_ENABLED = config(
    "SECURE_CSP_ENABLED", default=ENVIRONMENT == "production", cast=bool
)
SECURE_CSP = config(
    "SECURE_CSP",
    default="default-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'; img-src 'self' data: https:; font-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'",
)
EXPOSE_DB_QUERY_METRICS = config("EXPOSE_DB_QUERY_METRICS", default=False, cast=bool)


# Enable only after private storage, FFmpeg, purge worker and policy rollout checks.
CONVERSATION_RECORDINGS_ENABLED = config(
    "CONVERSATION_RECORDINGS_ENABLED", default=False, cast=bool
)
CONVERSATION_PRIVATE_MEDIA_ROOT = config(
    "CONVERSATION_PRIVATE_MEDIA_ROOT",
    default=str(BASE_DIR / "private_conversation_media"),
)
CONVERSATION_MEDIA_CDN_ENABLED = config(
    "CONVERSATION_MEDIA_CDN_ENABLED", default=False, cast=bool
)
CONVERSATION_MEDIA_CDN_PURGE_HOOK = config(
    "CONVERSATION_MEDIA_CDN_PURGE_HOOK", default=""
)

CONVERSATION_PURGE_LEDGER_PATH = config("CONVERSATION_PURGE_LEDGER_PATH", default="")

# Operator confirmations are explicit gates, never inferred from an application setting.
CONVERSATION_MEDIA_BACKUP_POLICY_CONFIRMED = config(
    "CONVERSATION_MEDIA_BACKUP_POLICY_CONFIRMED", default=False, cast=bool
)
FLW_CHARGEBACK_WEBHOOKS_CONFIRMED = config(
    "FLW_CHARGEBACK_WEBHOOKS_CONFIRMED", default=False, cast=bool
)
CONVERSATION_MEDIA_REPLICA_INVENTORY_CONFIRMED = config(
    "CONVERSATION_MEDIA_REPLICA_INVENTORY_CONFIRMED", default=False, cast=bool
)
CONVERSATION_MEDIA_HISTORICAL_REPLICA_BUCKETS = [
    x.strip()
    for x in config("CONVERSATION_MEDIA_HISTORICAL_REPLICA_BUCKETS", default="").split(
        ","
    )
    if x.strip()
]
CONVERSATION_LEGACY_CDN_ENABLED = config(
    "CONVERSATION_LEGACY_CDN_ENABLED", default=False, cast=bool
)
CONVERSATION_LEGACY_CDN_REVOKE_HOOK = config(
    "CONVERSATION_LEGACY_CDN_REVOKE_HOOK", default=""
)
