import re
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
import requests

from accounts import turnstile
from accounts.models import EmailCheck
from accounts.tokens import cache as accountCache
from accounts.views import hashCode

KEYS = {
    "TURNSTILE_ENABLED": True,
    "TURNSTILE_SITE_KEY": "1x00000000000000000000AA",
    "TURNSTILE_SECRET_KEY": "1x0000000000000000000000000000000AA",
}
OLD = "Zxcv-9876!"
NEW = "Qwer-5678!"


def siteverify(success):
    return patch.object(
        turnstile.requests, "post", return_value=Mock(json=Mock(return_value={"success": success}))
    )


def registerData():
    return {
        "fullName": "Tan Wei Ling",
        "email": "asdf@test.com",
        "password": NEW,
        "confirmPassword": NEW,
        "role": "buyer",
        "cf-turnstile-response": "XXXX.DUMMY.TOKEN.XXXX",
    }


@override_settings(
    TURNSTILE_ENABLED=False,
    TURNSTILE_SITE_KEY="",
    TURNSTILE_SECRET_KEY="",
    EMAIL_HOST="",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class OffTests(TransactionTestCase):

    def setUp(self):
        cache.clear()
        accountCache.clear()

    def test_disabled_turnstile_skips(self):
        for page in ["register", "forgot_password"]:
            with self.subTest(page=page):
                self.assertNotContains(self.client.get(reverse(page)), "cf-turnstile")
        with siteverify(False) as verify:
            response = self.client.post(reverse("register"), registerData())
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("human_check"), response.url)
        verify.assert_not_called()

    def test_disabled_check_redirects(self):
        self.assertEqual(self.client.get("/packages").status_code, 200)
        self.assertRedirects(
            self.client.get("/check?next=/packages"), "/packages", fetch_redirect_response=False
        )

    def test_signup_missing_turnstile_keys(self):
        with siteverify(False) as verify:
            response = self.client.post(reverse("register"), registerData(), follow=True)
        verify.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request["PATH_INFO"], reverse("email_check"))
        self.assertEqual(len(mail.outbox), 1)
        user = User.objects.get()
        code = re.search("\\b(\\d{6})\\b", mail.outbox[0].body).group(1)
        check = EmailCheck.objects.get(user=user)
        self.assertIsNone(check.verifiedAt)
        self.assertEqual(check.codeHash, hashCode(user.pk, "email:" + code))
        self.assertNotIn("pendingSignupEmailCheck", self.client.session)
        self.client.get("/check?next=/check-email&force=1")
        self.assertEqual(len(mail.outbox), 1)
        self.assertRedirects(
            self.client.post(reverse("email_check"), {"code": code}), reverse("account_settings")
        )
        check.refresh_from_db()
        self.assertIsNotNone(check.verifiedAt)

    def test_failed_initial_delivery_retry(self):
        secret = "private-email-password@example.invalid"
        with patch("accounts.verification.sendCode", side_effect=RuntimeError(secret)):
            with self.assertLogs("accounts.views", level="ERROR") as logs:
                response = self.client.post(reverse("register"), registerData(), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sorry, we couldn’t send your code just now.")
        self.assertNotIn(secret, " ".join(logs.output))
        self.assertNotIn("Traceback", " ".join(logs.output))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(EmailCheck.objects.get().codeHash, "")
        self.client.post(reverse("email_check"), {"resend": "1"})
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(TURNSTILE_GATE=True)
    def test_gate_flag_no_effect(self):
        self.assertEqual(self.client.get("/packages").status_code, 200)


@override_settings(**KEYS)
class FormTests(TransactionTestCase):

    def setUp(self):
        cache.clear()
        accountCache.clear()

    def test_widget_loads_on_protected(self):
        response = self.client.get(reverse("register"))
        self.assertContains(response, 'data-sitekey="1x00000000000000000000AA"')
        self.assertContains(response, "data-human-check")
        self.assertContains(self.client.get(reverse("forgot_password")), "cf-turnstile")
        self.assertNotContains(self.client.get("/packages"), "data-human-check")

    def test_checks_wait_for_the_button(self):
        for page in ["forgot_password", "signin", "register"]:
            with self.subTest(page=page):
                self.assertContains(self.client.get(reverse(page)), "data-on-submit")
        self.assertNotContains(self.client.get(reverse("human_check") + "?force=1"), "data-on-submit")

    def test_failed_check_blocks(self):
        with siteverify(False):
            response = self.client.post(reverse("register"), registerData())
        self.assertContains(response, "Please try the security check again.")
        self.assertFalse(User.objects.exists())

    def test_successful_check_posts_token(self):
        with siteverify(True) as verify:
            response = self.client.post(reverse("register"), registerData())
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("human_check"), response.url)
        self.assertEqual(verify.call_args.args[0], turnstile.VERIFY_URL)
        self.assertEqual(verify.call_args.kwargs["timeout"], 5)
        self.assertEqual(verify.call_args.kwargs["data"]["response"], "XXXX.DUMMY.TOKEN.XXXX")

    def test_signup_sends_code_straight(self):
        with siteverify(True):
            response = self.client.post(reverse("register"), registerData())
        self.assertRedirects(response, reverse("email_check"), fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertNotIn("pendingSignupEmailCheck", self.client.session)

    def test_one_check_covers_later(self):
        with siteverify(True):
            self.client.post(reverse("register"), registerData())
        for target in ("/packages", "/account"):
            response = self.client.get(reverse("human_check"), {"next": target, "force": "1"})
            self.assertRedirects(response, target, fetch_redirect_response=False)

    def test_no_code_sent_until(self):
        with siteverify(False):
            self.client.post(reverse("register"), registerData())
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(User.objects.exists())
        with siteverify(True):
            self.client.post(reverse("register"), registerData())
        self.assertEqual(len(mail.outbox), 1)

    def test_waiting_account_must_finish(self):
        with siteverify(True):
            self.client.post(reverse("register"), registerData())
        session = self.client.session
        session["emailCheckUser"]["at"] -= 3601
        session.save()
        self.assertRedirects(
            self.client.get(reverse("email_check")),
            reverse("signin"),
            fetch_redirect_response=False,
        )

    def test_password_reset_email_requires(self):
        User.objects.create_user("asdf@test.com", "asdf@test.com", OLD)
        data = {"email": "asdf@test.com", "cf-turnstile-response": "bad"}
        with siteverify(False):
            response = self.client.post(reverse("forgot_password"), data)
        self.assertContains(response, "Please try the security check again.")
        self.assertEqual(len(mail.outbox), 0)
        with siteverify(True):
            response = self.client.post(reverse("forgot_password"), data)
        self.assertRedirects(response, reverse("forgot_code"))
        self.assertEqual(len(mail.outbox), 1)

    def test_password_change_not_checked(self):
        user = User.objects.create_user("asdf@test.com", "asdf@test.com", OLD)
        with siteverify(True):
            self.client.post(
                reverse("forgot_password"),
                {"email": "asdf@test.com", "cf-turnstile-response": "ok"},
            )
        code = re.search("\\b(\\d{6})\\b", mail.outbox[0].body).group(1)
        self.client.post(reverse("forgot_code"), {"code": code})
        self.assertNotContains(self.client.get(reverse("forgot_new")), "data-human-check")
        with siteverify(False) as verify:
            response = self.client.post(reverse("forgot_new"), {"password": NEW, "confirm": NEW})
        self.assertRedirects(response, reverse("signin"))
        verify.assert_not_called()
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW))

    def test_invalid_tokens_verification(self):
        for token in ["", "x" * 3000]:
            with self.subTest(token=token):
                self.assertFalse(turnstile.tokenOk(token, "127.0.0.1"))
        with patch.object(turnstile.requests, "post", side_effect=requests.ConnectionError):
            self.assertFalse(turnstile.tokenOk("ok", "127.0.0.1"))
        with patch.object(turnstile.requests, "post", return_value=Mock(json=Mock(side_effect=ValueError))):
            self.assertFalse(turnstile.tokenOk("ok", "127.0.0.1"))


