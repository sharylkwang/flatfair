import base64
import hmac
import secrets
import time
from datetime import timedelta
from io import BytesIO
from hashlib import sha256

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from urllib.parse import urlencode
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

import qrcode

from storefront.models import lock_user
from .forms import CodeForm, TotpForm
from .models import EmailCheck
from .tokens import (
    SETUP_KEY,
    PENDING_KEY,
    addDevice,
    setupLink,
    useSpare,
    validCode,
    addTry,
)
from .views import hashCode, log

CODE_MINUTES = 15
RESEND_SECONDS = 60
DEMO_KEY = "emailCheckDemo"
RETURN_KEY = "emailCheckReturn"
WAITING_KEY = "emailCheckUser"
SEND_FAILED = "Sorry, we couldn’t send your code just now. Tap “Send a new code” in a minute to try again."


def waitEmail(request, user, path):
    request.session[WAITING_KEY] = {"userId": user.pk, "at": time.time()}
    request.session[RETURN_KEY] = path


def liveCode(user):
    return (
        EmailCheck.objects.filter(
            user=user, verifiedAt__isnull=True, expiresAt__gt=timezone.now()
        )
        .exclude(codeHash="")
        .exists()
    )


def held_user(request):
    from django.contrib.auth.models import User

    data = request.session.get(WAITING_KEY)


    if not isinstance(data, dict) or not isinstance(data.get("userId"), int):
        return None

    if not isinstance(data.get("at"), (int, float)) or time.time() - data["at"] > 3600:
        request.session.pop(WAITING_KEY, None)

        return None

    return User.objects.filter(pk=data["userId"], is_active=True).first()


def emailVerified(user):
    return EmailCheck.objects.filter(user=user, verifiedAt__isnull=False).exists()


def totpEnabled(user):
    try:
        device = user.totpDevice
    except ObjectDoesNotExist:
        return False

    return bool(device.enabledAt)


def needsTotp(user):
    try:
        device = user.totpDevice
    except ObjectDoesNotExist:
        return False

    return bool(device.setupRequired and not device.enabledAt)


def end_login(request, user, path="/"):
    from django.contrib.auth import login

    if totpEnabled(user):
        request.session[PENDING_KEY] = {"userId": user.pk, "path": path}


        return redirect("authenticator")

    login(request, user)

    return redirect(
        reverse("human_check") + "?" + urlencode({"next": path, "force": "1"})
    )


def setEmailOk(user):
    EmailCheck.objects.update_or_create(
        user=user,
        defaults={
            "verifiedAt": timezone.now(),
            "codeHash": "",
            "expiresAt": timezone.now(),
        },
    )


def demoMode():
    return settings.DEBUG and not settings.EMAIL_HOST


def demoCode(userId, nonce):
    digest = hmac.new(
        settings.SECRET_KEY.encode(), f"email:{userId}:{nonce}".encode(), sha256
    )

    return f"{int(digest.hexdigest(), 16) % 10 ** 6:06d}"


def sendCode(user, code, to=None):
    from storefront.notices import buildEmail



    target = to or user.email

    sent = buildEmail(
        "Your FlatFair verification code",
        target,
        "Confirm your email",
        name=user.first_name,
        lines=[f"Enter this code on FlatFair to confirm {target}."],
        code=code,
        notes=[
            f"The code expires in {CODE_MINUTES} minutes. If you ask for a new one, only the newest code works.",
            "Didn’t ask for this? You can ignore this email. Nothing changes until the code is entered.",
        ],
        preview=f"Your code is {code}",
    ).send(fail_silently=False)

    if sent != 1:
        raise RuntimeError("Email backend did not accept the verification email")


    return True


def makeCode(request, user, notify=False):
    deferred = transaction.get_connection().in_atomic_block
    delivery = {"ok": True}

    with transaction.atomic():
        user = lock_user(user.pk)

        if not user or not user.is_active:
            return False

        check, created = EmailCheck.objects.get_or_create(user=user)

        if check.verifiedAt and not check.pendingEmail:
            return False


        now = timezone.now()

        if check.sentAt and (now - check.sentAt).total_seconds() < RESEND_SECONDS:
            return False

        if addTry(f"email-check:user:{user.pk}", 3600) > 3:
            return False

        mail_hash = sha256(request.META.get("REMOTE_ADDR", "").encode()).hexdigest()

        if addTry("email-check:ip:" + mail_hash, 3600) > 10:
            return False

        nonce = secrets.token_hex(32)

        if demoMode():
            code = demoCode(user.pk, nonce)
        else:
            code = f"{secrets.randbelow(10 ** 6):06d}"

        old = {
            field: getattr(check, field)
            for field in ("codeHash", "expiresAt", "attempts", "sentCount", "sentAt")
        }
        pending = check.pendingEmail
        digest = hashCode(user.pk, "email:" + code)
        check.codeHash = digest



        check.expiresAt = now + timedelta(minutes=CODE_MINUTES)
        check.attempts = 0
        check.sentCount += 1
        check.sentAt = now
        check.save()

        def send():
            try:
                sendCode(user, code, pending or None)
            except Exception:
                delivery["ok"] = False
                log.error("Could not send the email_check email")

                if deferred:
                    messages.error(request, SEND_FAILED)

                EmailCheck.objects.filter(
                    pk=check.pk, codeHash=digest, sentAt=now, pendingEmail=pending
                ).update(**old)


                return

            request.session.pop(DEMO_KEY, None)

            if demoMode() or settings.DEBUG:
                request.session[DEMO_KEY] = {
                    "userId": user.pk, "nonce": nonce, "code": code
                }

            if notify:
                messages.success(request, "We sent a new code. Only the newest code works.")

        transaction.on_commit(send, robust=True)

    return delivery["ok"]


