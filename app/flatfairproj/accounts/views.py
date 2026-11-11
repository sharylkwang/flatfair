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

log = logging.getLogger(__name__)
CODE_MINUTES = 10
RESEND_SECONDS = 60
WRONG_PER_CODE = 5
SESSION_KEY = "passwordReset"
TOO_MANY_CODES = "You’ve asked for too many codes. Please wait an hour, then try again."


def client_Ip(request):
    return request.META.get("REMOTE_ADDR") or None


def hashCode(userId, code):
    return hmac.new(
        settings.SECRET_KEY.encode(), f"{userId}:{code}".encode(), sha256
    ).hexdigest()


def find_user(email):
    users = User.objects.filter(email__iexact=email, is_active=True)

    return users.order_by("pk").first()


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


def deliver(subject, user, template, context):
    from storefront.notices import buildEmail

    message = buildEmail(
        subject,
        user.email,
        name=user.first_name,
        **mailBody(template, user, context),
    )



    try:
        message.send()
    except Exception as error:
        log.error("Could not send the %s email", template)


def tellChange(request, user):
    context = {
        "when": timezone.localtime(),
        "resetUrl": request.build_absolute_uri(reverse("forgot_password")),
    }



    deliver("Your FlatFair password was changed", user, "password_changed", context)


@transaction.atomic
def sendCode(request, user):
    user = lock_user(user.pk)

    if not user or not user.is_active:
        return False

    from_ip = client_Ip(request)

    if from_ip:
        codeLock, created = ResetIssuanceLock.objects.get_or_create(requestIp=from_ip)

        ResetIssuanceLock.objects.select_for_update().get(pk=codeLock.pk)

    now = timezone.now()


    dayAgo = now - timedelta(days=1)

    if ResetCode.objects.filter(user=user, createdAt__gte=dayAgo).count() >= 2:
        return False

    hourAgo = now - timedelta(hours=1)

    if ResetCode.objects.filter(user=user, createdAt__gte=hourAgo).count() >= 3:
        return False


    if (
        from_ip
        and ResetCode.objects.filter(requestIp=from_ip, createdAt__gte=hourAgo).count()
        >= 10
    ):
        return False

    ResetCode.objects.filter(createdAt__lt=now - timedelta(days=1)).delete()
    ResetCode.objects.filter(user=user, usedAt__isnull=True).update(expiresAt=now)
    code = f"{secrets.randbelow(10 ** 6):06d}"


    ResetCode.objects.create(
        user=user,
        codeHash=hashCode(user.pk, code),
        requestIp=from_ip,
        expiresAt=now + timedelta(minutes=CODE_MINUTES),
    )

    transaction.on_commit(
        lambda: deliver(
            "Your FlatFair reset code",
            user,
            "reset_code",
            {"code": code, "minutes": CODE_MINUTES},
        )
    )

    return True


def activeCode(email):
    codes = ResetCode.objects.filter(
        user__email__iexact=email,
        user__is_active=True,
        usedAt__isnull=True,
        verifiedAt__isnull=True,
        expiresAt__gt=timezone.now(),
        attempts__lt=WRONG_PER_CODE,
    )

    return codes.order_by("-createdAt", "-pk").first()


def goodCode(state):
    since = timezone.now() - timedelta(minutes=CODE_MINUTES)

    codes = ResetCode.objects.select_related("user").filter(
        pk=state.get("codeId"),
        usedAt__isnull=True,
        verifiedAt__gte=since,
        expiresAt__gt=timezone.now(),
        user__is_active=True,
    )

    return codes.first()


@never_cache
def forgot(request):
    state = request.session.get(SESSION_KEY, {})
    form = EmailForm(request.POST or None, initial={"email": state.get("email", "")})



    if request.method == "POST" and not formPasses(request):
        form.add_error(None, CHECK_FAILED)

    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]

        if (
            email == state.get("email")
            and time.time() - state["sentAt"] < RESEND_SECONDS
        ):
            return redirect("forgot_code")

        user = find_user(email)

        if user and not sendCode(request, user):
            form.add_error(None, TOO_MANY_CODES)
        else:
            request.session[SESSION_KEY] = {
                "email": email,
                "sentAt": time.time(),
                "wrong": 0,
            }

            return redirect("forgot_code")

    return render(request, "accounts/forgot_email.html", {"form": form, "step": 1})


