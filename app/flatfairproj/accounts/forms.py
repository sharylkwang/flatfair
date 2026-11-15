from unicodedata import normalize

from django import forms
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from accounts.tokens import REUSED, keepPass, wasUsed

EMAIL_INVALID = "Enter an email address like name@example.com."


def tidyEmail(value):
    return normalize("NFKC", value).lower().strip()


class NormalizedEmailField(forms.EmailField):
    default_error_messages = {
        "required": "Enter an email address.",
        "invalid": EMAIL_INVALID,
        "max_length": "That email is too long. Please use a shorter one.",
    }

    def to_python(self, value):
        return tidyEmail(super().to_python(value))


class EmailForm(forms.Form):
    email = NormalizedEmailField(max_length=254)


class CodeForm(forms.Form):
    code = forms.CharField(
        max_length=12,
        error_messages={"required": "Type the 6-digit code from the email."},
    )

    def clean_code(self):
        code = "".join(self.cleaned_data["code"].split())

        if len(code) != 6 or not code.isdigit():
            raise ValidationError("Type the 6-digit code from the email.")

        return code


class TotpForm(forms.Form):
    code = forms.CharField(max_length=12, label="Authenticator code")

    def clean_code(self):
        code = "".join(self.cleaned_data["code"].split()).replace("-", "").upper()

        if len(code) == 6 and code.isdigit():
            return code



        if len(code) == 8 and all(char in "0123456789ABCDEF" for char in code):
            return code

        raise ValidationError(
            "Type the 6-digit code from Microsoft Authenticator, or one of your recovery codes."
        )


class NewPasswordForm(forms.Form):
    password = forms.CharField(
        strip=False, max_length=128, error_messages={"required": "Type a new password."}
    )
    confirm = forms.CharField(
        strip=False,
        max_length=128,
        error_messages={"required": "Type your new password again."},
    )



    def __init__(self, user, *args, **kwargs):
        self.user = user


        super().__init__(*args, **kwargs)

    def clean(self):
        data = super().clean()



        password = data.get("password")


        confirm = data.get("confirm")



        if not password or not confirm:
            return data

        if password != confirm:
            self.add_error(
                "confirm", "Passwords don't match. Type the same password twice."
            )

            return data

        try:
            validate_password(password, self.user)
        except ValidationError as error:
            self.add_error("password", error)



            return data

        if wasUsed(self.user, password):
            self.add_error("password", REUSED)

        return data

    def save(self):
        keepPass(self.user)


        self.user.set_password(self.cleaned_data["password"])


        self.user.save(update_fields=["password"])


        return self.user


class ChangePasswordForm(PasswordChangeForm):
    def clean(self):
        data = super().clean()



        password = data.get("new_password2")


        if password and wasUsed(self.user, password):
            self.add_error("new_password2", REUSED)

        return data

    def save(self, commit=True):
        keepPass(self.user)



        return super().save(commit)
