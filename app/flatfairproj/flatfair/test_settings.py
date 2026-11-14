"""Isolated settings for the local test suite."""

import os
import tempfile

os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("SECRET_KEY", "flatfair-unit-tests-only")

from .settings import *

# Catch file DB locks too.
_test_folder = tempfile.TemporaryDirectory(prefix="flatfair-test-db-")
DATABASES["default"].setdefault("TEST", {})["NAME"] = os.path.join(_test_folder.name, "db.sqlite3")

SECRET_KEY = "flatfair-unit-tests-only"
DATA_KEY_SEED = SECRET_KEY
DATA_ENCRYPTION_KEY = ""
DEBUG = True
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
PUBLIC_URL = ""
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
EMAIL_HOST = ""
EMAIL_HOST_USER = ""
EMAIL_HOST_PASSWORD = ""
DEFAULT_FROM_EMAIL = "FlatFair <no-reply@flatfair.sg>"
GOOGLE_CLIENT_ID = ""
GOOGLE_CLIENT_SECRET = ""
STRIPE_SECRET_KEY = ""
TURNSTILE_ENABLED = False
TURNSTILE_GATE = False
TURNSTILE_SITE_KEY = ""
TURNSTILE_SECRET_KEY = ""
NOTICE_MAIL_ASYNC = False
PHOTO_WORKER_ON = False
FLATFAIR_GPU = "off"

# Hashing tests override this with the real production hashers.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
]
