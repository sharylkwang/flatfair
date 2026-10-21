from types import SimpleNamespace
from django import forms
from django.contrib.auth.models import User
from accounts.forms import EMAIL_INVALID, NormalizedEmailField, tidyEmail
from storefront.forms import checkPass, emailTaken
from storefront.models import object_id, Listing, Order, Product
from sellerpanel.stripepay import MAX_PRODUCT_PRICE
from sellerpanel.forms import (
    FLOOR_ERROR,
    PRICE_ERROR,
    SIZE_ERROR,
    ListingValidationMixin,
)
from sellerpanel.plans import packIssue
from storefront.presentation import tierOf
from storefront.valuation import FLAT_MODELS
from .models import Ticket

class ProductForm(forms.ModelForm):
    price = forms.IntegerField(
        label="Price ($, before GST)",
        min_value=0,
        max_value=MAX_PRODUCT_PRICE,
        error_messages={
            "required": "Enter the price in dollars, like 60.",
            "invalid": "Enter the price in dollars, like 60.",
            "min_value": "Enter a price of $0 or more.",
            "max_value": "That price is too high once GST is added. Enter a lower price.",
        },
        widget=forms.NumberInput(attrs={"inputmode": "numeric", "autocomplete": "off"}),
    )
    durationDays = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=3650,
        label="Days a package lasts (leave empty for “until sold”)",
        error_messages={
            "invalid": "Enter the number of days, like 30.",
            "min_value": "Enter 1 to 3650 days.",
            "max_value": "Enter 1 to 3650 days.",
        },
    )

    class Meta:
        model = Product
        fields = [
            "name",
            "kind",
            "tier",
            "price",
            "description",
            "durationDays",
            "isActive",
        ]
        labels = {
            "kind": "Type",
            "tier": "Place in search",
            "isActive": "Show in the shop",
        }
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}
        error_messages = {
            "name": {
                "required": "Enter a name, like Boost 30 days.",
                "max_length": "Keep the name to 40 characters or fewer.",
            },
            "kind": {
                "required": "Choose package or service.",
                "invalid_choice": "Choose package or service.",
            },
            "tier": {"invalid_choice": "Choose a place in search from the list."},
            "description": {"max_length": "Keep the description to 120 characters or fewer."},
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.packageType = ""
        self.packageEffect = ""
        self.fields["kind"].choices = [("", "Choose a type")] + Product.KIND_CHOICES

        if not self.instance.pk:
            self.fields["tier"].required = False

            self.fields["tier"].choices = [
                ("", "No change (add-on services)")
            ] + Product.TIER_CHOICES[1:]

            self.fields["tier"].help_text = "Packages only. It sets where the flat shows in search."

            return

        del self.fields["durationDays"]

        del self.fields["tier"]

        self.fields["kind"].disabled = True
        self.fields["kind"].help_text = "The type can’t change once a product is made."

        if self.instance.kind == "package":
            self.packageType = tierOf(self.instance.tier)

            self.packageEffect = PACKAGE_TYPES.get(self.packageType, "Changes nothing in search.")

    def clean(self):
        data = super().clean()

        if not self.instance.pk and data.get("kind") != "package":
            data["durationDays"] = None
            data["tier"] = 0

        if not self.instance.pk and data.get("kind") == "package" and not data.get("tier"):
            self.add_error("tier", "Choose where a flat with this package shows in search.")

        if data.get("kind") == "package":
            product = SimpleNamespace(
                kind="package",
                name=data.get("name", ""),
                tier=self.instance.tier if self.instance.pk else data.get("tier"),
                durationDays=(
                    self.instance.durationDays if self.instance.pk else data.get("durationDays")
                ),
            )

            problem = packIssue(product)

            if problem:
                self.add_error(None, problem)

        return data


class ContactForm(forms.ModelForm):
    email = NormalizedEmailField(
        required=True, max_length=150, widget=forms.EmailInput(attrs=EMAIL_ATTRS)
    )

    class Meta:
        model = User
        fields = ["first_name", "email"]
        labels = {"first_name": "Full name", "email": "Email"}
        widgets = {"first_name": forms.TextInput(attrs={"autocomplete": "name"})}
        error_messages = {
            "first_name": {
                "required": "Enter their full name.",
                "max_length": "Keep the name to 150 characters or fewer.",
            }
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["first_name"].required = True

    def clean_email(self):
        email = tidyEmail(self.cleaned_data["email"])

        if emailTaken(email, exclude=self.instance.pk):
            raise forms.ValidationError("Another account already uses this email.")

        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.username = user.email

        if commit:
            user.save()

        return user


class StaffForm(forms.Form):
    fullName = forms.CharField(
        max_length=150,
        label="Full name",
        error_messages={
            "required": "Enter their full name.",
            "max_length": "Keep the name to 150 characters or fewer.",
        },
        widget=forms.TextInput(attrs={"autocomplete": "name"}),
    )
    email = NormalizedEmailField(
        max_length=150, label="Work email", widget=forms.EmailInput(attrs=EMAIL_ATTRS)
    )
    password = forms.CharField(
        strip=False,
        min_length=8,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        label="Starting password",
        error_messages={
            "required": "Set a starting password of at least 8 characters.",
            "min_length": "Use at least 8 characters.",
        },
    )

    def clean_email(self):
        email = tidyEmail(self.cleaned_data["email"])

        if emailTaken(email):
            raise forms.ValidationError("An account with this email already exists.")

        return email

    def clean(self):
        data = super().clean()
        checkPass(self, data)

        return data


