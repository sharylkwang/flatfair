import hmac
import ipaddress
import secrets
from urllib.parse import urlencode, urlsplit

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.core.cache import caches
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from storefront.models import Profile, lock_user
from .views import homeFor, nextPath
from .forms import NormalizedEmailField
from .tokens import issueToken, readToken, PENDING_KEY
from .verification import RETURN_KEY, setEmailOk, totpEnabled
from .turnstile import markHuman

SESSION_KEY = "googleSignin"
FAILED = "Google sign-in didn’t work. Please try again, or sign in with your email and password."
RELAY_PURPOSE = "google-relay"
relayCache = caches["resources"]


def googleOn():
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


def google_ok(request):
    host = urlsplit("//" + request.get_host()).hostname

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def googleButton(request):
    relay = not google_ok(request) and bool(settings.PUBLIC_URL)
    url = reverse("google_start")
    query = {}
    nextUrl = nextPath(request)

    if nextUrl:
        query["next"] = nextUrl

    if relay:
        url = settings.PUBLIC_URL.rstrip("/") + url
        query["return"] = request.build_absolute_uri("/").rstrip("/")

    if request.resolver_match and request.resolver_match.url_name == "register":
        mode = "register"
    else:
        mode = "signin"

    query["mode"] = mode
    url += ("?" if "?" not in url else "&") + urlencode(query)

    return {
        "googleOn": googleOn() and (google_ok(request) or relay),
        "googleRelay": relay,
        "googleStartUrl": url,
    }


def relaySite(value):
    try:
        origin = urlsplit(value)
        host = origin.hostname
        address = ipaddress.ip_address(host)
        origin.port
    except (ValueError, TypeError):
        return ""

    if (
        origin.scheme not in ("http", "https")
        or origin.username is not None
        or origin.password is not None
        or origin.path not in ("", "/")
        or origin.query
        or origin.fragment
        or "\\" in value
        or any(char.isspace() or ord(char) < 32 for char in value)
        or not (address.is_private or address.is_loopback)
    ):
        return ""

    if not (
        host in settings.ALLOWED_HOSTS
        or f"[{host}]" in settings.ALLOWED_HOSTS
        or (settings.DEBUG and "*" in settings.ALLOWED_HOSTS)
    ):
        return ""

    return f"{origin.scheme}://{origin.netloc}"


def back_url(request):
    return request.build_absolute_uri(reverse("google_callback"))


def refuse(request, text):
    messages.error(request, text)

    return redirect("signin")


def googleGo(request):
    if not googleOn() or not google_ok(request):
        raise Http404

    mode = request.GET.get("mode", "register")
    state = secrets.token_urlsafe(24)
    role = "buyer"

    if request.POST.get("role") == "seller":
        role = "seller"

    if mode not in ("signin", "register"):
        mode = "register"

    request.session[SESSION_KEY] = {
        "state": state,
        "role": role,
        "mode": mode,
        "next": nextPath(request),
    }

    if settings.PUBLIC_URL and request.build_absolute_uri("/").rstrip(
        "/"
    ) == settings.PUBLIC_URL.rstrip("/"):
        origin = relaySite(request.GET.get("return", ""))

        if origin:
            request.session[SESSION_KEY]["return"] = origin

    query = urlencode(
        {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": back_url(request),
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
        }
    )

    return redirect(("https://accounts.google.com/o/oauth2/v2/auth") + "?" + query)


def googleInfo(request, code):
    tokenData = {
        "code": code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": back_url(request),
        "grant_type": "authorization_code",
    }

    tokens = requests.post(("https://oauth2.googleapis.com/token"), data=tokenData, timeout=10)
    tokens.raise_for_status()
    access = tokens.json()["access_token"]

    if not isinstance(access, str) or not access:
        raise ValueError

    info = requests.get(
        ("https://openidconnect.googleapis.com/v1/userinfo"),
        headers={"Authorization": "Bearer " + access},
        timeout=10,
    )

    info.raise_for_status()

    return info.json()