@override_settings(TURNSTILE_GATE=True, **KEYS)
class GateTests(TestCase):

    def test_page_keeps_requested_path(self):
        response = self.client.get("/listings?town=BEDOK")
        self.assertRedirects(
            response, "/check?next=%2Flistings%3Ftown%3DBEDOK", fetch_redirect_response=False
        )
        check = self.client.get(response["Location"])
        self.assertContains(check, 'data-sitekey="1x00000000000000000000AA"')
        self.assertContains(check, 'value="/listings?town=BEDOK"')

    def test_successful_sets_httponly_cookie(self):
        with siteverify(True):
            response = self.client.post("/check", {"cf-turnstile-response": "ok", "next": "/packages"})
        self.assertRedirects(response, "/packages", fetch_redirect_response=False)
        self.assertTrue(response.cookies[turnstile.PASS_COOKIE]["httponly"])
        self.assertEqual(self.client.get("/packages").status_code, 200)

    def test_failed_not_set_cookie(self):
        with siteverify(False):
            response = self.client.post("/check", {"cf-turnstile-response": "bad", "next": "/packages"})
        self.assertContains(response, "Please try the security check again.", status_code=403)
        self.assertNotIn(turnstile.PASS_COOKIE, response.cookies)

    def test_assets_apis_post_requests(self):
        for url in [
            "/static/storefront/site.css",
            "/media/x.jpg",
            "/api/addresses?q=bedok",
            "/favicon.ico",
        ]:
            response = self.client.get(url)
            self.assertNotIn("/check", response.get("Location", ""))
        response = self.client.post("/signin", {"email": "qwer@test.com", "password": "asdf"})
        self.assertNotIn("/check", response.get("Location", ""))

    def test_unsigned_cookie_external(self):
        self.client.cookies[turnstile.PASS_COOKIE] = "1"
        self.assertEqual(self.client.get("/packages").status_code, 302)
        with siteverify(True):
            response = self.client.post(
                "/check", {"cf-turnstile-response": "ok", "next": "https://evil.example/"}
            )
        self.assertRedirects(response, "/", fetch_redirect_response=False)