def mailStart(request, user, newEmail):
    check, created = EmailCheck.objects.get_or_create(user=user)
    check.pendingEmail = newEmail
    check.codeHash = ""
    check.expiresAt = timezone.now()
    check.attempts = 0


    check.sentAt = None

    check.save(
        update_fields=["pendingEmail", "codeHash", "expiresAt", "attempts", "sentAt"]
    )

    request.session.pop(DEMO_KEY, None)

    return makeCode(request, user)


def mailDone(request, user, check):
    from copy import copy

    from django.contrib.auth.models import User
    from .models import ResetCode
    from .views import deliver

    newMail = check.pendingEmail

    if (
        User.objects.filter(email__iexact=newMail).exclude(pk=user.pk).exists()
        or User.objects.filter(username__iexact=newMail).exclude(pk=user.pk).exists()
    ):
        check.pendingEmail = ""


        check.codeHash = ""
        check.save(update_fields=["pendingEmail", "codeHash"])

        messages.error(
            request,
            "That email is now used by another account, so your email was not changed.",
        )

        return False

    oldUser = copy(user)
    user.email = newMail
    user.username = newMail
    user.save(update_fields=["username", "email"])
    check.pendingEmail = ""
    check.codeHash = ""
    check.verifiedAt = timezone.now()
    check.save(update_fields=["pendingEmail", "codeHash", "verifiedAt"])



    ResetCode.objects.filter(user=user, usedAt__isnull=True).update(
        usedAt=timezone.now()
    )

    context = {
        "newEmail": newMail,
        "supportUrl": request.build_absolute_uri(reverse("support")),
    }

    transaction.on_commit(
        lambda: deliver(
            "Your FlatFair email was changed", oldUser, "email_changed", context
        )
    )

    return True


def safePath(request, path):
    return (
        path
        if path
        and path.startswith("/")
        and url_has_allowed_host_and_scheme(
            path, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        )
        else ""
    )


def saveBack(request):
    if request.method == "GET":
        path = request.get_full_path()
    else:
        referer = request.META.get("HTTP_REFERER", "")
        host = request.build_absolute_uri("/")[:-1]

        if referer.startswith(host):
            path = referer[len(host) :]
        else:
            path = ""


    path = safePath(request, path)

    if path:
        request.session[RETURN_KEY] = path


