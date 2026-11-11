import base64
from html import unescape
import json
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
import warnings

from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail, signing
from django.core.cache import caches
from django.test import Client, TestCase, override_settings
from django.urls import reverse
import jwt

from accounts.google import relaySite
from accounts.models import EmailCheck
from accounts.tokens import issueToken, readToken, tokenKey
from accounts.verification import setEmailOk
from storefront.tests.helpers import makeUser


@override_settings(
    GOOGLE_CLIENT_ID="asdf",
    GOOGLE_CLIENT_SECRET="qwer",
    PUBLIC_URL="https://flatfair.example.com",
    ALLOWED_HOSTS=["flatfair.example.com", "192.168.0.253", "127.0.0.1", "[::1]"],
)
class GoogleRelayTests(TestCase):
    public = {"HTTP_HOST": "flatfair.example.com", "secure": True}
    lan = {"HTTP_HOST": "192.168.0.253:8765"}
    origin = "http://192.168.0.253:8765"

    def setUp(self):
        caches["resources"].clear()

    def start(self, origin=None, nextUrl="/saved", **host):
        response = self.client.get(
            reverse("google_start"),
            {"return": self.origin if origin is None else origin, "next": nextUrl},
            **host or self.public,
        )
        return parse_qs(urlsplit(response.url).query)["state"][0]

    def finish(self, state):
        with (
            patch("accounts.google.requests.post") as post,
            patch("accounts.google.requests.get") as get,
        ):
            post.return_value.json.return_value = {"access_token": "test-token"}
            get.return_value.json.return_value = {
                "email": "relay@example.com",
                "email_verified": True,
                "name": "Relay User",
            }
            response = self.client.get(
                reverse("google_callback"), {"state": state, "code": "test-code"}, **self.public
            )
        self.assertEqual(
            post.call_args.kwargs["data"]["redirect_uri"],
            "https://flatfair.example.com/accounts/google/callback",
        )
        return response

    def token(self, nextUrl="/saved", emailPending=False):
        if not emailPending:
            user, _ = User.objects.get_or_create(
                username="relay@example.com", defaults={"email": "relay@example.com"}
            )
            setEmailOk(user)
        response = self.finish(self.start(nextUrl=nextUrl))
        return parse_qs(urlsplit(response.url).query)["token"][0]

    def claims(self, token):
        saved = readToken("google-relay", token)
        return {name: saved[name] for name in ("userId", "jti", "next", "emailPending")}

    def rejected(self, token, client=None):
        client = client or Client()
        response = client.get(reverse("google_relay"), {"token": token}, **self.lan)
        self.assertRedirects(response, reverse("signin"), fetch_redirect_response=False)
        page = client.get(response.url, **self.lan)
        self.assertContains(page, "That Google sign-in link has expired. Please try again.")
        self.assertNotIn("_auth_user_id", client.session)

    def test_lan_button_links_public(self):
        for name in ("signin", "register"):
            response = self.client.get(reverse(name), {"next": "/saved?sort=new"}, **self.lan)
            self.assertContains(response, "Continue with Google")
            page = unescape(response.content.decode())
            url = response.context["googleStartUrl"]
            self.assertIn('href="' + url + '"', page)
            parsed = urlsplit(url)
            self.assertEqual(parsed.scheme + "://" + parsed.netloc, "https://flatfair.example.com")
            self.assertEqual(parsed.path, reverse("google_start"))
            self.assertEqual(
                parse_qs(parsed.query),
                {"return": [self.origin], "next": ["/saved?sort=new"], "mode": [name]},
            )
        response = self.client.get(reverse("signin"), {"next": "https://evil.example/"}, **self.lan)
        self.assertNotIn("next", parse_qs(urlsplit(response.context["googleStartUrl"]).query))
        with self.settings(GOOGLE_CLIENT_ID=""):
            self.assertNotContains(self.client.get(reverse("signin"), **self.lan), "Continue with Google")

    @override_settings(PUBLIC_URL="")
    def test_lan_missing_public_url(self):
        self.assertNotContains(self.client.get(reverse("signin"), **self.lan), "Continue with Google")
        self.assertEqual(self.client.get(reverse("google_start"), **self.lan).status_code, 404)

    def test_domain_loopback_buttons_keep(self):
        for host in (self.public, {"HTTP_HOST": "127.0.0.1:8765"}, {"HTTP_HOST": "[::1]"}):
            response = self.client.get(reverse("signin"), **host)
            url = response.context["googleStartUrl"]
            self.assertContains(response, 'formaction="' + url + '"')
            self.assertEqual(urlsplit(url).path, reverse("google_start"))
            self.assertEqual(parse_qs(urlsplit(url).query), {"mode": ["signin"]})
            self.assertFalse(response.context["googleRelay"])

    def test_start_allows_private_loopback(self):
        for origin in (self.origin, "https://127.0.0.1:8765", "http://[::1]:8765"):
            with self.subTest(origin=origin):
                self.start(origin)
                self.assertEqual(self.client.session["googleSignin"]["return"], origin)
        for origin in (
            "http://8.8.8.8:8765",
            "http://192.168.0.254:8765",
            "ftp://192.168.0.253:8765",
            "//192.168.0.253:8765",
            "http://flatfair.example.com",
            "http://user:password@192.168.0.253:8765",
            "http://192.168.0.253:8765/path",
            "http://192.168.0.253:8765?next=/evil",
            "http://192.168.0.253:8765#fragment",
            "http://192.168.0.253:99999",
            "http://192.168.0.253:8765\\evil",
            "http://192.168.0.253:\n8765",
            "http://[broken",
            "",
        ):
            with self.subTest(origin=origin):
                self.start(origin)
                self.assertNotIn("return", self.client.session["googleSignin"])
        with self.settings(ALLOWED_HOSTS=["flatfair.example.com", "8.8.8.8"]):
            self.start("http://8.8.8.8:8765")
            self.assertNotIn("return", self.client.session["googleSignin"])

    def test_start_ignores_return_not(self):
        for host in ({"HTTP_HOST": "127.0.0.1:8765"}, {"HTTP_HOST": "flatfair.example.com"}):
            self.start(**host)
            self.assertNotIn("return", self.client.session["googleSignin"])

    @override_settings(ALLOWED_HOSTS=["*"], DEBUG=True)
    def test_wildcard_allows_private_origins(self):
        self.assertEqual(relaySite("http://10.0.0.5:8765"), "http://10.0.0.5:8765")
        self.assertEqual(relaySite("http://8.8.8.8"), "")
        with self.settings(DEBUG=False):
            self.assertEqual(relaySite("http://10.0.0.5:8765"), "")

    def test_callback_issues_signed_token(self):
        response = self.finish(self.start())
        parsed = urlsplit(response.url)
        self.assertEqual(parsed.scheme + "://" + parsed.netloc, self.origin)
        self.assertEqual(parsed.path, reverse("google_relay"))
        token = parse_qs(parsed.query)["token"][0]
        saved = readToken("google-relay", token)
        user = User.objects.get()
        self.assertEqual(saved["userId"], user.pk)
        self.assertEqual(saved["next"], "/saved")
        self.assertEqual(saved["exp"] - saved["iat"], 60)
        self.assertTrue(caches["resources"].get("google-relay:" + saved["jti"]))
        self.assertFalse(saved["emailPending"])
        self.assertIsNotNone(EmailCheck.objects.get(user=user).verifiedAt)
        self.assertEqual(len(mail.outbox), 0)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertNotIn("googleSignin", self.client.session)
        self.assertIn("no-store", response["Cache-Control"])

    def test_logs_in_once_rotates(self):
        token = self.token()
        browser = Client()
        session = browser.session
        session["marker"] = "kept"
        session.save()
        oldKey = session.session_key
        response = browser.get(reverse("google_relay"), {"token": token}, **self.lan)
        self.assertRedirects(response, "/saved", fetch_redirect_response=False)
        self.assertEqual(browser.session["_auth_user_id"], str(User.objects.get().pk))
        self.assertEqual(browser.session["_auth_user_backend"], "django.contrib.auth.backends.ModelBackend")
        self.assertNotEqual(browser.session.session_key, oldKey)
        self.assertEqual(browser.session["marker"], "kept")
        self.assertIn("no-store", response["Cache-Control"])
        self.rejected(token)

    @override_settings(TURNSTILE_ENABLED=False, TURNSTILE_SITE_KEY="", TURNSTILE_SECRET_KEY="")
    def test_new_signs_in_missing(self):
        token = self.token(emailPending=True)
        self.assertEqual(len(mail.outbox), 0)
        browser = Client()
        response = browser.get(reverse("google_relay"), {"token": token}, **self.lan)
        self.assertRedirects(response, "/saved", fetch_redirect_response=False)
        user = User.objects.get()
        self.assertIsNotNone(EmailCheck.objects.get(user=user).verifiedAt)
        self.assertEqual(browser.session["_auth_user_id"], str(user.pk))
        self.rejected(token)

    def test_blocks_expired_tampered_missing(self):
        token = self.token()
        saved = self.claims(token)
        with patch("accounts.tokens.time.time", return_value=time.time() - 61):
            expired = issueToken("google-relay", saved, 60)
        self.rejected(expired)
        self.rejected(token + "tampered")
        self.rejected("")
        caches["resources"].delete("google-relay:" + saved["jti"])
        self.rejected(token)

    def test_blocks_inactive_staff_deleted(self):
        for field in ("is_active", "is_staff", "deleted"):
            with self.subTest(field=field):
                token = self.token()
                user = User.objects.get()
                if field == "deleted":
                    user.delete()
                else:
                    setattr(user, field, field == "is_staff")
                    user.save(update_fields=[field])
                self.rejected(token)
                if field != "deleted":
                    setattr(user, field, field == "is_active")
                    user.save(update_fields=[field])
                self.rejected(token)

    def test_redirects_safe_local_next(self):
        token = self.token()
        saved = self.claims(token)
        for nextUrl in (
            "https://evil.example/",
            "//evil.example/",
            "/\\evil.example/",
            "javascript:alert(1)",
            "https://192.168.0.253:8765/saved",
            "",
            "/saved?sort=new",
        ):
            with self.subTest(nextUrl=nextUrl):
                saved["next"] = nextUrl
                caches["resources"].set("google-relay:" + saved["jti"], True, timeout=60)
                token = issueToken("google-relay", saved, 60)
                response = Client().get(reverse("google_relay"), {"token": token}, **self.lan)
                target = nextUrl if nextUrl == "/saved?sort=new" else reverse("home")
                self.assertRedirects(response, target, fetch_redirect_response=False)

    def test_relay_requires_get(self):
        token = self.token()
        self.assertEqual(
            self.client.post(reverse("google_relay"), {"token": token}, **self.lan).status_code, 405
        )
        response = Client().get(reverse("google_relay") + "?" + urlencode({"token": token}), **self.lan)
        self.assertRedirects(response, "/saved", fetch_redirect_response=False)


