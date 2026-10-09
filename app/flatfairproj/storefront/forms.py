from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.validators import MaxValueValidator
from django.db.models import Q
from django.utils import timezone
from accounts.forms import EMAIL_INVALID, NormalizedEmailField, tidyEmail
from storefront.models import FLAT_RULES, Profile
from .templatetags.estate_tags import flat_label

FLAT_TYPES = ["2 ROOM", "3 ROOM", "4 ROOM", "5 ROOM", "EXECUTIVE"]
class RegisterForm(ErrorAttrsMixin, forms.Form):
    fullName = forms.CharField(
        max_length=60,
        error_messages={
            "required": "Enter your full name.",
            "max_length": "Keep your name to 60 characters or fewer.",
        },
        widget=forms.TextInput(attrs={"autocomplete": "name", "placeholder": "e.g. Tan Wei Ling"}),
    )
    email = NormalizedEmailField(
        max_length=150,
        error_messages={
            "required": "Enter your email address.",
            "invalid": EMAIL_INVALID,
        },
        widget=forms.EmailInput(
            attrs={
                "autocomplete": "email",
                "autocapitalize": "off",
                "placeholder": "e.g. weiling@gmail.com",
            }
        ),
    )
    password = forms.CharField(
        strip=False,
        min_length=8,
        widget=forms.PasswordInput(
            attrs={
                "autocomplete": "new-password",
                "placeholder": "At least 8 characters",
            }
        ),
        error_messages={
            "required": "Choose a password with at least 8 characters.",
            "min_length": "Use at least 8 characters.",
        },
    )
    confirmPassword = forms.CharField(
        strip=False,
        label="Confirm password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        error_messages={"required": "Type your password again."},
    )
    role = forms.ChoiceField(
        choices=Profile.ROLE_CHOICES,
        widget=forms.RadioSelect,
        error_messages={
            "required": "Choose whether you want to buy or sell.",
            "invalid_choice": "Choose whether you want to buy or sell.",
        },
    )

    def clean_email(self):
        email = tidyEmail(self.cleaned_data["email"])

        if emailTaken(email):
            raise forms.ValidationError("This email already has an account. Sign in instead.")

        return email

    def clean(self):
        data = super().clean()
        checkPass(self, data)

        if (
            data.get("password")
            and data.get("confirmPassword")
            and data["password"] != data["confirmPassword"]
        ):
            self.add_error(
                "confirmPassword",
                "Passwords don't match. Type the same password twice.",
            )

        return data


