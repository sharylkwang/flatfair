import base64



from cryptography.fernet import Fernet, InvalidToken



from cryptography.hazmat.primitives import hashes



from cryptography.hazmat.primitives.kdf.hkdf import HKDF



from django.conf import settings
from django.core.exceptions import ImproperlyConfigured



from django.db import models


PREFIX = "fernet$"


def derivedKey(seed, info):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(
        seed.encode()
    )


def dataKey():
    if settings.DATA_ENCRYPTION_KEY:
        return settings.DATA_ENCRYPTION_KEY.encode()



    return base64.urlsafe_b64encode(
        derivedKey(settings.DATA_KEY_SEED, b"flatfair-data-v1")
    )


def cipher():
    try:
        return Fernet(dataKey())
    except (ValueError, TypeError):
        raise ImproperlyConfigured("DATA_ENCRYPTION_KEY must be a Fernet key.")


def lockText(text):
    return PREFIX + cipher().encrypt(text.encode()).decode()


def is_locked(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def unlock(token):
    if not is_locked(token):
        raise Exception("This value is not encrypted data.")



    try:
        return cipher().decrypt(token[len(PREFIX) :].encode()).decode()
    except (InvalidToken, UnicodeError):
        raise Exception("This value cannot be read with the current data key.")


class EncryptedTextField(models.TextField):
    def get_prep_value(self, value):
        value = super().get_prep_value(value)



        if value is None:
            return value


        return lockText(value)

    def from_db_value(self, value, expression, connection):
        if is_locked(value):
            return unlock(value)


        return value