@override_settings(
    GOOGLE_CLIENT_ID="asdf",
    GOOGLE_CLIENT_SECRET="qwer",
    PUBLIC_URL="https://flatfair.example.com",
    ALLOWED_HOSTS=["flatfair.example.com"],
    TURNSTILE_ENABLED=True,
    TURNSTILE_SITE_KEY="1x00000000000000000000AA",
    TURNSTILE_SECRET_KEY="1x0000000000000000000000000000000AA",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class GoogleNoSecondCheckTests(TestCase):
    host = {"HTTP_HOST": "flatfair.example.com", "secure": True}

    def setUp(self):
        caches["resources"].clear()

    def googleSignIn(self, email, nextUrl="/saved"):
        response = self.client.get(reverse("google_start"), {"next": nextUrl}, **self.host)
        state = parse_qs(urlsplit(response.url).query)["state"][0]
        with (
            patch("accounts.google.requests.post") as post,
            patch("accounts.google.requests.get") as get,
        ):
            post.return_value.json.return_value = {"access_token": "t"}
            get.return_value.json.return_value = {
                "email": email,
                "email_verified": True,
                "name": "Wei Ling",
            }
            return self.client.get(reverse("google_callback"), {"state": state, "code": "c"}, **self.host)

    def test_returning_user_goes_straight(self):
        user = User.objects.create_user("weiling@example.com", "weiling@example.com", first_name="Wei Ling")
        setEmailOk(user)
        response = self.googleSignIn("weiling@example.com", nextUrl="/packages")
        self.assertRedirects(response, "/packages", fetch_redirect_response=False)
        page = self.client.get("/packages", **self.host)
        self.assertNotContains(page, "data-human-check")
        self.assertContains(page, "Welcome back, Wei Ling.")

    def test_new_user_signed_in(self):
        response = self.googleSignIn("new@example.com", nextUrl="/packages")
        self.assertRedirects(response, "/packages", fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 0)
        page = self.client.get("/packages", **self.host)
        self.assertNotContains(page, "data-human-check")
        self.assertContains(page, "Welcome to FlatFair, Wei Ling.")


def part(data):
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def signed(claims, key, algorithm="HS256"):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return jwt.encode(claims, key, algorithm=algorithm)


def payloadOf(token):
    text = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))


