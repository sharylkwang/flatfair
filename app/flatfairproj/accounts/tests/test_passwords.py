from datetime import timedelta
import re
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.forms import NewPasswordForm
from accounts.models import PasswordHistory, ResetCode
from accounts.tokens import MixedCharactersValidator, rulesLeft, wasUsed

OLD = "Zxcv-9876!"
NEW = "Qwer-5678!"


class RuleTests(TestCase):

    def test_password_errors_list_missing(self):
        self.assertEqual(rulesLeft("Qwer-5678!"), [])
        self.assertEqual(
            rulesLeft("qwer"),
            ["at least 8 characters", "an upper case letter", "a number", "a symbol like ! or #"],
        )

    def test_validator_combines_missing(self):
        with self.assertRaisesMessage(
            ValidationError, "Your password needs an upper case letter and a symbol like ! or #."
        ):
            MixedCharactersValidator().validate("qwer5678")

    def test_registration_page_loads(self):
        response = self.client.get(reverse("register"))
        for text in ["data-rule=", "accounts/rules.js"]:
            with self.subTest(text=text):
                self.assertContains(response, text)


class HistoryTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("asdf@test.com", "asdf@test.com", OLD)

    def change(self, password):
        form = NewPasswordForm(self.user, {"password": password, "confirm": password})
        if form.is_valid():
            form.save()
        return form

    def test_current_recent_passwords_blocks(self):
        self.assertIn("password", self.change(OLD).errors)
        self.assertTrue(self.change(NEW).is_valid())
        self.assertTrue(wasUsed(self.user, OLD))
        self.assertIn("last 3 passwords", self.change(OLD).errors["password"][0])

    def test_oldest_password_reused_three(self):
        self.change(NEW)
        self.change("Asdf-1111!")
        self.assertFalse(self.change(OLD).is_valid())
        self.change("Asdf-2222!")
        self.assertEqual(PasswordHistory.objects.filter(user=self.user).count(), 2)
        self.assertTrue(self.change(OLD).is_valid())

    def test_new_password_must_satisfy(self):
        form = self.change("asdfghjkl")
        self.assertIn("upper case letter", form.errors["password"][0])