@never_cache
def forgotCode(request):
    tries = "Too many wrong tries. Please ask for a new code."
    state = request.session.get(SESSION_KEY)


    if not state:
        return redirect("forgot_password")

    if goodCode(state):
        return redirect("forgot_new")

    retryIn = int(RESEND_SECONDS - (time.time() - state["sentAt"]))
    form = CodeForm()

    if request.method == "POST" and "resend" in request.POST:
        if retryIn <= 0:
            user = find_user(state["email"])

            if user and not sendCode(request, user):
                messages.error(request, TOO_MANY_CODES)
            else:
                state["sentAt"] = time.time()
                state["wrong"] = 0
                request.session[SESSION_KEY] = state

                messages.success(
                    request, "We sent a new code. Only the newest code works."
                )

        return redirect("forgot_code")

    if request.method == "POST":
        form = CodeForm(request.POST)



        if state["wrong"] >= WRONG_PER_CODE:
            form.add_error(None, tries)
        elif form.is_valid():
            code = activeCode(state["email"])
            typed = form.cleaned_data["code"]

            if code and hmac.compare_digest(
                code.codeHash, hashCode(code.user_id, typed)
            ):
                claimed = ResetCode.objects.filter(
                    pk=code.pk,
                    usedAt__isnull=True,
                    verifiedAt__isnull=True,
                    expiresAt__gt=timezone.now(),
                    attempts__lt=WRONG_PER_CODE,
                    user__is_active=True,
                ).update(verifiedAt=timezone.now())

                if claimed:
                    state["codeId"] = code.pk
                    request.session[SESSION_KEY] = state


                    return redirect("forgot_new")

            if code:
                ResetCode.objects.filter(
                    pk=code.pk, attempts__lt=WRONG_PER_CODE, verifiedAt__isnull=True
                ).update(attempts=F("attempts") + 1)

            state["wrong"] += 1
            request.session[SESSION_KEY] = state

            if state["wrong"] >= WRONG_PER_CODE:
                form.add_error("code", tries)
            else:
                form.add_error(
                    "code",
                    "That code is wrong or has expired. Check the email and try again.",
                )

    context = {
        "form": form,
        "step": 2,
        "email": state["email"],
        "resendIn": max(retryIn, 0),
        "minutes": CODE_MINUTES,
    }

    return render(request, "accounts/forgot_code.html", context)


@never_cache
def forgotNew(request):
    state = request.session.get(SESSION_KEY, {})
    code = goodCode(state)

    if not code:
        if state.get("codeId"):
            del request.session[SESSION_KEY]

            messages.error(request, "Your code has expired. Ask for a new one.")



        return redirect("forgot_password")

    user = code.user
    form = NewPasswordForm(user, request.POST or None)

    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            now = timezone.now()

            used = ResetCode.objects.filter(
                pk=code.pk,
                usedAt__isnull=True,
                verifiedAt__gte=now - timedelta(minutes=CODE_MINUTES),
                expiresAt__gt=now,
                user__is_active=True,
            ).update(usedAt=now)

            if not used:
                del request.session[SESSION_KEY]

                messages.error(request, "Your code has expired. Ask for a new one.")

                return redirect("forgot_password")

            ResetCode.objects.filter(user=user, usedAt__isnull=True).update(usedAt=now)
            form.save()

        signKey = sha256(
            f"{state['email']}:{request.META.get('REMOTE_ADDR', '')}".encode()
        ).hexdigest()

        cache.delete("signin:" + signKey)


        tellChange(request, user)

        del request.session[SESSION_KEY]

        messages.success(request, "Password changed. Sign in with your new password.")



        return redirect("signin")

    context = {
        "form": form,
        "step": 3,
        "email": state["email"],
        "rules": PASSWORD_RULES,
    }

    return render(request, "accounts/forgot_new.html", context)


def homeFor(user):
    if user.is_staff:
        return reverse("admin_overview")

    profile = getattr(user, "profile", None)

    if profile is not None and profile.role == "seller":
        return reverse("dashboard")

    return reverse("home")


def landing(request, user):
    nextUrl = nextPath(request)

    if nextUrl:
        return redirect(nextUrl)

    if user.is_staff:
        return redirect("admin_overview")

    if currentMode(request) == "seller":
        return redirect("dashboard")

    return redirect("home")


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
    from .verification import emailVerified, totpEnabled

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


def nextPath(request):
    nextUrl = request.GET.get("next", "")

    if nextUrl.startswith("/") and url_has_allowed_host_and_scheme(
        nextUrl, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return nextUrl

    return ""


def signinNote(request):
    nextUrl = nextPath(request)



    if not nextUrl:
        return ""

    action = "continue"

    for prefix, text in (
        ("/my-offers", "see your offers and viewings"),
        ("/saved", "see your saved flats"),
        ("/notifications", "see your notifications"),
        ("/seller", "manage your flat"),
    ):
        if nextUrl.startswith(prefix):
            action = text

            break

    if nextUrl.startswith("/listing/"):
        action = "see this flat"

        if "#listing-action-card" in nextUrl:
            action = "make an offer or book a viewing"

        if "save=1" in nextUrl:
            action = "save this flat"

    return f"Sign in to {action}."


def signout(request):
    if request.method == "POST":
        logout(request)
        messages.success(request, "You’ve signed out.")

    return redirect("home")
