from datetime import timedelta
import re
from unittest import skip
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import EmailCheck
from accounts.tokens import cache
from accounts.verification import demoCode
from accounts.views import hashCode
from storefront.models import Profile
from storefront.tests.helpers import makeListing, makeUser


@override_settings(DEBUG=False, EMAIL_HOST="", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailCheckTests(TestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("verify@example.com", "verify@example.com", "Zxcv-9876!")
        Profile.objects.create(user=self.user, role="buyer", canSell=True)
        self.client.force_login(self.user)
        self.url = reverse("email_check")

    def send(self, code=123456):
        with (
            self.captureOnCommitCallbacks(execute=True),
            patch("accounts.verification.secrets.randbelow", return_value=code),
        ):
            response = self.client.post(self.url, {"resend": "1"})
        if response.wsgi_request.session.modified:
            response.wsgi_request.session.save()
        return response

    def wait(self):
        EmailCheck.objects.filter(user=self.user).update(sentAt=timezone.now() - timedelta(seconds=61))

    @skip("Registration now requires email verification before Cloudflare")
    def test_registration_redirects(self):
        self.client.logout()
        with (
            self.captureOnCommitCallbacks(execute=True),
            patch("accounts.verification.secrets.randbelow", return_value=123456),
        ):
            response = self.client.post(
                reverse("register"),
                {
                    "fullName": "Mei Tan",
                    "email": "new@example.com",
                    "password": "Qwer-5678!zx",
                    "confirmPassword": "Qwer-5678!zx",
                    "role": "buyer",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("human_check") + "?next=%2Faccount&force=1")
        user = User.objects.get(email="new@example.com")
        self.assertFalse(EmailCheck.objects.filter(user=user).exists())
        self.assertEqual(mail.outbox, [])
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))

    def test_valid_code_verifies_clears(self):
        self.send()
        self.assertRedirects(self.client.post(self.url, {"code": "123456"}), reverse("home"))
        check = EmailCheck.objects.get(user=self.user)
        self.assertIsNotNone(check.verifiedAt)
        self.assertEqual(check.codeHash, "")
        self.assertEqual(self.client.get(reverse("my_offers")).status_code, 200)
        self.assertRedirects(self.client.post(self.url, {"resend": "1"}), reverse("home"))
        self.assertEqual(len(mail.outbox), 1)

    def test_verifying_returns_page_asked(self):
        self.client.get(reverse("my_offers"))
        self.send()
        self.assertRedirects(self.client.post(self.url, {"code": "123456"}), reverse("my_offers"))

    def test_post_gate_returns_local(self):
        flat = makeListing(makeUser("owner@example.com", "seller"))
        page = reverse("listing_detail", args=[flat.publicId])
        self.client.post(
            reverse("activity_action"),
            {"action": "offer", "id": "1"},
            HTTP_REFERER="http://testserver" + page,
        )
        self.send()
        self.assertRedirects(
            self.client.post(self.url, {"code": "123456"}), page, fetch_redirect_response=False
        )
        self.client.logout()
        other = User.objects.create_user("other@example.com", "other@example.com", "Zxcv-9876!")
        Profile.objects.create(user=other, role="buyer")
        self.client.force_login(other)
        self.client.post(
            reverse("activity_action"),
            {"action": "offer", "id": "1"},
            HTTP_REFERER="https://evil.example/steal",
        )
        with (
            self.captureOnCommitCallbacks(execute=True),
            patch("accounts.verification.secrets.randbelow", return_value=654321),
        ):
            self.client.post(self.url, {"resend": "1"})
        self.assertRedirects(self.client.post(self.url, {"code": "654321"}), reverse("home"))

    def test_expired_code_not_verify(self):
        self.send()
        EmailCheck.objects.filter(user=self.user).update(expiresAt=timezone.now() - timedelta(seconds=1))
        self.assertContains(self.client.post(self.url, {"code": "123456"}), "wrong or has expired")
        self.assertIsNone(EmailCheck.objects.get(user=self.user).verifiedAt)

    def test_five_wrong_attempts_require(self):
        self.send()
        for index in range(5):
            response = self.client.post(self.url, {"code": "654321"})
        self.assertContains(response, "Too many wrong tries")
        self.assertContains(self.client.post(self.url, {"code": "123456"}), "Too many wrong tries")
        self.assertEqual(EmailCheck.objects.get(user=self.user).attempts, 5)
        self.wait()
        self.send(234567)
        self.assertEqual(EmailCheck.objects.get(user=self.user).attempts, 0)
        self.assertRedirects(self.client.post(self.url, {"code": "234567"}), reverse("home"))

    def test_only_newest_code_works(self):
        self.send()
        self.wait()
        self.send(234567)
        self.assertContains(self.client.post(self.url, {"code": "123456"}), "wrong or has expired")
        self.assertRedirects(self.client.post(self.url, {"code": "234567"}), reverse("home"))

    def test_resend_cooldown_three_per(self):
        self.send()
        self.send()
        self.assertEqual(len(mail.outbox), 1)
        for index in range(2):
            self.wait()
            self.send()
        self.wait()
        self.client.logout()
        self.client.force_login(self.user)
        response = self.send()
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertContains(self.client.get(self.url), "asked for too many codes")
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(EmailCheck.objects.get(user=self.user).sentCount, 3)

    def test_ip_limit_applies_across(self):
        for index in range(11):
            user = User.objects.create_user(f"limit{index}@example.com", f"limit{index}@example.com")
            self.client.force_login(user)
            self.send()
        self.assertEqual(len(mail.outbox), 10)

    def test_hourly_limit_expires(self):
        for index in range(3):
            self.send()
            self.wait()
        cache.delete(f"email-check:user:{self.user.pk}")
        self.send()
        self.assertEqual(len(mail.outbox), 4)

    @skip("Unverified users must complete email verification before browsing")
    def test_unverified_user_sign_in(self):
        self.client.logout()
        self.assertRedirects(
            self.client.post(reverse("signin"), {"email": self.user.email, "password": "Zxcv-9876!"}),
            reverse("home"),
        )
        for name in ("home", "listings", "account_settings", "saved"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_actions_redirect_missing(self):
        for name in ("my_offers", "seller_viewings", "sell", "checkout", "stripe_return"):
            self.assertRedirects(self.client.get(reverse(name)), self.url)
        for action in ("offer", "book", "slot"):
            self.assertRedirects(
                self.client.post(reverse("activity_action"), {"action": action, "id": "1"}),
                self.url,
            )
        self.assertRedirects(self.client.post(reverse("submit_documents", args=[1]), {}), self.url)
        self.assertRedirects(self.client.post(reverse("check_payment", args=[1]), {}), self.url)
        flat = makeListing(self.user)
        self.assertRedirects(self.client.post(reverse("edit_listing", args=[flat.publicId]), {}), self.url)
        self.assertFalse(self.user.offers.exists())

    def test_anonymous_missing_waiting(self):
        self.client.logout()
        self.assertRedirects(self.client.get(self.url), reverse("signin"))

    def test_demo_code_requires_debug(self):
        for debug, host, visible in (
            (True, "", True),
            (False, "", False),
            (True, "smtp.example.com", False),
        ):
            with self.subTest(debug=debug, host=host), self.settings(DEBUG=debug, EMAIL_HOST=host):
                if EmailCheck.objects.filter(user=self.user).exists():
                    self.wait()
                self.send()
                response = self.client.get(self.url)
                if visible:
                    nonce = self.client.session["emailCheckDemo"]["nonce"]
                    code = demoCode(self.user.pk, nonce)
                    self.assertContains(response, "Local development code: " + code)
                    self.assertEqual(
                        EmailCheck.objects.get(user=self.user).codeHash,
                        hashCode(self.user.pk, "email:" + code),
                    )
                    self.assertEqual(set(self.client.session["emailCheckDemo"]), {"userId", "nonce", "code"})
                    with self.settings(DEBUG=False):
                        self.assertNotContains(self.client.get(self.url), "Demo mode")
                    with self.settings(EMAIL_HOST="smtp.example.com"):
                        self.assertNotContains(self.client.get(self.url), "Demo mode")
                else:
                    self.assertNotContains(response, "Demo mode")

    def test_change_keeps_account_verified(self):
        self.send()
        EmailCheck.objects.filter(user=self.user).update(verifiedAt=timezone.now())
        self.wait()
        with (
            self.captureOnCommitCallbacks(execute=True),
            patch("accounts.verification.secrets.randbelow", return_value=234567),
        ):
            self.client.post(
                reverse("account_settings"),
                {
                    "action": "email",
                    "email-email": "changed@example.com",
                    "email-currentPassword": "Zxcv-9876!",
                },
            )
        check = EmailCheck.objects.get(user=self.user)
        self.assertIsNotNone(check.verifiedAt)
        self.assertEqual(check.pendingEmail, "changed@example.com")
        self.assertEqual(mail.outbox[-1].to, ["changed@example.com"])
        self.assertContains(self.client.post(self.url, {"code": "123456"}), "wrong or has expired")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertRedirects(
                self.client.post(self.url, {"code": "234567"}),
                reverse("account_settings"),
                fetch_redirect_response=False,
            )
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "changed@example.com")
        self.assertEqual(mail.outbox[-1].to, ["verify@example.com"])

    def test_keeping_old_cancels_change(self):
        self.send()
        EmailCheck.objects.filter(user=self.user).update(verifiedAt=timezone.now())
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("account_settings"),
                {
                    "action": "email",
                    "email-email": "changed@example.com",
                    "email-currentPassword": "Zxcv-9876!",
                },
            )
        self.assertContains(self.client.get(self.url), "Keep my old email")
        response = self.client.post(self.url, {"cancel": "1"}, follow=True)
        self.assertContains(response, "Your email stays verify@example.com.")
        check = EmailCheck.objects.get(user=self.user)
        self.assertEqual((check.pendingEmail, check.codeHash), ("", ""))
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "verify@example.com")


