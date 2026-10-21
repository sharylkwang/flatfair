from pathlib import Path
import os
import secrets
import sys
from urllib.parse import urlsplit
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
if (BASE_DIR / ".env").is_file():
    for _line in (BASE_DIR / ".env").read_text(encoding="utf-8").splitlines():
        _name, _sep, _value = _line.strip().partition("=")

        if _sep and not _name.startswith("#"):
            DOTENV[_name.strip().removeprefix("export ").strip()] = _value.strip().strip("\"'")


def env_flag(name, default):
    return env(name, default).lower() in ("1", "true", "yes")


DEBUG = env_flag("DEBUG", "false")
SECRET_KEY = env("SECRET_KEY")
DATA_ENCRYPTION_KEY = env("DATA_ENCRYPTION_KEY")
DATA_KEY_SEED = SECRET_KEY or "flatfair-local-demo"

if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("SECRET_KEY must be set when DEBUG is false.")

    SECRET_KEY = secrets.token_urlsafe(48)

if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = int(env("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = env("EMAIL_HOST_USER")
    EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD")
    EMAIL_USE_SSL = env_flag("EMAIL_USE_SSL", "false")
    EMAIL_USE_TLS = env_flag("EMAIL_USE_TLS", "false" if EMAIL_USE_SSL else "true")
    EMAIL_TIMEOUT = 15
else:
    EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
    EMAIL_FILE_PATH = BASE_DIR / "sent_emails"

NOTICE_MAIL_SKIP = (
    "flatfair.sg",



    "test.com",
    "example.com",
    "example.org",
    "example.invalid",


    "example.net",
)

NOTICE_MAIL_ASYNC = sys.argv[1:2] != ["test"]

if "@" not in DEFAULT_FROM_EMAIL and EMAIL_HOST and EMAIL_HOST_USER:
    DEFAULT_FROM_EMAIL = f"{DEFAULT_FROM_EMAIL or 'FlatFair'} <{EMAIL_HOST_USER}>"

FLATFAIR_GPU = "off" if sys.argv[1:2] == ["test"] else env("FLATFAIR_GPU", "auto")
PHOTO_WORKER_ON = sys.argv[1:2] != ["test"]
if TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY and not DEBUG:
    _turnstile_default = "true"

if TURNSTILE_ENABLED and DEBUG and not TURNSTILE_SITE_KEY and not TURNSTILE_SECRET_KEY:
    TURNSTILE_SITE_KEY = "1x00000000000000000000AA"
    TURNSTILE_SECRET_KEY = "1x0000000000000000000000000000000AA"

if TURNSTILE_ENABLED and not (TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY):
    raise ImproperlyConfigured(
        "Set TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY, or TURNSTILE_ENABLED=false."
    )

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("ALLOWED_HOSTS", _default_hosts).split(",")
    if host.strip()
]

SINGLE_ADDRESS = env_flag("SINGLE_ADDRESS", "true")
SINGLE_ADDRESS_HOST = urlsplit(PUBLIC_URL).hostname if PUBLIC_URL else ""
X_FRAME_OPTIONS = "SAMEORIGIN"

if PUBLIC_URL:
    ALLOWED_HOSTS.append(urlsplit(PUBLIC_URL).hostname)
    CSRF_TRUSTED_ORIGINS.append(PUBLIC_URL)
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",



    "django.contrib.messages",
    "django.contrib.staticfiles",
    "storefront.StorefrontConfig",
    "adminpanel.AdminpanelConfig",
    "sellerpanel.SellerpanelConfig",
    "accounts.AccountsConfig",


    "forum.ForumConfig",
    "vision.VisionConfig",
    "django.contrib.humanize",
]

MIDDLEWARE = [
    "flatfair.middleware.RealClientAddressMiddleware",
    "flatfair.middleware.FriendlyDebug404Middleware",


    "django.middleware.security.SecurityMiddleware",
    "flatfair.middleware.SingleAddressMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.turnstile.TurnstileGate",
    "accounts.verification.RequiredTotpSetupMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "flatfair.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],


        "APP_DIRS": True,



        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",



                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "storefront.models.modeContext",
                "accounts.google.googleButton",



                "accounts.turnstile.turnstileField",
            ],
        },
    },
]

WSGI_APPLICATION = "flatfair.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",



        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
    }
}

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "accounts.tokens.MixedCharactersValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Singapore"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "assets" / "static"]
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

if sys.argv[1:2] == ["test"]:
    import atexit, shutil, tempfile

    MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="flatfair-test-media-"))
    atexit.register(shutil.rmtree, MEDIA_ROOT, ignore_errors=True)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "signin"