@never_cache
@require_http_methods(["GET", "POST"])
def checkEmail(request):
    code = 5
    tries = "Too many wrong tries. Please ask for a new code."


    if request.user.is_authenticated:
        userId, signIn = request.user.pk, False
    else:
        waiting = held_user(request)

        if waiting is None:
            messages.info(request, "Please sign in to finish checking your email.")

            return redirect("signin")

        userId, signIn = waiting.pk, True

    form = CodeForm(request.POST or None)

    with transaction.atomic():
        user = lock_user(userId)



        check, created = EmailCheck.objects.get_or_create(user=user)
        change = bool(check.pendingEmail) and not signIn

        if request.method == "POST" and "cancel" in request.POST and change:
            check.pendingEmail = ""
            check.codeHash = ""
            check.save(update_fields=["pendingEmail", "codeHash"])
            messages.success(request, f"Your email stays {user.email}.")

            return redirect("account_settings")

        if check.verifiedAt and not change:
            return redirect("home")

        now = timezone.now()

        if check.sentAt:
            retryIn = max(0, RESEND_SECONDS - int((now - check.sentAt).total_seconds()))
        else:
            retryIn = 0

        if request.method == "POST" and "resend" in request.POST:
            if retryIn:
                messages.error(
                    request, "Please wait a minute before asking for another code."
                )
            elif not makeCode(request, user, notify=True):
                messages.error(
                    request,
                    "You’ve asked for too many codes. Please wait an hour, then try again.",
                )

            return redirect("email_check")

        if request.method == "POST":
            if check.attempts >= code:
                form.add_error(None, tries)
            elif form.is_valid():
                typed = hashCode(user.pk, "email:" + form.cleaned_data["code"])

                if check.expiresAt > now and hmac.compare_digest(check.codeHash, typed):
                    request.session.pop(DEMO_KEY, None)

                    if change:
                        if mailDone(request, user, check):
                            messages.success(
                                request,
                                f"Your email is now {user.email}. We told your old address.",
                            )

                        return redirect("account_settings")



                    check.verifiedAt = now
                    check.codeHash = ""
                    check.save(update_fields=["verifiedAt", "codeHash"])

                    goTo = safePath(
                        request, request.session.pop(RETURN_KEY, "")
                    ) or reverse("home")

                    if signIn:
                        request.session.pop(WAITING_KEY, None)

                        if totpEnabled(user):
                            request.session[PENDING_KEY] = {
                                "userId": user.pk,
                                "path": goTo,
                            }

                            request.session["totpAttempts"] = 0

                            return redirect("authenticator")

                        from django.contrib.auth import login

                        login(
                            request,
                            user,
                            backend="django.contrib.auth.backends.ModelBackend",
                        )


                        messages.success(
                            request,
                            (
                                f"Your email is verified. Welcome to FlatFair, {user.first_name}."
                                if user.first_name
                                else "Your email is verified. Welcome to FlatFair."
                            ),
                        )
                    else:
                        messages.success(
                            request, "Your email is verified. You can buy and sell now."
                        )

                    return redirect(goTo)

                check.attempts += 1
                check.save(update_fields=["attempts"])

                form.add_error(
                    "code",
                    (
                        tries
                        if check.attempts >= code
                        else "That code is wrong or has expired. Ask for a new code."
                    ),
                )

        shown = None
        demo = request.session.get(DEMO_KEY, {})

        if (
            demoMode()
            and demo.get("userId") == user.pk
            and check.expiresAt > now
            and check.attempts < code
        ):
            option = demo.get("code") or demoCode(user.pk, demo["nonce"])

            if hmac.compare_digest(
                check.codeHash, hashCode(user.pk, "email:" + option)
            ):
                shown = option



    context = {
        "form": form,
        "email": check.pendingEmail if change else user.email,
        "resendIn": retryIn,
        "demoCode": shown,
        "signingIn": signIn,
        "changing": change,
    }

    return render(request, "accounts/email_check.html", context)


@never_cache
@require_http_methods(["GET", "POST"])
def authenticator(request):
    from django.contrib.auth import login
    from django.contrib.auth.models import User

    pending = request.session.get(PENDING_KEY)

    if not pending:
        return redirect("signin")

    user = User.objects.filter(pk=pending.get("userId"), is_active=True).first()

    if not user or not totpEnabled(user):
        request.session.pop(PENDING_KEY, None)

        return redirect("signin")

    form = TotpForm(request.POST or None)
    tries = request.session.get("totpAttempts", 0)

    if request.method == "POST" and form.is_valid():
        if tries >= 5:
            form.add_error(None, "Too many attempts. Please start signing in again.")
        else:
            code = form.cleaned_data["code"]



            valid = validCode(user.totpDevice.secret, code) or useSpare(
                user.totpDevice, code
            )

            if valid:
                login(request, user)
                request.session["welcomeAfterCloudflare"] = True


                path = safePath(request, pending.get("path", "")) or "/"
                request.session.pop(PENDING_KEY, None)
                request.session.pop("totpAttempts", None)

                return redirect(
                    reverse("human_check")
                    + "?"
                    + urlencode({"next": path, "force": "1"})
                )

            request.session["totpAttempts"] = tries + 1

            form.add_error(
                "code", "That code isn’t right, or it has already been used."
            )

    return render(
        request, "accounts/authenticator.html", {"form": form, "recovery": True}
    )


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def totpSetup(request):
    needed = request.session.get(SETUP_KEY, {}).get("required") is True
    device, codes = addDevice(request.user, required=needed)
    image = qrcode.make(setupLink(request.user, device.secret))
    output = BytesIO()
    image.save(output, format="PNG")
    qrCode = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()
    form = TotpForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        if validCode(device.secret, form.cleaned_data["code"]):
            device.enabledAt = timezone.now()
            device.setupRequired = False


            device.save(update_fields=["enabledAt", "setupRequired"])
            request.session.pop(SETUP_KEY, None)

            messages.success(
                request, "Microsoft Authenticator is now required when you sign in."
            )

            return redirect("account_settings")

        form.add_error(
            "code", "That code isn’t valid yet. Please check your Authenticator app."
        )

    return render(
        request,
        "accounts/authenticator_setup.html",
        {
            "form": form,
            "uri": setupLink(request.user, device.secret),
            "secret": device.secret,
            "qrCode": qrCode,
            "recoveryCodes": codes,
        },
    )


class RequiredTotpSetupMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        needed = request.session.get(SETUP_KEY, {}).get("required")

        needed = needed or (request.user.is_authenticated and needsTotp(request.user))



        if needed and request.user.is_authenticated:
            allowed = {
                "authenticator_setup",
                "signout",
                "static",
                "human_check",
                "account_settings",
            }

            if (
                request.resolver_match
                and request.resolver_match.url_name not in allowed
            ):
                return redirect("authenticator_setup")

        return self.get_response(request)
