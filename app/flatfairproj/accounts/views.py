import hmac
import logging
import secrets
import time
from datetime import timedelta
from hashlib import sha256
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import F
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache

from storefront.models import lock_user, currentMode, Profile
from storefront.forms import RegisterForm, emailTaken
from .forms import CodeForm, EmailForm, NewPasswordForm, tidyEmail
from .models import ResetCode, ResetIssuanceLock
from accounts.tokens import PASSWORD_RULES, PENDING_KEY, cache, cache as authCache, addTry
from .turnstile import CHECK_FAILED, formPasses

def mailBody(template, user, context):
    if template == "reset_code":
        return {
            "heading": "Reset your password",
            "lines": ["Enter this code on the reset page to choose a new password."],
            "code": context["code"],
            "notes": [
                f"The code expires in {context['minutes']} minutes, and only the newest code works.",
                "Didn’t ask for this? You can ignore this email. Your password stays the same.",
            ],
            "preview": f"Your reset code is {context['code']}",
        }

    if template == "password_changed":
        return {
            "heading": "Your password was changed",
            "lines": [
                f"The password for {user.email} was changed on {context['when']:%-d %b %Y, %-I:%M %p} (Singapore time). Anyone signed in on another device has been signed out."
            ],
            "button": {"label": "This wasn’t me", "url": context["resetUrl"]},
            "notes": ["If you made this change, you don’t need to do anything."],
        }

    return {
        "heading": "Your sign-in email was changed",
        "lines": ["You now sign in to FlatFair with your new email."],
        "rows": [
            ("Old email", user.email, False),
            ("New email", context["newEmail"], True),
        ],
        "button": {"label": "This wasn’t me", "url": context["supportUrl"]},
        "notes": ["If you made this change, you don’t need to do anything."],
    }


def homeFor(user):
    if user.is_staff:
        return reverse("admin_overview")

    profile = getattr(user, "profile", None)

    if profile is not None and profile.role == "seller":
        return reverse("dashboard")

    return reverse("home")


def register(request):
    if request.user.is_authenticated:
        return landing(request, request.user)

    if request.method == "POST":
        form = RegisterForm(request.POST)

        if not formPasses(request):
            form.add_error(None, CHECK_FAILED)

        if form.is_valid():
            data = form.cleaned_data

            try:
                with transaction.atomic():
                    user = User.objects.create_user(
                        username=data["email"],
                        email=data["email"],
                        password=data["password"],
                        first_name=data["fullName"],
                    )

                    Profile.objects.create(user=user, role=data["role"])
            except IntegrityError:
                if not emailTaken(data["email"]):
                    raise

                form.add_error("email", "This email can’t be used for a new account.")
            else:
                from accounts.verification import SEND_FAILED, waitEmail, makeCode

                backUrl = nextPath(request)

                if not backUrl and data["role"] == "seller":
                    backUrl = reverse("dashboard")

                waitEmail(request, user, backUrl or reverse("account_settings"))

                if not makeCode(request, user):
                    messages.error(request, SEND_FAILED)

                return redirect("email_check")
    else:
        role = request.GET.get("role")
        form = RegisterForm(initial={"role": role if role in ("buyer", "seller") else None})

    return render(request, "storefront/register.html", {"form": form, "rules": PASSWORD_RULES})


def signin(request):
    if request.user.is_authenticated:
        return landing(request, request.user)

    if request.method == "POST":
        if not formPasses(request):
            return render(
                request,
                "storefront/signin.html",
                {"nextNote": signinNote(request), "signinError": CHECK_FAILED},
            )

        email = tidyEmail(request.POST.get("email", ""))
        password = request.POST.get("password", "")

        key = (
            "signin:"
            + sha256(f"{email}:{request.META.get('REMOTE_ADDR', '')}".encode()).hexdigest()
        )

        tries = authCache.get(key, 0)

        if tries >= 5:
            error = "Too many failed attempts. Please wait 15 minutes, then try again."

            return render(
                request,
                "storefront/signin.html",
                {"nextNote": signinNote(request), "signinError": error},
            )

        user = authenticate(request, username=email, password=password)

        if user is not None:
            authCache.delete(key)

            if totpEnabled(user):
                request.session[PENDING_KEY] = {
                    "userId": user.pk,
                    "path": nextPath(request) or homeFor(user),
                }
                request.session["totpAttempts"] = 0

                return redirect("authenticator")

            if not emailVerified(user):
                from accounts.verification import SEND_FAILED, waitEmail, makeCode, liveCode

                waitEmail(request, user, nextPath(request) or homeFor(user))

                if not makeCode(request, user) and not liveCode(user):
                    messages.error(request, SEND_FAILED)

                return redirect("email_check")

            login(request, user)
            request.session["welcomeAfterCloudflare"] = True

            return redirect(
                reverse("human_check")
                + "?"
                + urlencode({"next": nextPath(request) or homeFor(user), "force": "1"})
            )

        addTry(key, 900)
        error = "That email or password isn’t right. Please check both and try again."

        return render(
            request,
            "storefront/signin.html",
            {"nextNote": signinNote(request), "signinError": error},
        )

    return render(request, "storefront/signin.html", {"nextNote": signinNote(request)})


def signout(request):
    if request.method == "POST":
        logout(request)
        messages.success(request, "You’ve signed out.")

    return redirect("home")