@never_cache
def googleBack(request):
    seconds = 60

    if not googleOn():
        raise Http404

    saved = request.session.pop(SESSION_KEY, None)
    state = request.GET.get("state", "")
    code = request.GET.get("code", "")

    if not saved or not state or not hmac.compare_digest(saved["state"].encode(), state.encode()):
        return refuse(request, FAILED)

    if not code:
        return refuse(request, "Google sign-in was cancelled.")

    try:
        info = googleInfo(request, code)
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return refuse(request, FAILED)

    if not isinstance(info, dict):
        return refuse(request, FAILED)

    if info.get("email_verified") is not True:
        return refuse(
            request,
            "Your Google email isn’t verified, so we can’t use it to sign you in.",
        )

    try:
        email = NormalizedEmailField(max_length=150).clean(info.get("email"))
    except ValidationError:
        return refuse(request, FAILED)

    users = User.objects.filter(Q(username__iexact=email) | Q(email__iexact=email)).order_by("pk")

    user = users.first()

    if saved.get("mode") == "signin" and user is None:
        return redirect("register")

    created = False

    if user is None:
        name = info.get("name")
        name = (name if isinstance(name, str) and name else email.split("@")[0])[:60]

        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    username=email, email=email, password=None, first_name=name
                )

                Profile.objects.create(user=user, role=saved["role"])

            created = True
        except IntegrityError:
            user = users.first()

            if user is None:
                raise

    with transaction.atomic():
        user = lock_user(user.pk)

        if (
            user is None
            or user.email.casefold() != email.casefold()
            or users.exclude(pk=user.pk).exists()
        ):
            return refuse(request, FAILED)

        if user.is_staff:
            return refuse(request, "Staff accounts must sign in with their password.")

        if not user.is_active:
            return refuse(request, "This account is suspended, so you can’t sign in.")

        setEmailOk(user)

    if created and not saved.get("return"):
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        markHuman(request)
        messages.success(request, f"Welcome to FlatFair, {user.first_name}.")

        return redirect(saved["next"] or homeFor(user))

    if saved.get("return"):
        nonce = secrets.token_urlsafe(24)

        token = issueToken(
            RELAY_PURPOSE,
            {
                "userId": user.pk,
                "jti": nonce,
                "next": saved["next"],
                "emailPending": False,
            },
            seconds,
        )

        relayCache.set("google-relay:" + nonce, True, timeout=seconds)

        return redirect(
            saved["return"] + reverse("google_relay") + "?" + urlencode({"token": token})
        )

    if created:
        nextUrl = reverse("account_settings")
    else:
        nextUrl = saved["next"] or homeFor(user)

    markHuman(request)

    if not created and totpEnabled(user):
        request.session[PENDING_KEY] = {"userId": user.pk, "path": nextUrl}
        request.session["totpAttempts"] = 0

        return redirect("authenticator")

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    markHuman(request)

    if not created:
        messages.success(
            request,
            f"Welcome back, {user.first_name}." if user.first_name else "Welcome back.",
        )

    return redirect(nextUrl)


@never_cache
@require_GET
def googleRelay(request):
    relay_failed = "That Google sign-in link has expired. Please try again."

    try:
        saved = readToken(RELAY_PURPOSE, request.GET.get("token", ""))

        if (
            not isinstance(saved, dict)
            or not isinstance(saved.get("userId"), int)
            or not isinstance(saved.get("jti"), str)
            or len(saved["jti"]) != 32
            or not isinstance(saved.get("next"), str)
        ):
            return refuse(request, relay_failed)

        if not relayCache.delete("google-relay:" + saved["jti"]):
            return refuse(request, relay_failed)

        user = User.objects.get(pk=saved["userId"], is_active=True, is_staff=False)
    except (User.DoesNotExist, ValueError, TypeError):
        return refuse(request, relay_failed)

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session.cycle_key()
    markHuman(request)

    if saved.get("emailPending"):
        request.session[RETURN_KEY] = reverse("account_settings")

        return redirect("email_check")

    nextUrl = saved["next"]

    if nextUrl.startswith("/") and url_has_allowed_host_and_scheme(
        nextUrl, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(nextUrl)

    return redirect("home")
