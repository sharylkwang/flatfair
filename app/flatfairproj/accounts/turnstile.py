import time
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.core.signing import BadSignature
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
PASS_COOKIE = "ff_check"
PASS_SALT = "flatfair.turnstile"
CHECK_FAILED = "That didn’t go through. Please try the security check again."
FORM_KEY = "humanCheckedAt"


def turnstileField(request):
    return {
        "turnstileOn": settings.TURNSTILE_ENABLED,
        "turnstileKey": settings.TURNSTILE_SITE_KEY,
    }


def tokenOk(token, ip):
    if not token or len(token) > 2048:
        return False

    data = {"secret": settings.TURNSTILE_SECRET_KEY, "response": token, "remoteip": ip}

    try:
        result = requests.post(VERIFY_URL, data=data, timeout=5).json()
    except (requests.RequestException, ValueError):
        return False

    return isinstance(result, dict) and result.get("success") is True


def formPasses(request):
    if not settings.TURNSTILE_ENABLED:
        return True


    token = request.POST.get("cf-turnstile-response", "")
    passed = tokenOk(token, request.META.get("REMOTE_ADDR", ""))

    if passed:
        markHuman(request)

    return passed


def markHuman(request):
    request.session[FORM_KEY] = time.time()


def human_ok(request):
    stamp = request.session.get(FORM_KEY, 0)

    return isinstance(stamp, (int, float)) and 0 <= time.time() - stamp < 600


def has_pass(request):
    try:
        value = request.get_signed_cookie(
            PASS_COOKIE, salt=PASS_SALT, max_age=settings.TURNSTILE_PASS_HOURS * 3600
        )
    except (KeyError, BadSignature):
        return False



    return value == "1"


def isVisit(request):
    if request.method not in ("GET", "HEAD"):
        return False

    if request.path == "/check":
        return False


    return not request.path.startswith(
        (
            (
                "/static/",
                "/media/",
                "/api/",
                "/accounts/google",
                "/favicon.ico",
                "/robots.txt",
            )
        )
    )


class TurnstileGate:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        gateOn = settings.TURNSTILE_ENABLED and settings.TURNSTILE_GATE

        if gateOn and isVisit(request) and not has_pass(request):
            query = urlencode({"next": request.get_full_path()})



            return redirect(reverse("human_check") + "?" + query)

        return self.get_response(request)


def checkUrl(request, nextUrl):
    query = urlencode({"next": nextUrl, "force": "1"})

    return reverse("human_check") + "?" + query


def safeNext(request):
    nextUrl = request.POST.get("next") or request.GET.get("next") or "/"

    if nextUrl.startswith("/") and url_has_allowed_host_and_scheme(
        nextUrl, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return nextUrl


    return "/"


def joinError(request, nextUrl, forced):
    if not request.session.get("pendingSignupEmailCheck"):
        return None

    from accounts.verification import makeCode

    if makeCode(request, request.user):
        request.session.pop("pendingSignupEmailCheck", None)


        return None

    context = {
        "next": nextUrl,
        "force": forced,
        "error": "Sorry, we couldn’t send your code just now. Please try again in a minute.",
    }


    return render(request, "accounts/check.html", context, status=503)


@never_cache
def humanCheck(request):
    nextUrl = safeNext(request)


    forced = request.GET.get("force") == "1" or request.POST.get("force") == "1"

    if not settings.TURNSTILE_ENABLED:
        failure = joinError(request, nextUrl, forced)

        if failure is not None:
            return failure

        return redirect(nextUrl)

    if (not settings.TURNSTILE_GATE and not forced) or (
        has_pass(request) and not forced
    ):
        return redirect(nextUrl)


    if (request.method == "POST" and formPasses(request)) or (
        request.method == "GET" and human_ok(request)
    ):
        failure = joinError(request, nextUrl, forced)

        if failure is not None:
            return failure

        if request.session.pop("pendingSignupAuthenticator", False):
            from accounts.tokens import SETUP_KEY

            request.session[SETUP_KEY] = {"required": True}

        if request.session.pop("welcomeAfterCloudflare", False):
            from django.contrib import messages


            messages.success(request, f"Welcome back, {request.user.first_name}.")

        reply = redirect(nextUrl)

        reply.set_signed_cookie(
            PASS_COOKIE,
            "1",
            salt=PASS_SALT,
            httponly=True,
            samesite="Lax",
            secure=request.is_secure(),
        )

        return reply

    context = {"next": nextUrl, "force": forced}



    if request.method == "POST":
        context["error"] = CHECK_FAILED


        return render(request, "accounts/check.html", context, status=403)


    return render(request, "accounts/check.html", context)
