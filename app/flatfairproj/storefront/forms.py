class ErrorAttrsMixin:
    def full_clean(self):
        super().full_clean()

        for name in self.errors:
            if name in self.fields:
                self.fields[name].widget.attrs.update(
                    {"aria-invalid": "true", "aria-describedby": f"id_{name}_error"}
                )


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
LEASE_MIN = 1966
MOP_YEARS = 5


def emailTaken(email, exclude=None):
    users = User.objects.filter(Q(username__iexact=email) | Q(email__iexact=email))

    if exclude:
        users = users.exclude(pk=exclude)

    return users.exists()


def checkPass(form, data):
    if data.get("password"):
        user = User(
            username=data.get("email", ""),
            email=data.get("email", ""),
            first_name=data.get("fullName", ""),
        )

        try:
            validate_password(data["password"], user=user)
        except forms.ValidationError as error:
            form.add_error("password", error)


def knownModel(form, data, notSure):
    if notSure and form.address:
        allowed = FLAT_RULES[data["flatType"]]["models"]

        for kind in form.address["flatTypes"]:
            if kind["type"] == data.get("flatType"):
                for name in kind["models"]:
                    if name in allowed:
                        data["flatModel"] = name

                        return


class AddressChecksMixin:
    address_fields = {}

    def check_address(self, data):
        from . import address

        self.address = None
        self.warnings = {}
        names = self.address_fields
        postal = data.get(names["postal"])

        if postal:
            given = {}

            for check, field in names.items():
                if check != "postal" and field not in self.errors:
                    given[check] = data.get(field)

            if "block" in names and names["block"] not in self.errors:
                given["block"] = data.get(names["block"]) or ""

            record, errors, warns, fixes = address.check(postal, **given)
            self.address = record

            if fixes:
                self.data = self.data.copy()

            for check, value in fixes.items():
                if names[check] in self.fields:
                    data[names[check]] = value
                    self.data[names[check]] = value
                else:
                    self.add_error(
                        None,
                        "The listing address is incomplete. Ask the seller to correct it before editing.",
                    )

            for check, message in errors.items():
                self.add_error(names[check] if names[check] in self.fields else None, message)

            for check, message in warns.items():
                if names[check] in self.fields and names[check] not in self.errors:
                    self.warnings[names[check]] = message
                    self.fields[names[check]].warning = message

        if (
            not data.get(names["town"])
            and names["town"] not in self.errors
            and names["postal"] not in self.errors
        ):
            self.add_error(
                names["town"] if names["town"] in self.fields else None,
                ("Pick your town, or enter your postal code and we'll fill it in."),
            )

        if "block" in names and not data.get(names["block"]):
            if names["block"] not in self.errors and names["postal"] not in self.errors:
                self.add_error(
                    names["block"] if names["block"] in self.fields else None,
                    "Enter the block number, like 123 or 123A.",
                )

        return data


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


def bad_range(required, low, high, words):
    between = f"Enter {words} from {low} to {high}."

    return {
        "required": required,
        "invalid": required or between,
        "min_value": between,
        "max_value": between,
    }