class ResetTests(TestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("asdf@test.com", "asdf@test.com", OLD, first_name="Wei Ling")

    def post(self, *args, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(*args, **kwargs)

    def askCode(self, email="ASDF@test.com"):
        self.post(reverse("forgot_password"), {"email": email})
        return re.search("\\b(\\d{6})\\b", mail.outbox[-1].body).group(1)

    def wrongCode(self, code):
        if code == "000000":
            return "111111"
        return "000000"

    def test_request_emails_named_user(self):
        response = self.post(reverse("forgot_password"), {"email": self.user.email})
        self.assertRedirects(response, reverse("forgot_code"))
        self.assertEqual(mail.outbox[0].to, [self.user.email])
        self.assertIn("Hi Wei Ling", mail.outbox[0].body)
        self.assertEqual(ResetCode.objects.get().user, self.user)

    def test_unknown_email_same_redirect(self):
        response = self.post(reverse("forgot_password"), {"email": "nobody@test.com"})
        self.assertRedirects(response, reverse("forgot_code"))
        self.assertEqual(len(mail.outbox), 0)
        self.assertContains(self.client.get(reverse("forgot_code")), "nobody@test.com")

    def test_wrong_code_increments_attempts(self):
        code = self.askCode()
        response = self.post(reverse("forgot_code"), {"code": self.wrongCode(code)})
        self.assertContains(response, "That code is wrong or has expired.")
        self.assertEqual(ResetCode.objects.get().attempts, 1)
        self.assertRedirects(self.client.get(reverse("forgot_new")), reverse("forgot_password"))

    def test_expired_code_is_rejected(self):
        code = self.askCode()
        ResetCode.objects.update(expiresAt=timezone.now() - timedelta(minutes=1))
        response = self.post(reverse("forgot_code"), {"code": code})
        self.assertContains(response, "That code is wrong or has expired.")

    def test_five_wrong_codes_lock(self):
        code = self.askCode()
        for i in range(5):
            response = self.post(reverse("forgot_code"), {"code": self.wrongCode(code)})
        self.assertContains(response, "Too many wrong tries.")
        response = self.post(reverse("forgot_code"), {"code": code})
        self.assertContains(response, "Too many wrong tries.")
        self.assertEqual(ResetCode.objects.get().attempts, 5)

    def test_verified_code_changes_password(self):
        code = self.askCode()
        response = self.post(reverse("forgot_code"), {"code": code})
        self.assertRedirects(response, reverse("forgot_new"))
        response = self.post(reverse("forgot_new"), {"password": NEW, "confirm": NEW})
        self.assertRedirects(response, reverse("signin"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW))
        self.assertEqual(mail.outbox[-1].subject, "Your FlatFair password was changed")
        self.assertIsNotNone(ResetCode.objects.get().usedAt)
        self.assertEqual(PasswordHistory.objects.get().user, self.user)

    def test_blocks_reused_weak_passwords(self):
        code = self.askCode()
        self.post(reverse("forgot_code"), {"code": code})
        for password, text in [(OLD, "last 3 passwords"), ("qwerasdf", "Your password needs")]:
            with self.subTest(password=password):
                response = self.post(reverse("forgot_new"), {"password": password, "confirm": password})
                self.assertContains(response, text)
        self.assertEqual(len(mail.outbox), 1)

    def test_code_requests_limited_two(self):
        for i in range(2):
            self.client.cookies.clear()
            self.askCode()
        self.client.cookies.clear()
        response = self.post(reverse("forgot_password"), {"email": self.user.email})
        self.assertContains(response, "asked for too many codes.")
        self.assertEqual(len(mail.outbox), 2)

    def test_resend_waits_replaces_active(self):
        self.askCode()
        response = self.post(reverse("forgot_code"), {"resend": "1"}, follow=True)
        self.assertContains(response, "Send a new code in")
        self.assertEqual(len(mail.outbox), 1)
        session = self.client.session
        session["passwordReset"]["sentAt"] -= 120
        session.save()
        self.post(reverse("forgot_code"), {"resend": "1"})
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(ResetCode.objects.filter(expiresAt__gt=timezone.now()).count(), 1)

    def test_passwordless_account_set(self):
        user = User.objects.create_user("qwer@test.com", "qwer@test.com", None)
        code = self.askCode(user.email)
        self.post(reverse("forgot_code"), {"code": code})
        self.post(reverse("forgot_new"), {"password": NEW, "confirm": NEW})
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW))

    def test_new_code_request_invalidates(self):
        code = self.askCode()
        self.post(reverse("forgot_code"), {"code": code})
        otherClient = Client()
        with self.captureOnCommitCallbacks(execute=True):
            otherClient.post(reverse("forgot_password"), {"email": self.user.email})
        response = self.post(reverse("forgot_new"), {"password": NEW, "confirm": NEW})
        self.assertRedirects(response, reverse("forgot_password"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(OLD))
        self.assertFalse(PasswordHistory.objects.exists())

    def test_expiry_verification_blocks(self):
        code = self.askCode()
        self.post(reverse("forgot_code"), {"code": code})
        ResetCode.objects.update(expiresAt=timezone.now() - timedelta(seconds=1))
        response = self.post(reverse("forgot_new"), {"password": NEW, "confirm": NEW})
        self.assertRedirects(response, reverse("forgot_password"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(OLD))

    def test_concurrent_verification_not(self):
        typed = self.askCode()
        code = ResetCode.objects.get()
        ResetCode.objects.update(verifiedAt=timezone.now())
        with patch("accounts.views.activeCode", return_value=code):
            response = self.post(reverse("forgot_code"), {"code": typed})
        self.assertContains(response, "That code is wrong or has expired.")
        self.assertNotIn("codeId", self.client.session["passwordReset"])

    def test_concurrent_attempt_limit_not(self):
        typed = self.askCode()
        code = ResetCode.objects.get()
        ResetCode.objects.update(attempts=5)
        with patch("accounts.views.activeCode", return_value=code):
            response = self.post(reverse("forgot_code"), {"code": typed})
        self.assertContains(response, "That code is wrong or has expired.")
        self.assertNotIn("codeId", self.client.session["passwordReset"])
        code.refresh_from_db()
        self.assertEqual(code.attempts, 5)

    def test_concurrent_code_consumption(self):
        typed = self.askCode()
        self.post(reverse("forgot_code"), {"code": typed})
        code = ResetCode.objects.select_related("user").get()
        ResetCode.objects.update(usedAt=timezone.now())
        with patch("accounts.views.goodCode", return_value=code):
            response = self.post(reverse("forgot_new"), {"password": NEW, "confirm": NEW})
        self.assertRedirects(response, reverse("forgot_password"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(OLD))
        self.assertFalse(PasswordHistory.objects.exists())
        self.assertEqual(len(mail.outbox), 1)


class ResetIssuanceTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("issue@test.com", "issue@test.com", OLD)
        self.request = RequestFactory().post("/", REMOTE_ADDR="192.0.2.1")

    def test_sequential_expires_previous(self):
        from accounts.views import sendCode

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            self.assertTrue(sendCode(self.request, self.user))
            self.assertTrue(sendCode(self.request, self.user))
            self.assertEqual(len(mail.outbox), 0)
            self.assertEqual(ResetCode.objects.filter(expiresAt__gt=timezone.now()).count(), 1)
        self.assertEqual(len(callbacks), 2)
        self.assertEqual(len(mail.outbox), 2)

    def test_email_shared_ip_quota(self):
        from accounts.views import sendCode

        with self.captureOnCommitCallbacks(execute=True):
            for attempt in range(2):
                self.assertTrue(sendCode(self.request, self.user))
            self.assertFalse(sendCode(self.request, self.user))
            for number in range(8):
                user = User.objects.create_user(f"ip{number}@test.com", f"ip{number}@test.com", OLD)
                self.assertTrue(sendCode(self.request, user))
            user = User.objects.create_user("blocked@test.com", "blocked@test.com", OLD)
            self.assertFalse(sendCode(self.request, user))
        self.assertEqual(ResetCode.objects.count(), 10)
        self.assertEqual(len(mail.outbox), 10)

    def test_rolled_back_not_send(self):
        from accounts.views import sendCode
        from django.db import transaction

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(ValueError), transaction.atomic():
                self.assertTrue(sendCode(self.request, self.user))
                raise ValueError("rollback")
        self.assertEqual(callbacks, [])
        self.assertFalse(ResetCode.objects.exists())
        self.assertEqual(len(mail.outbox), 0)