class TokenTests(TestCase):

    def claims(self, **changes):
        now = int(time.time())
        claims = {
            "iss": "flatfair",
            "aud": "asdf",
            "iat": now,
            "exp": now + 60,
            "jti": "qwer",
            "userId": 7,
        }
        claims.update(changes)
        return {name: value for name, value in claims.items() if value is not None}

    def test_reads_back_standard_claims(self):
        before = int(time.time())
        token = issueToken("asdf", {"userId": 7}, 60)
        saved = readToken("asdf", token)
        for name, value in {"userId": 7, "iss": "flatfair", "aud": "asdf"}.items():
            with self.subTest(name=name):
                self.assertEqual(saved[name], value)
        self.assertEqual(saved["exp"] - saved["iat"], 60)
        self.assertGreaterEqual(saved["iat"], before)
        self.assertEqual(len(saved["jti"]), 32)
        self.assertEqual(jwt.get_unverified_header(token), {"alg": "HS256", "typ": "JWT"})
        self.assertNotEqual(saved["jti"], readToken("asdf", issueToken("asdf", {"userId": 7}, 60))["jti"])

    def test_expired_token_is_refused(self):
        with patch("accounts.tokens.time.time", return_value=time.time() - 61):
            token = issueToken("asdf", {"userId": 7}, 60)
        self.assertIsNone(readToken("asdf", token))
        with patch("accounts.tokens.time.time", return_value=time.time() - 30):
            token = issueToken("asdf", {"userId": 7}, 60)
        self.assertEqual(readToken("asdf", token)["userId"], 7)

    def test_wrong_purpose_is_refused(self):
        token = issueToken("asdf", {"userId": 7}, 60)
        for purpose in ("qwer", "ASDF", "asdf ", "", None):
            with self.subTest(purpose=purpose):
                self.assertIsNone(readToken(purpose, token))

    def test_tampered_payload_is_refused(self):
        token = issueToken("asdf", {"userId": 7}, 60)
        header, payload, signature = token.split(".")
        changed = payloadOf(token)
        changed["userId"] = 8
        for bad in [
            [header, part(changed), signature],
            [header, payload, signature[:-2] + "AA"],
            [header, payload, ""],
        ]:
            with self.subTest(token=bad):
                self.assertIsNone(readToken("asdf", ".".join(bad)))
        self.assertEqual(readToken("asdf", token)["userId"], 7)

    def test_alg_none_is_refused(self):
        token = issueToken("asdf", {"userId": 7}, 60)
        payload = token.split(".")[1]
        for alg in ("none", "None", "NONE"):
            with self.subTest(alg=alg):
                header = part({"alg": alg, "typ": "JWT"})
                self.assertIsNone(readToken("asdf", header + "." + payload + "."))
                self.assertIsNone(readToken("asdf", header + "." + payload + "." + token.split(".")[2]))
        self.assertIsNone(readToken("asdf", signed(self.claims(), None, "none")))

    def test_other_keys_algorithms_refused(self):
        self.assertEqual(readToken("asdf", signed(self.claims(), tokenKey()))["userId"], 7)
        for key, kind in [
            (tokenKey(), "HS512"),
            (settings.SECRET_KEY, "HS256"),
            ("a" * 32, "HS256"),
        ]:
            with self.subTest(kind=kind):
                self.assertIsNone(readToken("asdf", signed(self.claims(), key, kind)))
        token = issueToken("asdf", {"userId": 7}, 60)
        with override_settings(SECRET_KEY="another secret key for this test only"):
            self.assertIsNone(readToken("asdf", token))

    def test_missing_wrong_standard_claims(self):
        for changes in (
            {"exp": None},
            {"iat": None},
            {"jti": None},
            {"iss": None},
            {"aud": None},
            {"iss": "someone else"},
            {"aud": ["qwer"]},
            {"exp": "soon"},
            {"iat": int(time.time()) + 600, "exp": int(time.time()) + 900},
        ):
            with self.subTest(changes=changes):
                token = signed(self.claims(**changes), tokenKey())
                self.assertIsNone(readToken("asdf", token))

    def test_caller_blocks_replace_standard(self):
        token = issueToken(
            "asdf",
            {"aud": "qwer", "iss": "someone else", "exp": 9999999999, "iat": 1, "jti": "zxcv"},
            60,
        )
        saved = readToken("asdf", token)
        self.assertEqual(saved["exp"] - saved["iat"], 60)
        self.assertEqual(saved["jti"], "zxcv")
        self.assertIsNone(readToken("qwer", token))

    def test_values_not_refused(self):
        for value in (
            None,
            "",
            "asdf",
            "a.b.c",
            7,
            b"asdf",
            signing.dumps({"userId": 7}, salt="asdf"),
        ):
            with self.subTest(value=value):
                self.assertIsNone(readToken("asdf", value))


