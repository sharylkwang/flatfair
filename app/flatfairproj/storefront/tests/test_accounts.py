import re
from unittest.mock import patch
from urllib.parse import urlencode

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.db.models.query import QuerySet
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from accounts.models import EmailCheck
from accounts.tokens import cache as accountCache
from accounts.verification import setEmailOk
from .helpers import PASSWORD, makeListing, makeStaff, makeUser
from storefront.forms import RegisterForm
from storefront.models import Profile, lock_user


class RegisterTests(TransactionTestCase):

    def setUp(self):
        cache.clear()
        accountCache.clear()

    def data(self, **changes):
        data = {
            "fullName": "Tan Wei Ling",
            "email": "Asdf@Test.com",
            "password": "Qwer-5678!zx",
            "confirmPassword": "Qwer-5678!zx",
            "role": "buyer",
        }
        data.update(changes)
        return data

    def test_registration_cleans_email_signs(self):
        response = self.client.post(reverse("register"), self.data())
        self.assertRedirects(response, reverse("email_check"), fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)
        user = User.objects.get()
        self.assertEqual(user.username, "asdf@test.com")
        self.assertEqual(user.profile.role, "buyer")
        self.assertEqual((user.profile.canBuy, user.profile.canSell), (True, False))
        self.assertNotIn("_auth_user_id", self.client.session)
        page = self.client.get(reverse("email_check"))
        self.assertContains(page, "You’ll be signed in once the code is checked.")
        self.assertEqual(self.client.get(reverse("account_settings")).status_code, 302)
        code = re.search("\\b(\\d{6})\\b", mail.outbox[0].body).group(1)
        self.client.post(reverse("email_check"), {"code": "000000" if code != "000000" else "111111"})
        self.assertNotIn("_auth_user_id", self.client.session)
        response = self.client.post(reverse("email_check"), {"code": code})
        self.assertRedirects(response, reverse("account_settings"), fetch_redirect_response=False)
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))

    def test_registration_remembers_page(self):
        self.client.post(reverse("register") + "?next=/saved", self.data())
        self.assertEqual(self.client.session["emailCheckReturn"], "/saved")
        self.client.logout()
        self.client.post(
            reverse("register") + "?next=/saved", self.data(email="zxcv@test.com", role="seller")
        )
        self.assertEqual(self.client.session["emailCheckReturn"], "/saved")

    def test_registration_missing_return(self):
        self.client.post(reverse("register"), self.data())
        self.assertEqual(self.client.session["emailCheckReturn"], reverse("account_settings"))
        self.client.logout()
        self.client.post(
            reverse("register") + "?next=https://evil.example/",
            self.data(email="zxcv@test.com", role="seller"),
        )
        self.assertEqual(self.client.session["emailCheckReturn"], reverse("dashboard"))

    def test_registration_blocks_existing(self):
        makeUser("asdf@test.com")
        response = self.client.post(reverse("register"), self.data())
        self.assertContains(response, "This email already has an account.")
        self.assertEqual(User.objects.count(), 1)

    def test_registration_blocks_mismatched(self):
        response = self.client.post(reverse("register"), self.data(confirmPassword="Qwer-5678!zy"))
        self.assertIn("confirmPassword", response.context["form"].errors)
        response = self.client.post(
            reverse("register"), self.data(password="12345678", confirmPassword="12345678")
        )
        self.assertIn("password", response.context["form"].errors)
        response = self.client.post(
            reverse("register"), self.data(password="qwerasdf12", confirmPassword="qwerasdf12")
        )
        self.assertIn("upper case letter", response.context["form"].errors["password"][0])
        self.assertFalse(User.objects.exists())

    def test_seller_role_prefilled_saved(self):
        response = self.client.get(reverse("register"), {"role": "seller"})
        self.assertEqual(response.context["form"].initial["role"], "seller")
        self.assertContains(response, "You can also buy with this account.")
        self.client.post(reverse("register") + "?next=/saved", self.data(role="seller"))
        self.assertEqual(Profile.objects.get().role, "seller")
        self.assertEqual((Profile.objects.get().canBuy, Profile.objects.get().canSell), (True, True))

    def test_seller_signup_lands_in(self):
        with patch("accounts.verification.secrets.randbelow", return_value=123456):
            response = self.client.post(reverse("register"), self.data(role="seller"), follow=True)
        self.assertEqual(response.request["PATH_INFO"], reverse("email_check"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertRedirects(
            self.client.post(reverse("email_check"), {"code": "123456"}), reverse("dashboard")
        )
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["currentMode"], "seller")
        self.assertContains(response, "Buying or selling")

    def test_registration_handles_concurrent(self):
        validate = RegisterForm.is_valid

        def registerOther(form):
            valid = validate(form)
            makeUser("asdf@test.com")
            return valid

        with patch.object(RegisterForm, "is_valid", registerOther):
            response = self.client.post(reverse("register"), self.data())
        self.assertContains(response, "This email can’t be used for a new account.")
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(Profile.objects.count(), 1)
        self.assertNotIn("_auth_user_id", self.client.session)


class SigninTests(TestCase):

    def setUp(self):
        cache.clear()
        self.buyer = makeUser("asdf@test.com", password=PASSWORD)
        self.seller = makeUser("qwer@test.com", "seller", password=PASSWORD)
        self.admin = makeStaff("zxcv@test.com", password=PASSWORD)
        setEmailOk(self.admin)

    def signin(self, email, password=PASSWORD, next=""):
        url = reverse("signin")
        if next:
            url += "?next=" + next
        return self.client.post(url, {"email": email, "password": password})

    def assertCheckedRedirect(self, response, target):
        self.assertRedirects(
            response,
            reverse("human_check") + "?" + urlencode({"next": target, "force": "1"}),
            fetch_redirect_response=False,
        )
        self.assertRedirects(self.client.get(response.url), target)

    def test_case_insensitive_keeps_role(self):
        self.assertCheckedRedirect(self.signin("ASDF@test.com"), reverse("home"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.buyer.pk))
        self.assertEqual(self.client.get(reverse("saved")).status_code, 200)
        self.client.logout()
        self.assertCheckedRedirect(self.signin("qwer@test.com"), reverse("dashboard"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.seller.pk))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        self.client.logout()
        self.assertCheckedRedirect(self.signin("zxcv@test.com"), reverse("admin_overview"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.admin.pk))
        self.assertEqual(self.client.get(reverse("admin_overview")).status_code, 200)

    def test_allows_local_next_blocks(self):
        response = self.signin("asdf@test.com", next="/saved")
        self.assertCheckedRedirect(response, reverse("saved"))
        self.client.logout()
        response = self.signin("asdf@test.com", next="https://evil.example/")
        self.assertCheckedRedirect(response, reverse("home"))

    def test_failed_trigger_attempt_limit(self):
        response = self.signin("asdf@test.com", "wrong")
        self.assertContains(response, "That email or password isn’t right.")
        for i in range(5):
            self.signin("asdf@test.com", "wrong")
        response = self.signin("asdf@test.com")
        self.assertContains(response, "Too many failed attempts.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_signout_requires_post(self):
        self.client.force_login(self.buyer)
        self.client.get(reverse("signout"))
        self.assertIn("_auth_user_id", self.client.session)
        self.client.post(reverse("signout"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_explains_offer_save(self):
        response = self.client.get(reverse("signin") + "?next=/my-offers")
        self.assertEqual(response.context["nextNote"], "Sign in to see your offers and viewings.")
        response = self.client.get(reverse("signin") + "?next=/listing/abc?save=1")
        self.assertEqual(response.context["nextNote"], "Sign in to save this flat.")

    def test_database_attempt_limit_survives(self):
        with patch.object(cache, "get", return_value=0):
            for i in range(5):
                self.signin("asdf@test.com", "wrong")
        response = self.signin("asdf@test.com")
        self.assertContains(response, "Too many failed attempts.")
        self.assertNotIn("_auth_user_id", self.client.session)


class RoleTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.buyer = makeUser("asdf@test.com")
        cls.seller = makeUser("qwer@test.com", "seller")
        cls.admin = makeStaff("zxcv@test.com")

    def test_protected_pages_redirect(self):
        for name in [
            "saved",
            "my_offers",
            "dashboard",
            "sell",
            "cart",
            "admin_overview",
            "notifications",
        ]:
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.url.startswith(reverse("signin")))

    def test_buyer_blocks_open_seller(self):
        self.client.force_login(self.buyer)
        for name in ["dashboard", "sell", "my_listings", "seller_viewings", "cart", "my_orders"]:
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 403)
            self.assertTemplateUsed(response, "sellerpanel/buyer_account.html")
        self.assertEqual(self.client.get(reverse("admin_overview")).status_code, 403)

    def test_seller_open_buyer_pages(self):
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get(reverse("dashboard")).context["currentMode"], "seller")
        self.assertRedirects(self.client.post(reverse("switch_mode"), {"mode": "buyer"}), reverse("listings"))
        for name in ["saved", "my_offers"]:
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["currentMode"], "buyer")
        self.assertRedirects(
            self.client.post(reverse("switch_mode"), {"mode": "seller"}), reverse("dashboard")
        )
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_buyer_start_selling_missing(self):
        self.client.force_login(self.buyer)
        response = self.client.post(reverse("new_seller_account"))
        self.assertRedirects(response, reverse("sell"))
        self.assertIn("_auth_user_id", self.client.session)
        self.buyer.profile.refresh_from_db()
        self.assertTrue(self.buyer.profile.canBuy)
        self.assertTrue(self.buyer.profile.canSell)

    def test_staff_open_pages_but(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("saved")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin_overview")).status_code, 200)
        self.assertRedirects(self.client.get(reverse("signin")), reverse("admin_overview"))

    def test_review_listing_visible_owner(self):
        flat = makeListing(self.seller, status="review")
        url = reverse("listing_detail", args=[flat.publicId])
        self.assertRedirects(self.client.get(url), reverse("home"))
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(self.admin)
        self.assertTrue(self.client.get(url).context["staffPreview"])


@override_settings(GOOGLE_CLIENT_ID="asdf", GOOGLE_CLIENT_SECRET="qwer")
class GoogleTests(TestCase):

    def start(self, role="buyer", next=""):
        response = self.client.post(reverse("google_start") + next, {"role": role})
        return response["Location"].split("state=")[1]

    def finish(self, state, email="zxcv@test.com", verified=True):
        token = {"access_token": "zxcv"}
        info = {"email": email, "email_verified": verified, "name": "Tan Wei Ling"}
        with (
            patch("accounts.google.requests.post") as post,
            patch("accounts.google.requests.get") as get,
        ):
            post.return_value.json.return_value = token
            get.return_value.json.return_value = info
            response = self.client.get(reverse("google_callback"), {"state": state, "code": "asdf"})
        return response

    def assertStraightRedirect(self, response, target):
        self.assertRedirects(response, target, fetch_redirect_response=False)

    def test_buttons_endpoints_require(self):
        with self.settings(GOOGLE_CLIENT_ID=""):
            self.assertNotContains(self.client.get(reverse("register")), "Continue with Google")
            self.assertNotContains(self.client.get(reverse("signin")), "Continue with Google")
            self.assertEqual(self.client.post(reverse("google_start")).status_code, 404)
            self.assertEqual(self.client.get(reverse("google_callback")).status_code, 404)
        self.assertContains(self.client.get(reverse("register")), "Continue with Google")

    def test_callback_blocks_mismatched(self):
        self.start()
        response = self.finish("wrong")
        self.assertRedirects(response, reverse("signin"))
        self.assertFalse(User.objects.exists())

    def test_signup_creates_seller_missing(self):
        response = self.finish(self.start("seller"), email="ZXCV@Test.com")
        self.assertStraightRedirect(response, reverse("dashboard"))
        user = User.objects.get()
        self.assertEqual(
            (user.username, user.first_name, user.profile.role),
            ("zxcv@test.com", "Tan Wei Ling", "seller"),
        )
        self.assertIsNotNone(EmailCheck.objects.get(user=user).verifiedAt)
        self.assertFalse(user.has_usable_password())
        self.assertEqual((user.profile.canBuy, user.profile.canSell), (True, True))
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))
        page = self.client.get(reverse("dashboard"))
        self.assertEqual(page.context["currentMode"], "seller")
        self.assertContains(page, "Buying or selling")

    def test_buyer_signup_remains_buy(self):
        response = self.finish(self.start())
        self.assertStraightRedirect(response, reverse("home"))
        profile = Profile.objects.get()
        self.assertEqual((profile.role, profile.canBuy, profile.canSell), ("buyer", True, False))

    @override_settings(TURNSTILE_ENABLED=False, TURNSTILE_SITE_KEY="", TURNSTILE_SECRET_KEY="")
    def test_new_signup_needs_no(self):
        response = self.finish(self.start())
        self.assertStraightRedirect(response, reverse("home"))
        self.assertEqual(len(mail.outbox), 0)
        user = User.objects.get()
        self.assertIsNotNone(EmailCheck.objects.get(user=user).verifiedAt)
        self.assertNotIn("pendingSignupEmailCheck", self.client.session)

    def test_signin_keeps_both_capabilities(self):
        user = makeUser("zxcv@test.com")
        EmailCheck.objects.filter(user=user).update(verifiedAt=None)
        user.profile.canSell = True
        user.profile.save(update_fields=["canSell"])
        response = self.finish(self.start("seller"))
        self.assertStraightRedirect(response, reverse("home"))
        user.profile.refresh_from_db()
        self.assertEqual((user.profile.canBuy, user.profile.canSell), (True, True))
        self.assertIsNotNone(EmailCheck.objects.get(user=user).verifiedAt)
        self.assertEqual(User.objects.count(), 1)

    def test_signin_reuses_account_local(self):
        user = makeUser("zxcv@test.com", password=PASSWORD)
        response = self.finish(self.start(next="?next=/saved"))
        self.assertStraightRedirect(response, "/saved")
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))
        self.assertEqual(User.objects.count(), 1)

    def test_signin_blocks_verify_different(self):
        user = makeUser("zxcv@test.com")
        User.objects.filter(pk=user.pk).update(email="someoneelse@test.com")
        EmailCheck.objects.filter(user=user).update(verifiedAt=None)
        response = self.finish(self.start())
        self.assertRedirects(response, reverse("signin"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertIsNone(EmailCheck.objects.get(user=user).verifiedAt)

    def test_signin_blocks_ambiguous_case(self):
        makeUser("zxcv@test.com")
        makeUser("ZXCV@test.com")
        response = self.finish(self.start())
        self.assertRedirects(response, reverse("signin"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_signin_rechecks_email_locking(self):
        user = makeUser("zxcv@test.com")
        EmailCheck.objects.filter(user=user).update(verifiedAt=None)

        def changedEmail(pk):
            User.objects.filter(pk=pk).update(email="changed@test.com", username="changed@test.com")
            return lock_user(pk)

        with patch("accounts.google.lock_user", side_effect=changedEmail):
            response = self.finish(self.start())
        self.assertRedirects(response, reverse("signin"))
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertIsNone(EmailCheck.objects.get(user=user).verifiedAt)

    def test_signin_blocks_staff_account(self):
        makeStaff("zxcv@test.com", password=PASSWORD)
        response = self.finish(self.start())
        self.assertRedirects(response, reverse("signin"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_signin_blocks_unverified_email(self):
        response = self.finish(self.start(), verified=False)
        self.assertRedirects(response, reverse("signin"))
        self.assertFalse(User.objects.exists())

    @override_settings(PUBLIC_URL="", ALLOWED_HOSTS=["192.168.0.253", "127.0.0.1"])
    def test_signin_hidden_blocked_on(self):
        lan = {"HTTP_HOST": "192.168.0.253:8765"}
        self.assertNotContains(self.client.get(reverse("signin"), **lan), "Continue with Google")
        self.assertEqual(self.client.post(reverse("google_start"), **lan).status_code, 404)
        local = {"HTTP_HOST": "127.0.0.1:8765"}
        self.assertContains(self.client.get(reverse("signin"), **local), "Continue with Google")

    def test_callback_blocks_unicode_state(self):
        self.start()
        response = self.finish("状态")
        self.assertRedirects(response, reverse("signin"))
        self.assertFalse(User.objects.exists())

    def test_callback_blocks_invalid_token(self):
        for token in [None, [], {"access_token": None}, {"access_token": []}]:
            with self.subTest(token=token):
                state = self.start()
                with (
                    patch("accounts.google.requests.post") as post,
                    patch("accounts.google.requests.get") as get,
                ):
                    post.return_value.json.return_value = token
                    response = self.client.get(reverse("google_callback"), {"state": state, "code": "asdf"})
                self.assertRedirects(response, reverse("signin"))
                get.assert_not_called()
                self.assertFalse(User.objects.exists())

    def test_callback_blocks_invalid_profile(self):
        for info in [
            None,
            [],
            {"email": "invalid", "email_verified": True},
            {"email": "a" * 140 + "@example.com", "email_verified": True},
        ]:
            with self.subTest(info=info):
                state = self.start()
                with patch("accounts.google.googleInfo", return_value=info):
                    response = self.client.get(reverse("google_callback"), {"state": state, "code": "asdf"})
                self.assertRedirects(response, reverse("signin"))
                self.assertFalse(User.objects.exists())

    def test_callback_email_name_not(self):
        state = self.start()
        with patch(
            "accounts.google.googleInfo",
            return_value={"email": "zxcv@test.com", "email_verified": True, "name": 123},
        ):
            response = self.client.get(reverse("google_callback"), {"state": state, "code": "asdf"})
        self.assertStraightRedirect(response, reverse("home"))
        self.assertEqual(User.objects.get().first_name, "zxcv")

    def test_signup_handles_concurrent(self):
        user = makeUser("zxcv@test.com")
        state = self.start()
        first = QuerySet.first
        calls = 0

        def concurrentUser(query):
            nonlocal calls
            if query.model is User:
                calls += 1
                if calls == 1:
                    return None
            return first(query)

        with patch.object(QuerySet, "first", concurrentUser):
            response = self.finish(state)
        self.assertStraightRedirect(response, reverse("home"))
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(Profile.objects.count(), 1)
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))

    def test_signin_blocks_inactive_account(self):
        user = makeUser("zxcv@test.com")
        user.is_active = False
        user.save(update_fields=["is_active"])
        response = self.finish(self.start())
        self.assertRedirects(response, reverse("signin"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_password_submit_precedes_form(self):
        for name, button in (
            ("signin", "Sign in</button>"),
            ("register", "Create account</button>"),
        ):
            page = self.client.get(reverse(name)).content.decode()
            self.assertLess(page.index(button), page.index("formaction="))


HOST = "flatfair.example.com"
TAB = {"HTTP_HOST": HOST, "HTTP_SEC_FETCH_DEST": "document", "HTTP_SEC_FETCH_MODE": "navigate"}
FRAME = {"HTTP_HOST": HOST, "HTTP_SEC_FETCH_DEST": "iframe", "HTTP_SEC_FETCH_MODE": "navigate"}


@override_settings(SINGLE_ADDRESS=True, SINGLE_ADDRESS_HOST=HOST, ALLOWED_HOSTS=[HOST, "testserver"])
class SingleAddressTests(TestCase):

    def test_page_opened_in_tab(self):
        response = self.client.get("/listings?q=tampines", **TAB)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-start="/listings?q=tampines"')
        self.assertContains(response, "storefront/shell.js")
        self.assertNotContains(response, "Flats for sale")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertIn("Sec-Fetch-Dest", response["Vary"])

    def test_same_inside_frame_real(self):
        response = self.client.get("/listings", **FRAME)
        self.assertContains(response, "Flats for sale")
        self.assertNotContains(response, "storefront/shell.js")
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("Sec-Fetch-Dest", response["Vary"])

    def test_forms_sent_tab_never(self):
        response = self.client.post(
            reverse("signin"), {"username": "nobody@test.com", "password": "x"}, **TAB
        )
        self.assertNotContains(response, "storefront/shell.js")

    def test_other_hosts_older_browsers(self):
        self.assertContains(
            self.client.get("/listings", HTTP_SEC_FETCH_DEST="document", HTTP_SEC_FETCH_MODE="navigate"),
            "Flats for sale",
        )
        self.assertContains(self.client.get("/listings", HTTP_HOST=HOST), "Flats for sale")

    @override_settings(SINGLE_ADDRESS=False)
    def test_it_can_be_switched_off(self):
        self.assertContains(self.client.get("/listings", **TAB), "Flats for sale")

    def test_google_stripe_leave_frame(self):
        with override_settings(GOOGLE_CLIENT_ID="id", GOOGLE_CLIENT_SECRET="secret"):
            page = self.client.get(reverse("signin")).content.decode()
        if "Continue with Google" in page:
            self.assertIn('formtarget="_top"', page)


class RealAddressTests(TestCase):

    def test_visitors_through_tunnel_told(self):
        from flatfair.middleware import RealClientAddressMiddleware

        request = type("Request", (), {})()
        request.META = {"REMOTE_ADDR": "127.0.0.1", "HTTP_CF_CONNECTING_IP": "203.0.113.7"}
        RealClientAddressMiddleware(lambda r: r)(request)
        self.assertEqual(request.META["REMOTE_ADDR"], "203.0.113.7")

    def test_header_ignored_anywhere_else(self):
        from flatfair.middleware import RealClientAddressMiddleware

        for meta, expected in (
            (
                {"REMOTE_ADDR": "198.51.100.4", "HTTP_CF_CONNECTING_IP": "203.0.113.7"},
                "198.51.100.4",
            ),
            ({"REMOTE_ADDR": "127.0.0.1", "HTTP_CF_CONNECTING_IP": "not an address"}, "127.0.0.1"),
            ({"REMOTE_ADDR": "127.0.0.1"}, "127.0.0.1"),
        ):
            request = type("Request", (), {})()
            request.META = dict(meta)
            RealClientAddressMiddleware(lambda r: r)(request)
            self.assertEqual(request.META["REMOTE_ADDR"], expected)

    def test_two_tunnel_visitors_get(self):
        from storefront import lens

        first = type(
            "Request",
            (),
            {
                "META": {"REMOTE_ADDR": "203.0.113.7"},
                "session": {},
                "user": type("U", (), {"is_authenticated": False})(),
            },
        )()
        second = type(
            "Request",
            (),
            {
                "META": {"REMOTE_ADDR": "203.0.113.8"},
                "session": {},
                "user": type("U", (), {"is_authenticated": False})(),
            },
        )()
        for _ in range(lens.SEARCHES_PER_HOUR):
            first.session = {}
            self.assertFalse(lens.searchFlood(first))
        first.session = {}
        self.assertTrue(lens.searchFlood(first))
        self.assertFalse(lens.searchFlood(second))