class ValuationForm(ErrorAttrsMixin, AddressChecksMixin, forms.Form):
    postal = forms.RegexField(
        r"^[0-9]{6}$",
        required=False,
        label="Postal code",
        error_messages={"invalid": "Enter a 6-digit postal code."},
        widget=forms.TextInput(
            attrs={
                "pattern": "[0-9]{3} ?[0-9]{3}",
                "inputmode": "numeric",
                "maxlength": "7",
                "autocomplete": "postal-code",
                "placeholder": "e.g. 560201",
                "data-address": True,
            }
        ),
    )
    town = forms.ChoiceField(
        required=False, error_messages={"invalid_choice": "Choose a town from the list."}
    )
    flatType = forms.ChoiceField(
        label="Flat type",
        widget=forms.RadioSelect,
        error_messages={
            "required": "Choose a flat type, like 4-room.",
            "invalid_choice": "Choose a flat type from the list.",
        },
    )
    flatModel = forms.ChoiceField(
        label="Flat model",
        required=False,
        help_text="It’s on your HDB lease papers. Not sure? You can skip this.",
        error_messages={"invalid_choice": "Choose a flat model from the list, or pick Not sure."},
    )
    area = forms.FloatField(
        label="Size (sqm)",
        help_text="Not sure? A 4-room is usually about 90–95 sqm.",
        min_value=25,
        max_value=300,
        error_messages=bad_range(
            "Enter your flat’s size in sqm, like 92.", 25, "300 sqm", "a size"
        ),
        widget=forms.NumberInput(attrs={"inputmode": "decimal", "placeholder": "e.g. 92"}),
    )
    storey = forms.IntegerField(
        label="Floor level",
        min_value=1,
        max_value=50,
        error_messages=bad_range("Enter your floor level, from 1 to 50.", 1, 50, "a floor level"),
        widget=forms.NumberInput(attrs={"inputmode": "numeric", "placeholder": "e.g. 8"}),
    )
    lease = forms.IntegerField(
        label="Lease start year",
        help_text="The year the block was completed. It’s on your HDB lease papers.",
        min_value=LEASE_MIN,
        error_messages={
            "required": "Enter the year your lease started, like 1988.",
            "invalid": "Enter the year your lease started, like 1988.",
            "min_value": f"Enter a year from {LEASE_MIN} onwards.",
        },
        widget=forms.NumberInput(attrs={"inputmode": "numeric", "placeholder": "e.g. 1988"}),
    )
    ask = forms.IntegerField(
        label="Price to compare (optional)",
        help_text="An asking price, or the price you have in mind.",
        required=False,
        min_value=50000,
        max_value=3000000,
        error_messages=bad_range("", "$50,000", "$3,000,000", "a price"),
        widget=forms.NumberInput(
            attrs={"inputmode": "numeric", "autocomplete": "off", "placeholder": "e.g. 650000"}
        ),
    )
    address_fields = {
        "postal": "postal",
        "town": "town",
        "flatType": "flatType",
        "area": "area",
        "lease": "lease",
        "storey": "storey",
    }
    field_order = ["postal", "town", "flatType", "area", "storey", "lease", "flatModel", "ask"]
    aliases = {
        "floorAreaSqm": "area",
        "askingPrice": "ask",
        "asking": "ask",
        "leaseCommenceYear": "lease",
    }

    def __init__(self, *args, **kwargs):
        from .valuation import TOWNS, FLAT_MODELS

        super().__init__(*args, **kwargs)

        if self.is_bound:
            self.data = self.data.copy()

            for old, new in self.aliases.items():
                if old in self.data and new not in self.data:
                    self.data[new] = self.data[old]

            if "postal" in self.data:
                self.data["postal"] = self.data["postal"].replace(" ", "")

        self.fields["town"].choices = [("", "Choose…")] + [(town, town.title()) for town in TOWNS]
        self.fields["flatType"].choices = [(kind, flat_label(kind)) for kind in FLAT_TYPES]
        self.fields["flatModel"].choices = [("", "Not sure")] + [
            (model, model) for model in FLAT_MODELS
        ]
        yearNow = timezone.localdate().year
        self.fields["lease"].widget.attrs.update({"min": LEASE_MIN, "max": yearNow})
        self.fields["lease"].validators.append(MaxValueValidator(yearNow))
        self.fields["lease"].error_messages["max_value"] = f"Enter {yearNow} or earlier."

        for name in ("area", "lease", "ask"):
            self.fields[name].widget.attrs["aria-describedby"] = f"id_{name}_hint"

    def clean(self):
        data = super().clean()
        notSure = not data.get("flatModel") and data.get("flatType") in FLAT_RULES

        if notSure:
            data["flatModel"] = FLAT_RULES[data["flatType"]]["models"][0]

        data = self.check_address(data)
        knownModel(self, data, notSure)

        return data
