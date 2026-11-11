import hashlib
from importlib import import_module

from cryptography.fernet import Fernet
from django.apps import apps
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.db.models import CharField, Value
from django.db.models.functions import Cast
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.crypto import dataKey, unlock, lockText
from accounts.models import TotpDevice, PasswordHistory, RecoveryCode
from accounts.tokens import PENDING_KEY, codeFor, wasUsed, cache, recoveryCodes, useSpare
from accounts.verification import totpEnabled, needsTotp
from flatfair.settings import PASSWORD_HASHERS as PRODUCTION_PASSWORD_HASHERS
from storefront.tests.helpers import makeUser, PASSWORD

SECRET = "JBSWY3DPEHPK3PXP"


def otherKey():
    return Fernet.generate_key().decode()


class TextEncryptionTests(TestCase):

    def test_reads_back_not_stored(self):
        token = lockText("asdf qwer")
        self.assertTrue(token.startswith("fernet$"))
        self.assertNotIn("asdf", token)
        self.assertEqual(unlock(token), "asdf qwer")
        self.assertNotEqual(token, lockText("asdf qwer"))

    def test_wrong_key_fails_cleanly(self):
        token = lockText("asdf")
        with override_settings(DATA_ENCRYPTION_KEY=otherKey()):
            with self.assertRaisesRegex(Exception, "This value (is not encrypted data|cannot be read with the current data key)\\."):
                unlock(token)
        self.assertEqual(unlock(token), "asdf")

    def test_changed_plain_refused(self):
        token = lockText("asdf")
        changed = token[:-6] + ("AAAAAA" if not token.endswith("AAAAAA") else "BBBBBB")
        for value in (changed, "asdf", "", "fernet$", "fernet$asdf", None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(Exception, "This value (is not encrypted data|cannot be read with the current data key)\\."):
                    unlock(value)

    @override_settings(DATA_ENCRYPTION_KEY="")
    def test_key_comes_secret_stays(self):
        first = dataKey()
        self.assertEqual(first, dataKey())
        self.assertEqual(len(first), 44)
        self.assertNotIn(first.decode().rstrip("="), settings.SECRET_KEY)
        token = lockText("asdf")
        self.assertEqual(unlock(token), "asdf")
        with override_settings(DATA_KEY_SEED="another secret key"):
            self.assertNotEqual(dataKey(), first)
            with self.assertRaisesRegex(Exception, "This value (is not encrypted data|cannot be read with the current data key)\\."):
                unlock(token)

    def test_own_key_setting_used(self):
        key = otherKey()
        with override_settings(DATA_ENCRYPTION_KEY=key):
            self.assertEqual(dataKey(), key.encode())
            token = lockText("asdf")
            self.assertEqual(unlock(token), "asdf")
        with override_settings(DATA_ENCRYPTION_KEY=""):
            with self.assertRaisesRegex(Exception, "This value (is not encrypted data|cannot be read with the current data key)\\."):
                unlock(token)

    @override_settings(DATA_ENCRYPTION_KEY="asdf")
    def test_bad_key_setting_is_reported(self):
        with self.assertRaises(ImproperlyConfigured):
            lockText("asdf")


class TotpSecretEncryptionTests(TestCase):

    def setUp(self):
        self.user = makeUser("secret@example.com")
        self.device = TotpDevice.objects.create(user=self.user, secret=SECRET, enabledAt=timezone.now())

    def stored(self):
        return TotpDevice.objects.annotate(stored=Cast("secret", CharField())).get(pk=self.device.pk).stored

    def storePlain(self):
        plain = Value(SECRET, output_field=CharField())
        TotpDevice.objects.filter(pk=self.device.pk).update(secret=plain)

    def test_not_readable_in_database(self):
        stored = self.stored()
        self.assertTrue(stored.startswith("fernet$"))
        self.assertNotIn(SECRET, stored)
        self.assertEqual(unlock(stored), SECRET)

    def test_reads_back_through_model(self):
        for saved in [
            self.device.secret,
            TotpDevice.objects.get(pk=self.device.pk).secret,
            User.objects.get(pk=self.user.pk).totpDevice.secret,
            TotpDevice.objects.values_list("secret", flat=True).get(pk=self.device.pk),
        ]:
            self.assertEqual(saved, SECRET)

    def test_saving_other_fields_keeps(self):
        device = TotpDevice.objects.get(pk=self.device.pk)
        device.setupRequired = True
        device.save()
        before = self.stored()
        device.save(update_fields=["setupRequired"])
        self.assertEqual(self.stored(), before)
        self.assertEqual(TotpDevice.objects.get(pk=self.device.pk).secret, SECRET)

    def test_wrong_key_fails_cleanly(self):
        with override_settings(DATA_ENCRYPTION_KEY=otherKey()):
            with self.assertRaisesRegex(Exception, "This value (is not encrypted data|cannot be read with the current data key)\\."):
                TotpDevice.objects.get(pk=self.device.pk)

    def test_wrong_key_not_switch(self):
        with override_settings(DATA_ENCRYPTION_KEY=otherKey()):
            for check in (totpEnabled, needsTotp):
                with self.subTest(check=check.__name__):
                    with self.assertRaisesRegex(Exception, "This value (is not encrypted data|cannot be read with the current data key)\\."):
                        check(User.objects.get(pk=self.user.pk))
        self.assertTrue(totpEnabled(User.objects.get(pk=self.user.pk)))
        self.assertFalse(totpEnabled(makeUser("none@example.com")))

    def test_old_plain_reads_converted(self):
        self.storePlain()
        self.assertEqual(self.stored(), SECRET)
        self.assertEqual(TotpDevice.objects.get(pk=self.device.pk).secret, SECRET)

    def test_data_migration_encrypts_old(self):
        migration = import_module("accounts.migrations.0005_email")
        self.storePlain()
        migration.old_0008_encryptSecrets(apps, None)
        self.assertTrue(self.stored().startswith("fernet$"))
        self.assertEqual(TotpDevice.objects.get(pk=self.device.pk).secret, SECRET)
        migration.old_0008_decryptSecrets(apps, None)
        self.assertEqual(self.stored(), SECRET)
        migration.old_0008_encryptSecrets(apps, None)
        self.assertEqual(unlock(self.stored()), SECRET)

    def test_authenticator_code_signs_in(self):
        session = self.client.session
        session[PENDING_KEY] = {"userId": self.user.pk, "path": "/"}
        session.save()
        response = self.client.post(reverse("authenticator"), {"code": codeFor(SECRET)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))

    def test_setup_page_creates_encrypted(self):
        user = makeUser("setup@example.com")
        self.client.force_login(user)
        response = self.client.get(reverse("authenticator_setup"))
        device = TotpDevice.objects.get(user=user)
        stored = TotpDevice.objects.annotate(stored=Cast("secret", CharField())).get(pk=device.pk).stored
        self.assertTrue(stored.startswith("fernet$"))
        self.assertNotIn(device.secret, stored)
        self.assertContains(response, device.secret)
        response = self.client.post(reverse("authenticator_setup"), {"code": codeFor(device.secret)})
        self.assertRedirects(response, reverse("account_settings"), fetch_redirect_response=False)
        self.assertTrue(totpEnabled(User.objects.get(pk=user.pk)))


NEW = "Qwer-5678!zxcv"


@override_settings(PASSWORD_HASHERS=PRODUCTION_PASSWORD_HASHERS)
class PasswordHashingTests(TestCase):

    def setUp(self):
        cache.clear()

    def oldUser(self):
        user = makeUser("old@example.com")
        User.objects.filter(pk=user.pk).update(password=make_password(PASSWORD, hasher="pbkdf2_sha256"))
        user.refresh_from_db()
        return user

    def test_new_stored_bcrypt(self):
        user = makeUser("new@example.com", password=PASSWORD)
        self.assertTrue(user.password.startswith("bcrypt_sha256$"))
        self.assertNotIn(PASSWORD, user.password)
        for typed, works in [(PASSWORD, True), (PASSWORD + "x", False)]:
            with self.subTest(typed=typed):
                self.assertEqual(user.check_password(typed), works)

    def test_same_gets_different_hash(self):
        first = make_password(PASSWORD)
        second = make_password(PASSWORD)
        self.assertTrue(first.startswith("bcrypt_sha256$"))
        self.assertNotEqual(first, second)

    def test_old_pbkdf_2_hash(self):
        user = self.oldUser()
        self.assertTrue(user.password.startswith("pbkdf2_sha256$"))
        response = self.client.post(reverse("signin"), {"email": user.email, "password": PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))
        user.refresh_from_db()
        self.assertTrue(user.password.startswith("bcrypt_sha256$"))
        self.assertTrue(user.check_password(PASSWORD))

    def test_wrong_not_sign_in(self):
        user = self.oldUser()
        response = self.client.post(reverse("signin"), {"email": user.email, "password": PASSWORD + "x"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        user.refresh_from_db()
        self.assertTrue(user.password.startswith("pbkdf2_sha256$"))

    def test_changed_stored_bcrypt(self):
        user = self.oldUser()
        self.client.force_login(user)
        data = {
            "action": "password",
            "password-currentPassword": PASSWORD,
            "password-password": NEW,
            "password-confirmPassword": NEW,
        }
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("account_settings"), data)
        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertTrue(user.password.startswith("bcrypt_sha256$"))
        self.assertTrue(user.check_password(NEW))
        self.assertFalse(user.check_password(PASSWORD))
        self.assertTrue(wasUsed(user, PASSWORD))

    def test_reuse_check_reads_old(self):
        user = makeUser("history@example.com", password=NEW)
        PasswordHistory.objects.create(
            user=user, passwordHash=make_password(PASSWORD, hasher="pbkdf2_sha256")
        )
        for typed, used in [(PASSWORD, True), (NEW, True), ("Zxcv-9876!asdf", False)]:
            with self.subTest(typed=typed):
                self.assertEqual(wasUsed(user, typed), used)


@override_settings(PASSWORD_HASHERS=PRODUCTION_PASSWORD_HASHERS)
class RecoveryCodeHashingTests(TestCase):

    def setUp(self):
        self.user = makeUser("codes@example.com")
        self.device = TotpDevice.objects.create(
            user=self.user, secret="JBSWY3DPEHPK3PXP", enabledAt=timezone.now()
        )

    def legacyCode(self, code):
        return RecoveryCode.objects.create(
            device=self.device, codeHash=hashlib.sha256(code.encode()).hexdigest()
        )

    def test_stored_hashed_bcrypt(self):
        codes = recoveryCodes(self.device, count=2)
        stored = list(self.device.recoveryCodes.values_list("codeHash", flat=True))
        self.assertEqual(len(stored), 2)
        for code in codes:
            self.assertRegex(code, "^[0-9A-F]{8}$")
            for codeHash in stored:
                self.assertTrue(codeHash.startswith("bcrypt_sha256$"))
                self.assertNotIn(code, codeHash)
                self.assertNotIn(hashlib.sha256(code.encode()).hexdigest(), codeHash)

    def test_recovery_code_works_once(self):
        codes = recoveryCodes(self.device, count=2)
        self.assertTrue(useSpare(self.device, codes[1]))
        self.assertFalse(useSpare(self.device, codes[1]))
        self.assertEqual(self.device.recoveryCodes.filter(usedAt__isnull=False).count(), 1)
        self.assertTrue(useSpare(self.device, codes[0][:4].lower() + "-" + codes[0][4:].lower()))
        self.assertFalse(self.device.recoveryCodes.filter(usedAt__isnull=True).exists())

    def test_wrong_malformed_refused(self):
        codes = recoveryCodes(self.device, count=1)
        wrong = "0000FFFF" if codes[0] != "0000FFFF" else "FFFF0000"
        for typed in (wrong, "", "hello", "123456", codes[0] + "0", "é" * 8):
            with self.subTest(typed=typed):
                self.assertFalse(useSpare(self.device, typed))
        self.assertFalse(self.device.recoveryCodes.filter(usedAt__isnull=False).exists())

    def test_another_device_refused(self):
        other = TotpDevice.objects.create(
            user=makeUser("other@example.com"), secret="JBSWY3DPEHPK3PXQ", enabledAt=timezone.now()
        )
        codes = recoveryCodes(other, count=1)
        self.assertFalse(useSpare(self.device, codes[0]))
        self.assertTrue(useSpare(other, codes[0]))

    def test_old_sha_256_rows(self):
        legacy = self.legacyCode("AB12CD34")
        before = legacy.codeHash
        self.assertFalse(useSpare(self.device, "AB12CD35"))
        self.assertTrue(useSpare(self.device, "ab12-cd34"))
        self.assertFalse(useSpare(self.device, "AB12CD34"))
        legacy.refresh_from_db()
        self.assertEqual(legacy.codeHash, before)
        self.assertEqual(len(legacy.codeHash), 64)
        self.assertIsNotNone(legacy.usedAt)

    def test_old_new_rows_work(self):
        self.legacyCode("AB12CD34")
        codes = recoveryCodes(self.device, count=1)
        self.assertTrue(useSpare(self.device, codes[0]))
        self.assertTrue(useSpare(self.device, "AB12CD34"))

    def test_typing_stored_hash_not(self):
        legacy = self.legacyCode("AB12CD34")
        recoveryCodes(self.device, count=1)
        for codeHash in self.device.recoveryCodes.values_list("codeHash", flat=True):
            self.assertFalse(useSpare(self.device, codeHash))
        self.assertFalse(useSpare(self.device, legacy.codeHash[:8].upper()))


class RecoveryCodeTests(TestCase):

    def setUp(self):
        self.user = makeUser("recover@example.com")
        self.device = TotpDevice.objects.create(
            user=self.user, secret="JBSWY3DPEHPK3PXP", enabledAt=timezone.now()
        )
        self.codes = recoveryCodes(self.device, count=2)
        session = self.client.session
        session[PENDING_KEY] = {"userId": self.user.pk, "path": "/"}
        session.save()

    def signedIn(self):
        return "_auth_user_id" in self.client.session

    def test_recovery_code_signs_in_once(self):
        response = self.client.post(reverse("authenticator"), {"code": self.codes[0].lower()})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.signedIn())
        self.assertEqual(self.device.recoveryCodes.filter(usedAt__isnull=False).count(), 1)

    def test_used_refused(self):
        self.device.recoveryCodes.update(usedAt=timezone.now())
        response = self.client.post(reverse("authenticator"), {"code": self.codes[0]})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.signedIn())
        self.assertContains(response, "it has already been used")

    def test_authenticator_signs_in(self):
        response = self.client.post(reverse("authenticator"), {"code": codeFor(self.device.secret)})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.signedIn())

    def test_other_text_refused_plain(self):
        response = self.client.post(reverse("authenticator"), {"code": "hello"})
        self.assertFalse(self.signedIn())
        self.assertContains(response, "or one of your recovery codes")
