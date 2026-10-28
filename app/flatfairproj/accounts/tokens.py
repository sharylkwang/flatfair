import base64
import hashlib
import hmac
import math
import re
import secrets
import struct
import time
from urllib.parse import quote
import jwt
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import caches
from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone
from storefront.models import ResourceCacheEntry
from .crypto import derivedKey
from .models import RecoveryCode, TotpDevice, PasswordHistory
from django.core.exceptions import ValidationError

PASSWORD_HISTORY = 3

PASSWORD_RULES = [
    ("length", r".{8,}", "8+ characters", "at least 8 characters"),
    ("upper", r"[A-Z]", "Upper case", "an upper case letter"),
    ("lower", r"[a-z]", "Lower case", "a lower case letter"),
    ("digit", r"[0-9]", "Number", "a number"),
    ("symbol", r"[^A-Za-z0-9]", "Symbol", "a symbol like ! or #"),
]

REUSED = "You can't reuse any of your last 3 passwords. Pick a new one."


def rulesLeft(password):
    missing = []

    for key, pattern, label, words in PASSWORD_RULES:
        if not re.search(pattern, password):
            missing.append(words)

    return missing


def joinWords(words):
    if len(words) == 1:
        return words[0]

    return ", ".join(words[:-1]) + " and " + words[-1]


class MixedCharactersValidator:
    def validate(self, password, user=None):
        missing = rulesLeft(password)

        if missing:
            raise ValidationError(
                f"Your password needs {joinWords(missing)}.", code="password_too_simple"
            )

    def get_help_text(self):
        return "Use 8 or more characters with upper and lower case letters, a number and a symbol."


def wasUsed(user, password):
    hashes = [user.password] + list(
        PasswordHistory.objects.filter(user=user)
        .order_by("-createdAt", "-pk")
        .values_list("passwordHash", flat=True)[: PASSWORD_HISTORY - 1]
    )

    for oldHash in hashes:
        if oldHash and check_password(password, oldHash):
            return True

    return False


def keepPass(user):
    if user.password and user.has_usable_password():
        PasswordHistory.objects.create(user=user, passwordHash=user.password)

    extra = list(
        PasswordHistory.objects.filter(user=user)
        .order_by("-createdAt", "-pk")
        .values_list("pk", flat=True)[PASSWORD_HISTORY - 1 :]
    )

    PasswordHistory.objects.filter(pk__in=extra).delete()


ISSUER = "flatfair"
ALGORITHM = "HS256"
REQUIRED = ["iss", "aud", "iat", "exp", "jti"]
cache = caches["resources"]


def addTry(key, timeout):
    with transaction.atomic():
        entries = ResourceCacheEntry.objects.filter(cacheKey=cache.make_key(key))

        if connection.vendor == "sqlite":
            entries.update(value=F("value"))

        cache.add(key, 0, timeout)
        entry = entries.select_for_update().get()
        tries = cache.get(key, 0) + 1
        left = max(1, math.ceil((entry.expires - timezone.now()).total_seconds()))
        cache.set(key, tries, left)

        return tries


def code_ok(code, codeHash):
    if "$" not in codeHash:
        return hmac.compare_digest(hashlib.sha256(code.encode()).hexdigest(), codeHash)

    return check_password(code, codeHash)