@override_settings(DEBUG=False, EMAIL_HOST="", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class VerificationDeliveryTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("delivery@example.com", "delivery@example.com", "Zxcv-9876!")
        Profile.objects.create(user=self.user, role="buyer")
        self.client.force_login(self.user)
        self.url = reverse("email_check")

    def send(self, code=123456):
        with patch("accounts.verification.secrets.randbelow", return_value=code):
            return self.client.post(self.url, {"resend": "1"})

    def test_email_not_hold_database(self):
        from accounts.verification import sendCode

        def deliver(*args):
            self.assertFalse(connection.in_atomic_block)
            return sendCode(*args)

        with patch("accounts.verification.sendCode", side_effect=deliver):
            self.send()
        self.assertEqual(len(mail.outbox), 1)

    def test_email_change_not_hold(self):
        from accounts.verification import sendCode

        EmailCheck.objects.create(user=self.user, verifiedAt=timezone.now())

        def deliver(*args):
            self.assertFalse(connection.in_atomic_block)
            return sendCode(*args)

        with patch("accounts.verification.sendCode", side_effect=deliver):
            response = self.client.post(
                reverse("account_settings"),
                {
                    "action": "email",
                    "email-currentPassword": "Zxcv-9876!",
                    "email-email": "new-delivery@example.com",
                },
            )
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertEqual(mail.outbox[0].to, ["new-delivery@example.com"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "delivery@example.com")

    def test_rolled_back_request_not(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.send()
                self.assertFalse(mail.outbox)
                raise RuntimeError("Cancel this request")
        self.assertFalse(mail.outbox)
        self.assertFalse(EmailCheck.objects.filter(user=self.user).exists())

    def test_failed_resend_keeps_working(self):
        self.send()
        self.client.get(self.url)
        EmailCheck.objects.filter(user=self.user).update(
            sentAt=timezone.now() - timedelta(seconds=61),
            attempts=2,
        )
        old = EmailCheck.objects.get(user=self.user)
        with (
            patch("accounts.verification.sendCode", side_effect=RuntimeError("mail down")),
            self.assertLogs("accounts.views", level="ERROR"),
        ):
            self.send(234567)
        check = EmailCheck.objects.get(user=self.user)
        for name in ("codeHash", "expiresAt", "attempts", "sentCount", "sentAt"):
            self.assertEqual(getattr(check, name), getattr(old, name))
        response = self.client.get(self.url)
        self.assertContains(response, "couldn’t send your code")
        self.assertNotContains(response, "We sent a new code")
        self.client.post(self.url, {"code": "123456"})
        check.refresh_from_db()
        self.assertIsNotNone(check.verifiedAt)

    def test_failed_not_overwrite_newer(self):
        self.send()
        EmailCheck.objects.filter(user=self.user).update(
            sentAt=timezone.now() - timedelta(seconds=61),
        )
        new_hash = hashCode(self.user.pk, "email:345678")

        def fail(*args):
            EmailCheck.objects.filter(user=self.user).update(
                codeHash=new_hash,
                sentAt=timezone.now(),
            )
            raise RuntimeError("Earlier delivery failed")

        with (
            patch("accounts.verification.sendCode", side_effect=fail),
            self.assertLogs("accounts.views", level="ERROR"),
        ):
            self.send(234567)
        self.assertEqual(EmailCheck.objects.get(user=self.user).codeHash, new_hash)


class EmailCheckMigrationTests(TransactionTestCase):

    def test_existing_users_including_staff(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        previous = [("accounts", "0001_start")]
        executor.migrate(previous)
        try:
            apps = executor.loader.project_state(previous).apps
            users = apps.get_model("auth", "User")
            ids = [
                users.objects.create(username="existing", email="existing@example.com").pk,
                users.objects.create(username="staff", is_staff=True).pk,
                users.objects.create(username="inactive", is_active=False).pk,
            ]
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            checks = executor.loader.project_state(latest).apps.get_model("accounts", "EmailCheck")
            self.assertEqual(checks.objects.filter(user_id__in=ids, verifiedAt__isnull=False).count(), 3)
            self.assertFalse(checks.objects.exclude(codeHash="").exists())
        finally:
            MigrationExecutor(connection).migrate(latest)


@override_settings(
    TURNSTILE_ENABLED=False,
    EMAIL_HOST="",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class WaitingSigninTests(TransactionTestCase):

    def setUp(self):
        cache.clear()
        self.user = makeUser("waiting@test.com", "seller", password="Qwer-5678!")
        EmailCheck.objects.filter(user=self.user).update(verifiedAt=None)

    def test_unverified_account_not_signed(self):
        response = self.client.post(
            reverse("signin"), {"email": "waiting@test.com", "password": "Qwer-5678!"}
        )
        self.assertRedirects(response, reverse("email_check"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)
        code = re.search("\\b(\\d{6})\\b", mail.outbox[0].body).group(1)
        response = self.client.post(reverse("email_check"), {"code": code})
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))
        self.assertNotIn("emailCheckUser", self.client.session)

    def test_another_visitor_blocks_code(self):
        self.client.post(reverse("signin"), {"email": "waiting@test.com", "password": "Qwer-5678!"})
        other = self.client_class()
        self.assertRedirects(
            other.get(reverse("email_check")), reverse("signin"), fetch_redirect_response=False
        )
