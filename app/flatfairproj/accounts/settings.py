from storefront.forms import ErrorAttrsMixin

from django import forms
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from adminpanel.models import Ticket
from storefront.models import hasRole, notAllowed
from .models import ResetCode
from accounts.tokens import PASSWORD_RULES, keepPass, REUSED, wasUsed, cache, addTry
from storefront.forms import checkPass, emailTaken
from .forms import NormalizedEmailField
from .views import tellChange
from .verification import emailVerified, mailStart

DELETION_MESSAGE = (
    "Please delete my FlatFair account. I confirmed this request with my password."
)
PASSWORD_ATTEMPTS = 5
PASSWORD_LOCKED = "Too many failed attempts. Please wait 15 minutes, then try again."


def triesKey(user):
    return f"account-password:{user.pk}"


class AccountForm(forms.Form):
    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

class AccountNameForm(AccountForm):
    fullName = forms.CharField(
        label="Full name",
        max_length=60,
        error_messages={"required": "Enter your full name."},
        widget=forms.TextInput(attrs={"autocomplete": "name"}),
    )


class PasswordConfirmationForm(AccountForm):
    currentPassword = forms.CharField(
        label="Current password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
        error_messages={"required": "Enter your current password."},
    )

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)

        if not user.has_usable_password():
            self.fields["currentPassword"].required = False

    def clean_currentPassword(self):
        if not self.user.has_usable_password():
            raise forms.ValidationError(
                (
                    "Please set a password below before you change your email or ask us to delete your account."
                )
            )



        key = triesKey(self.user)

        if cache.get(key, 0) >= PASSWORD_ATTEMPTS:
            raise forms.ValidationError(PASSWORD_LOCKED)

        password = self.cleaned_data["currentPassword"]

        if not self.user.check_password(password):
            tries = addTry(key, 900)

            if tries >= PASSWORD_ATTEMPTS:
                raise forms.ValidationError(PASSWORD_LOCKED)

            raise forms.ValidationError(
                "That isn’t your current password. Please try again."
            )


        cache.delete(key)



        return password


class AccountEmailForm(PasswordConfirmationForm):
    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)

        if not user.has_usable_password():
            del self.fields["currentPassword"]

    email = NormalizedEmailField(
        label="New email",
        max_length=150,
        widget=forms.EmailInput(
            attrs={"autocomplete": "email", "autocapitalize": "off"}
        ),
    )

    def clean_email(self):
        email = self.cleaned_data["email"]

        if (
            emailTaken(email, exclude=self.user.pk)
            or User.objects.filter(username__iexact=email)
            .exclude(pk=self.user.pk)
            .exists()
        ):
            raise forms.ValidationError(
                "That email is already registered. Please use another one."
            )


        if email == self.user.email and email == self.user.username:
            raise forms.ValidationError("Enter a different email address.")

        return email


class AccountPasswordForm(PasswordConfirmationForm):
    password = forms.CharField(
        label="New password",
        strip=False,
        max_length=128,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        error_messages={"required": "Enter a new password."},
    )
    confirmPassword = forms.CharField(
        label="Confirm new password",
        strip=False,
        max_length=128,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        error_messages={"required": "Type your new password again."},
    )

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)



        if not user.has_usable_password():
            del self.fields["currentPassword"]

    def clean(self):
        data = super().clean()
        password = data.get("password")

        checkPass(
            self,
            {**data, "email": self.user.email, "fullName": self.user.first_name},
        )


        if (
            password
            and data.get("confirmPassword")
            and password != data["confirmPassword"]
        ):
            self.add_error(
                "confirmPassword",
                "Passwords don't match. Type the same password twice.",
            )

        if password and wasUsed(self.user, password):
            self.add_error("password", REUSED)

        return data


class AccountDeletionForm(PasswordConfirmationForm):
    pass


ACCOUNT_FORMS = {
    "name": AccountNameForm,
    "email": AccountEmailForm,
    "password": AccountPasswordForm,
    "delete": AccountDeletionForm,
}


def save_form(request, user, action, form):
    if action == "name":
        user.first_name = form.cleaned_data["fullName"]
        user.save(update_fields=["first_name"])
        messages.success(request, "Your full name was updated.")
    elif action == "email":
        if not mailStart(request, user, form.cleaned_data["email"]):
            messages.error(
                request,
                "Sorry, we couldn’t send a code to that address just now. Check it, then tap “Send a new code” in a minute.",
            )

        return "email_check"
    elif action == "password":
        keepPass(user)
        user.set_password(form.cleaned_data["password"])
        user.save(update_fields=["password"])

        ResetCode.objects.filter(user=user, usedAt__isnull=True).update(
            usedAt=timezone.now()
        )

        update_session_auth_hash(request, user)
        transaction.on_commit(lambda: tellChange(request, user))



        messages.success(request, "Your password was changed. You are still signed in.")
    else:
        _, created = Ticket.objects.get_or_create(
            customer=user,
            topic="account",
            message=DELETION_MESSAGE,
            status="open",
            defaults={"email": user.email},
        )



        messages.success(
            request,
            (
                "Your account deletion request is with our support team. Follow it in My messages."
                if created
                else "Your account deletion request is already with our support team. Follow it in My messages."
            ),
        )


@sensitive_post_parameters()
@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def myAccount(request):
    if request.user.is_staff:
        return notAllowed(request, "buyer or seller")


    if not any(hasRole(request.user, role) for role in ("buyer", "seller")):
        return notAllowed(request, "buyer or seller")

    if request.method == "POST":
        action = request.POST.get("action")
    else:
        action = None

    if request.method == "POST" and action not in ACCOUNT_FORMS:
        messages.error(request, "Please choose an account setting to update.")

        return redirect("account_settings")


    with transaction.atomic():
        if request.method == "POST":
            user = User.objects.select_for_update().get(pk=request.user.pk)
        else:
            user = request.user

        forms = {
            name: formClass(
                user,
                request.POST if action == name else None,
                prefix=name,
                initial={"fullName": user.first_name},
            )
            for name, formClass in ACCOUNT_FORMS.items()
        }

        if action:
            form = forms[action]



            if form.is_valid():
                try:
                    with transaction.atomic():
                        goTo = save_form(request, user, action, form)
                except IntegrityError:
                    if action != "email":
                        raise

                    form.add_error(
                        "email",
                        "That email is already registered. Please use another one.",
                    )

                    form.fields["email"].widget.attrs.update(
                        {
                            "aria-invalid": "true",
                            "aria-describedby": f"{form['email'].auto_id}_error",
                        }
                    )
                else:
                    return redirect(goTo or "account_settings")

            messages.error(request, "Please check the account form and try again.")

    context = {
        "errorForm": forms.get(action),
        "errorTarget": f"account-{action}",
        "activeAction": action or "name",
        "blockedAction": False,
        "nameForm": forms["name"],
        "emailForm": forms["email"],
        "passwordForm": forms["password"],
        "deletionForm": forms["delete"],
        "hasPassword": user.has_usable_password(),
        "emailVerified": emailVerified(user),
        "pendingEmail": getattr(getattr(user, "emailCheck", None), "pendingEmail", ""),
        "totpEnabled": getattr(getattr(user, "totpDevice", None), "enabledAt", None)
        is not None,
        "rules": PASSWORD_RULES,
    }



    return render(request, "accounts/settings.html", context)