@override_settings(
    GOOGLE_CLIENT_ID="asdf",
    GOOGLE_CLIENT_SECRET="qwer",
    PUBLIC_URL="https://flatfair.example.com",
    ALLOWED_HOSTS=["flatfair.example.com", "192.168.0.253"],
)
class RelayTokenTests(TestCase):
    public = {"HTTP_HOST": "flatfair.example.com", "secure": True}
    lan = {"HTTP_HOST": "192.168.0.253:8765"}

    def setUp(self):
        caches["resources"].clear()
        self.user = makeUser("relay@example.com")

    def relayToken(self):
        response = self.client.get(
            reverse("google_start"),
            {"return": "http://192.168.0.253:8765", "next": "/saved"},
            **self.public,
        )
        state = parse_qs(urlsplit(response.url).query)["state"][0]
        with (
            patch("accounts.google.requests.post") as post,
            patch("accounts.google.requests.get") as get,
        ):
            post.return_value.json.return_value = {"access_token": "test-token"}
            get.return_value.json.return_value = {
                "email": self.user.email,
                "email_verified": True,
                "name": "Asdf",
            }
            response = self.client.get(
                reverse("google_callback"), {"state": state, "code": "test-code"}, **self.public
            )
        self.assertTrue(response.url.startswith("http://192.168.0.253:8765" + reverse("google_relay")))
        return parse_qs(urlsplit(response.url).query)["token"][0]

    def made(self, purpose="google-relay", seconds=60, **changes):
        claims = {"userId": self.user.pk, "next": "/saved", "emailPending": False}
        claims.update(changes)
        token = issueToken(purpose, claims, seconds)
        caches["resources"].set("google-relay:" + payloadOf(token)["jti"], True, timeout=60)
        return token

    def refused(self, token):
        browser = Client()
        response = browser.get(reverse("google_relay"), {"token": token}, **self.lan)
        self.assertRedirects(response, reverse("signin"), fetch_redirect_response=False)
        self.assertContains(
            browser.get(response.url, **self.lan),
            "That Google sign-in link has expired. Please try again.",
        )
        self.assertNotIn("_auth_user_id", browser.session)

    def accepted(self, token):
        browser = Client()
        response = browser.get(reverse("google_relay"), {"token": token}, **self.lan)
        self.assertRedirects(response, "/saved", fetch_redirect_response=False)
        self.assertEqual(browser.session["_auth_user_id"], str(self.user.pk))

    def test_sixty_second_jwt_purpose(self):
        token = self.relayToken()
        self.assertEqual(jwt.get_unverified_header(token)["alg"], "HS256")
        saved = readToken("google-relay", token)
        for name, value in {
            "userId": self.user.pk,
            "aud": "google-relay",
            "iss": "flatfair",
        }.items():
            with self.subTest(name=name):
                self.assertEqual(saved[name], value)
        self.assertEqual(saved["exp"] - saved["iat"], 60)
        self.assertTrue(caches["resources"].get("google-relay:" + saved["jti"]))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_signs_in_end_reused(self):
        token = self.relayToken()
        self.accepted(token)
        self.assertIsNone(caches["resources"].get("google-relay:" + payloadOf(token)["jti"]))
        self.refused(token)

    def test_expired_refused(self):
        with patch("accounts.tokens.time.time", return_value=time.time() - 61):
            token = self.made()
        self.refused(token)
        self.accepted(self.made())

    def test_another_purpose_refused(self):
        self.refused(self.made(purpose="download"))

    def test_tampered_unsigned_refused(self):
        other = makeUser("other@example.com")
        token = self.made()
        header, payload, signature = token.split(".")
        changed = payloadOf(token)
        changed["userId"] = other.pk
        self.refused(".".join([header, part(changed), signature]))
        self.refused(part({"alg": "none", "typ": "JWT"}) + "." + part(changed) + ".")
        self.refused(signed(changed, settings.SECRET_KEY))
        self.accepted(token)

    def test_old_format_token_is_refused(self):
        nonce = "a" * 32
        caches["resources"].set("google-relay:" + nonce, True, timeout=60)
        old = signing.dumps(
            {"userId": self.user.pk, "nonce": nonce, "next": "/saved", "emailPending": False},
            salt="google-relay",
        )
        self.refused(old)

    def test_payload_checks_kept(self):
        for changes in (
            {"userId": str(self.user.pk)},
            {"next": None},
            {"jti": "short"},
            {"jti": 7},
            {"userId": self.user.pk + 1000},
        ):
            with self.subTest(changes=changes):
                token = issueToken("google-relay", {"userId": self.user.pk, "next": "/saved", **changes}, 60)
                caches["resources"].set("google-relay:" + str(payloadOf(token)["jti"]), True, timeout=60)
                self.refused(token)

    def test_missing_waiting_nonce_refused(self):
        token = issueToken("google-relay", {"userId": self.user.pk, "next": "/saved"}, 60)
        self.refused(token)
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.refused(self.made())
