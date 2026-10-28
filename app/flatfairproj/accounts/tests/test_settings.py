from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
from unittest import skip
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.conf import settings
from django.core import mail
from django.core.cache.backends.db import DatabaseCache
from django.db.backends.sqlite3.base import DatabaseWrapper
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import EmailCheck, PasswordHistory, ResetCode
from accounts.settings import AccountEmailForm, DELETION_MESSAGE, PASSWORD_LOCKED, triesKey
from accounts.tokens import cache
from adminpanel.models import Ticket
from storefront.models import Profile

OLD = "Zxcv-9876!"
NEW = "Qwer-5678!"


class AccountDatabaseLockTests(SimpleTestCase):
    def test_email_password_check_waits(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "account.sqlite3"
            config = deepcopy(settings.DATABASES["default"])
            config["NAME"] = str(path)
            config["OPTIONS"]["timeout"] = 2
            reader = DatabaseWrapper(config, alias="account-lock-check")
            ready, starting, release = Event(), Event(), Event()

            def write_in_background():
                database = sqlite3.connect(path, timeout=2)
                try:
                    database.execute("BEGIN IMMEDIATE")
                    database.execute("UPDATE account_users SET stamp = stamp + 1")
                    ready.set()
                    if not starting.wait(2):
                        raise AssertionError("The account request did not start")
                    release.wait(0.25)
                    database.commit()
                finally:
                    database.close()

            try:
                with reader.cursor() as cursor:
                    cursor.execute("CREATE TABLE account_users (id integer, stamp integer)")
                    cursor.execute("INSERT INTO account_users VALUES (1, 0)")
                    cursor.execute(
                        "CREATE TABLE account_cache "
                        "(cache_key varchar(255) PRIMARY KEY, value text, expires datetime)"
                    )

                user = User(pk=1, email="seller@example.com", password=make_password(OLD))
                form = AccountEmailForm(user)
                form.cleaned_data = {"currentPassword": OLD}
                counters = DatabaseCache("account_cache", {})

                with ThreadPoolExecutor(max_workers=1) as pool:
                    writer = pool.submit(write_in_background)
                    try:
                        self.assertTrue(ready.wait(2))
                        starting.set()
                        reader.set_autocommit(False, force_begin_transaction_with_broken_autocommit=True)
                        with reader.cursor() as cursor:
                            cursor.execute("SELECT id FROM account_users")
                            self.assertEqual(cursor.fetchone()[0], user.pk)
                        with (
                            patch("accounts.settings.cache", counters),
                            patch("django.core.cache.backends.db.connections", {"default": reader}),
                        ):
                            self.assertEqual(form.clean_currentPassword(), OLD)
                        reader.commit()
                    finally:
                        reader.rollback()
                        release.set()
                        writer.result(timeout=5)
            finally:
                reader.close()


class AccountSettingsTests(TestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            "weiling@example.com", "weiling@example.com", OLD, first_name="Tan Wei Ling"
        )
        Profile.objects.create(user=self.user, role="buyer")
        self.client.force_login(self.user)
        self.url = reverse("account_settings")

    def postForm(self, action, **data):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                self.url,
                {"action": action, **{f"{action}-{name}": value for name, value in data.items()}},
            )

    def setGoogleOnly(self):
        self.user.set_unusable_password()
        self.user.save(update_fields=["password"])
        self.client.force_login(self.user)

    def resetCode(self):
        return ResetCode.objects.create(
            user=self.user, codeHash="unused", expiresAt=timezone.now() + timedelta(minutes=10)
        )

    def test_anonymous_visitors_must_sign(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse("signin") + "?next=/account")

    def test_buyers_sellers_open_header(self):
        self.assertEqual(self.url, "/account")
        for role in ("buyer", "seller"):
            Profile.objects.filter(user=self.user).update(role=role)
            response = self.client.get(self.url)
            for check_text_1 in (
                'href="/account"',
                "Account settings",
                'name="name-fullName"',
                'name="email-currentPassword"',
                'name="password-currentPassword"',
                'name="delete-currentPassword"',
            ):
                self.assertContains(response, check_text_1)
            self.assertIn("no-store", response.headers["Cache-Control"])

    def test_staff_blocks_read_mutate(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.assertEqual(self.client.get(self.url).status_code, 403)
        response = self.postForm("name", fullName="Changed")
        self.assertEqual(response.status_code, 403)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Tan Wei Ling")
        response = self.client.get(reverse("home"))
        self.assertNotContains(response, 'href="/account"')

    def test_users_missing_customer_profile(self):
        self.user.profile.delete()
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_edit_full_name_keeps(self):
        response = self.postForm("name", fullName="  Lim Mei Ling  ")
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Lim Mei Ling")
        self.assertEqual(self.user.email, "weiling@example.com")
        self.assertEqual(self.user.profile.role, "buyer")
        self.assertTrue(self.user.check_password(OLD))
        self.assertContains(self.client.get(self.url), "Your full name was updated.")

    def test_blank_overlong_names_show(self):
        for fullName in ("  ", "x" * 61):
            response = self.postForm("name", fullName=fullName)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["nameForm"].errors)
            self.assertContains(response, 'aria-describedby="id_name-fullName_error"')
            self.assertContains(response, "Please check the account form and try again.")
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Tan Wei Ling")

    def test_invalid_summary_links_error(self):
        response = self.postForm("password", currentPassword=OLD, password="", confirmPassword="")
        self.assertContains(response, 'id="account-feedback" role="alert" tabindex="-1"')
        self.assertContains(response, f'action="{self.url}#account-feedback"', count=4)
        for field in response.context["passwordForm"]:
            if field.errors:
                self.assertContains(response, f'href="#{field.id_for_label}"')
                self.assertContains(response, f'id="{field.id_for_label}"')
        self.assertNotContains(self.client.get(self.url), 'id="account-feedback"')

    @skip("Google-only email changes no longer require setting a password")
    def test_google_errors_link_visible(self):
        self.setGoogleOnly()
        response = self.postForm("email", email="new@example.com", currentPassword="")
        self.assertContains(response, 'href="#account-password"')
        self.assertContains(response, 'id="account-password" tabindex="-1"')
        self.assertNotContains(response, 'href="#id_email-currentPassword"')

    def test_email_change_waits_code(self):
        code = self.resetCode()
        with patch("accounts.verification.secrets.randbelow", return_value=234567):
            response = self.postForm("email", email="  ＮＥＷ@Example.COM  ", currentPassword=OLD)
        self.assertRedirects(response, reverse("email_check"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "weiling@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["new@example.com"])
        self.assertNotIn(OLD, mail.outbox[0].body)
        page = self.client.get(reverse("email_check"))
        self.assertContains(page, "Confirm your new email")
        self.assertContains(page, "Your email stays weiling@example.com until you enter it.")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("email_check"), {"code": "234567"})
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertEqual((self.user.email, self.user.username), ("new@example.com", "new@example.com"))
        self.assertTrue(self.user.check_password(OLD))
        self.assertEqual(mail.outbox[1].to, ["weiling@example.com"])
        self.assertEqual(mail.outbox[1].subject, "Your FlatFair email was changed")
        self.assertIn("new@example.com", mail.outbox[1].body)
        self.assertTrue(mail.outbox[1].alternatives)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        code.refresh_from_db()
        self.assertIsNotNone(code.usedAt)

    def test_email_change_requires_current(self):
        for password in ("", "wrong"):
            response = self.postForm("email", email="new@example.com", currentPassword=password)
            self.assertIn("currentPassword", response.context["emailForm"].errors)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "weiling@example.com")
        self.assertFalse(mail.outbox)

    def test_email_change_blocks_invalid(self):
        for email in (
            "bad",
            self.user.email,
            "a" * 64 + "@" + "b" * 63 + ".example.computer.example",
        ):
            response = self.postForm("email", email=email, currentPassword=OLD)
            self.assertIn("email", response.context["emailForm"].errors)
        self.assertFalse(mail.outbox)

    def test_email_change_blocks_case(self):
        User.objects.create_user("other@example.com", "TAKEN@example.com", OLD)
        User.objects.create_user("USERNAME@example.com", "different@example.com", OLD)
        for email in ("taken@EXAMPLE.com", "username@example.COM"):
            response = self.postForm("email", email=email, currentPassword=OLD)
            self.assertContains(response, "That email is already registered.")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "weiling@example.com")
        self.assertFalse(mail.outbox)

    def test_email_collision_during_save(self):
        User.objects.create_user("taken@example.com", "taken@example.com", OLD)
        with patch("accounts.settings.emailTaken", return_value=False):
            response = self.postForm("email", email="taken@example.com", currentPassword=OLD)
        self.assertContains(response, "That email is already registered.")
        self.assertContains(response, 'aria-describedby="id_email-email_error"')
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "weiling@example.com")
        self.assertFalse(mail.outbox)

    def test_email_delivery_failure_not(self):
        with patch("django.core.mail.EmailMultiAlternatives.send", side_effect=RuntimeError(OLD)):
            with self.assertLogs("accounts.views", level="ERROR") as logs:
                response = self.postForm("email", email="new@example.com", currentPassword=OLD)
        self.assertRedirects(response, reverse("email_check"), fetch_redirect_response=False)
        output = " ".join(logs.output)
        for secret in (OLD, "weiling@example.com", "new@example.com"):
            self.assertNotIn(secret, output)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "weiling@example.com")
        self.assertNotContains(self.client.get(self.url), "we sent there")

    def test_google_change_email_missing(self):
        self.setGoogleOnly()
        response = self.client.get(self.url)
        self.assertContains(response, 'name="email-email"')
        response = self.postForm("email", email="new@example.com", currentPassword="")
        self.assertRedirects(response, reverse("email_check"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "weiling@example.com")
        self.assertEqual(EmailCheck.objects.get(user=self.user).pendingEmail, "new@example.com")
        self.assertContains(
            self.client.get(self.url),
            "You asked to change your email to <strong>new@example.com</strong>.",
        )

    def test_password_change_records_history(self):
        code = self.resetCode()
        otherClient = Client()
        otherClient.force_login(self.user)
        response = self.postForm("password", currentPassword=OLD, password=NEW, confirmPassword=NEW)
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW))
        self.assertEqual(PasswordHistory.objects.get().user, self.user)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Your FlatFair password was changed")
        self.assertEqual(mail.outbox[0].to, [self.user.email])
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        self.assertContains(self.client.get(self.url), "You are still signed in.")
        self.assertRedirects(otherClient.get(self.url), reverse("signin") + "?next=/account")
        code.refresh_from_db()
        self.assertIsNotNone(code.usedAt)

    def test_password_change_requires(self):
        for currentPassword, confirmPassword, field in (
            ("", NEW, "currentPassword"),
            ("wrong", NEW, "currentPassword"),
            (OLD, "different", "confirmPassword"),
        ):
            response = self.postForm(
                "password",
                currentPassword=currentPassword,
                password=NEW,
                confirmPassword=confirmPassword,
            )
            self.assertIn(field, response.context["passwordForm"].errors)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(OLD))
        self.assertFalse(PasswordHistory.objects.exists())
        self.assertFalse(mail.outbox)

    def test_password_change_checks(self):
        for password in ("qwerasdf", "Qwer5678", "weiling@example.com"):
            response = self.postForm(
                "password", currentPassword=OLD, password=password, confirmPassword=password
            )
            self.assertIn("password", response.context["passwordForm"].errors)
        self.assertFalse(mail.outbox)

    def test_current_two_previous_passwords(self):
        previous = ["Asdf-1234!", "Tyui-4567!", "Ghjk-6543!"]
        for password in previous:
            PasswordHistory.objects.create(user=self.user, passwordHash=make_password(password))
        for password in (OLD, previous[1], previous[2]):
            response = self.postForm(
                "password", currentPassword=OLD, password=password, confirmPassword=password
            )
            self.assertContains(response, "last 3 passwords")
        response = self.postForm(
            "password", currentPassword=OLD, password=previous[0], confirmPassword=previous[0]
        )
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(previous[0]))
        self.assertEqual(PasswordHistory.objects.filter(user=self.user).count(), 2)

    def test_google_set_password_missing(self):
        self.setGoogleOnly()
        response = self.client.get(self.url)
        self.assertContains(response, "Set a password")
        self.assertNotContains(response, 'name="password-currentPassword"')
        response = self.postForm("password", password=NEW, confirmPassword=NEW)
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW))
        self.assertFalse(PasswordHistory.objects.exists())
        self.assertEqual(mail.outbox[0].subject, "Your FlatFair password was changed")
        self.assertContains(self.client.get(self.url), 'name="email-email"')

    def test_deletion_request_creates_ticket(self):
        response = self.postForm("delete", currentPassword=OLD)
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        ticket = Ticket.objects.get()
        self.assertEqual(ticket.customer, self.user)
        self.assertEqual(ticket.email, self.user.email)
        self.assertEqual(ticket.topic, "account")
        self.assertEqual(ticket.status, "open")
        self.assertEqual(ticket.message, DELETION_MESSAGE)
        self.assertNotIn(OLD, ticket.message)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertTrue(self.user.check_password(OLD))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        self.assertContains(
            self.client.get(self.url), "Your account deletion request is with our support team."
        )
        self.assertContains(self.client.get(reverse("my_ticket", args=[ticket.pk])), DELETION_MESSAGE)

    def test_repeated_deletion_request(self):
        self.postForm("delete", currentPassword=OLD)
        self.postForm("delete", currentPassword=OLD)
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertContains(self.client.get(self.url), "already with our support team")

    def test_deletion_requires_password(self):
        for password in ("", "wrong"):
            response = self.postForm("delete", currentPassword=password)
            self.assertIn("currentPassword", response.context["deletionForm"].errors)
        self.setGoogleOnly()
        response = self.postForm("delete", currentPassword="")
        self.assertContains(response, "Set a password first")
        self.assertIn("currentPassword", response.context["deletionForm"].errors)
        self.assertFalse(Ticket.objects.exists())

    def test_five_wrong_password_attempts(self):
        for action in ("email", "password", "delete", "email", "delete"):
            response = self.postForm(
                action,
                currentPassword="wrong",
                email="new@example.com",
                password=NEW,
                confirmPassword=NEW,
            )
        self.assertContains(response, PASSWORD_LOCKED)
        for action in ("email", "password", "delete"):
            response = self.postForm(
                action,
                currentPassword=OLD,
                email="new@example.com",
                password=NEW,
                confirmPassword=NEW,
            )
            self.assertContains(response, PASSWORD_LOCKED)
        self.assertEqual(cache.get(triesKey(self.user)), 5)
        self.assertFalse(Ticket.objects.exists())
        self.assertFalse(PasswordHistory.objects.exists())
        self.assertFalse(mail.outbox)
        self.assertRedirects(self.postForm("name", fullName="Lim Mei Ling"), self.url)

    def test_valid_current_password_clears(self):
        self.postForm("delete", currentPassword="wrong")
        self.assertEqual(cache.get(triesKey(self.user)), 1)
        self.postForm("delete", currentPassword=OLD)
        self.assertIsNone(cache.get(triesKey(self.user)))

    def test_password_attempt_lock_expires(self):
        with patch("accounts.settings.cache.add", wraps=cache.add) as add:
            self.postForm("delete", currentPassword="wrong")
        add.assert_called_once_with(triesKey(self.user), 0, 900)
        cache.delete(triesKey(self.user))
        self.assertRedirects(self.postForm("delete", currentPassword=OLD), self.url)

    def test_invalid_action_get_parameters(self):
        response = self.client.get(self.url, {"action": "delete", "delete-currentPassword": OLD})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Ticket.objects.exists())
        for action in ("", "role"):
            response = self.postForm(action, role="seller")
            self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertEqual(self.user.profile.role, "buyer")

    def test_forms_require_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(self.url, {"action": "name", "name-fullName": "Changed"})
        self.assertEqual(response.status_code, 403)
