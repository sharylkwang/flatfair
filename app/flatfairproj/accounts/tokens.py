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
PENDING_KEY = "totpPendingUser"
SETUP_KEY = "totpSetupUser"
cache = caches["resources"]


def tokenKey():
    return derivedKey(settings.SECRET_KEY, (b"flatfair-jwt-v1"))


def issueToken(purpose, claims, seconds):
    now = int(time.time())
    payload = {"jti": secrets.token_urlsafe(24)}
    payload.update(claims)
    payload.update({"iss": ISSUER, "aud": purpose, "iat": now, "exp": now + seconds})

    return jwt.encode(payload, tokenKey(), algorithm=ALGORITHM)


def readToken(purpose, token):
    if not purpose or not isinstance(token, str):
        return None

    try:
        return jwt.decode(
            token,
            tokenKey(),
            algorithms=[ALGORITHM],
            audience=purpose,
            issuer=ISSUER,
            options={"require": REQUIRED},
        )
    except jwt.PyJWTError:
        return None


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


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def setupLink(user, secret):
    issuer = getattr(settings, "TOTP_ISSUER", "FlatFair")
    label = f"{issuer}:{user.email}"

    return (
        "otpauth://totp/"
        + quote(label)
        + "?secret="
        + secret
        + "&issuer="
        + quote(issuer)
        + "&algorithm=SHA1&digits=6&period=30"
    )


def codeFor(secret, timestamp=None):
    timestamp = int(time.time() if timestamp is None else timestamp) // 30
    padded = secret + "=" * (-len(secret) % 8)
    key = base64.b32decode(padded, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", timestamp), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF

    return f"{value % 1000000:06d}"


def validCode(secret, code):
    for offset in (-1, 0, 1):
        now = time.time()
        shift = offset * 30
        when = now + shift
        made = codeFor(secret, when)

        if hmac.compare_digest(made, code):
            return True

    return False


def recoveryCodes(device, count=10):
    plain = [secrets.token_hex(4).upper() for _ in range(count)]

    RecoveryCode.objects.bulk_create(
        [RecoveryCode(device=device, codeHash=make_password(code)) for code in plain]
    )

    return plain


def code_ok(code, codeHash):
    if "$" not in codeHash:
        return hmac.compare_digest(hashlib.sha256(code.encode()).hexdigest(), codeHash)

    return check_password(code, codeHash)


def useSpare(device, code):
    code = code.replace("-", "").strip().upper()

    if not re.fullmatch("[0-9A-F]{8}", code):
        return False

    unused = device.recoveryCodes.filter(usedAt__isnull=True)

    for spare in unused.order_by("pk"):
        if code_ok(code, spare.codeHash):
            return unused.filter(pk=spare.pk).update(usedAt=timezone.now()) == 1

    return False


def addDevice(user, required=False):
    device, created = TotpDevice.objects.get_or_create(
        user=user, defaults={"secret": new_secret(), "setupRequired": required}
    )

    if created:
        return device, recoveryCodes(device)

    return device, []
