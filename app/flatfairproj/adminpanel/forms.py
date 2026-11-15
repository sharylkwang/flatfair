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

PACKAGE_TYPES = {
    "Boost 30 days": "Shows above free listings in search, with a Sponsored tag.",
    "Boost 90 days": "Shows above Boost 30 days flats in search, with a Sponsored tag.",
    "Top spot": "Shows first in search and on the home page, with a Sponsored tag.",
}
EMAIL_ATTRS = {
    "autocomplete": "email",
    "autocapitalize": "off",
    "spellcheck": "false",
}


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
            "description": {
                "max_length": "Keep the description to 120 characters or fewer."
            },
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

            self.fields["tier"].help_text = (
                "Packages only. It sets where the flat shows in search."
            )

            return

        del self.fields["durationDays"]

        del self.fields["tier"]


        self.fields["kind"].disabled = True
        self.fields["kind"].help_text = "The type can’t change once a product is made."

        if self.instance.kind == "package":
            self.packageType = tierOf(self.instance.tier)

            self.packageEffect = PACKAGE_TYPES.get(
                self.packageType, "Changes nothing in search."
            )

    def clean(self):
        data = super().clean()

        if not self.instance.pk and data.get("kind") != "package":
            data["durationDays"] = None
            data["tier"] = 0


        if (
            not self.instance.pk
            and data.get("kind") == "package"
            and not data.get("tier")
        ):
            self.add_error(
                "tier", "Choose where a flat with this package shows in search."
            )


        if data.get("kind") == "package":
            product = SimpleNamespace(
                kind="package",
                name=data.get("name", ""),
                tier=self.instance.tier if self.instance.pk else data.get("tier"),
                durationDays=(
                    self.instance.durationDays
                    if self.instance.pk
                    else data.get("durationDays")
                ),
            )



            problem = packIssue(product)

            if problem:
                self.add_error(None, problem)


        return data


class ListingDetailsForm(ListingValidationMixin, forms.ModelForm):
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["revision"] = self.instance.revision

    askingPrice = forms.IntegerField(
        label="Asking price ($)",
        min_value=50000,
        max_value=3000000,
        error_messages={
            "required": PRICE_ERROR,
            "invalid": PRICE_ERROR,
            "min_value": PRICE_ERROR,
            "max_value": PRICE_ERROR,
        },
        widget=forms.NumberInput(attrs={"inputmode": "numeric", "autocomplete": "off"}),
    )
    floorAreaSqm = forms.FloatField(
        label="Size (sqm)",
        min_value=25,
        max_value=300,
        error_messages={
            "required": SIZE_ERROR,
            "invalid": SIZE_ERROR,
            "min_value": SIZE_ERROR,
            "max_value": SIZE_ERROR,
        },
        widget=forms.NumberInput(attrs={"inputmode": "decimal"}),
    )
    storey = forms.IntegerField(
        label="Floor level",
        min_value=1,
        max_value=50,
        error_messages={
            "required": FLOOR_ERROR,
            "invalid": FLOOR_ERROR,
            "min_value": FLOOR_ERROR,
            "max_value": FLOOR_ERROR,
        },
        widget=forms.NumberInput(attrs={"inputmode": "numeric"}),
    )
    flatModel = forms.ChoiceField(
        label="Flat model",
        choices=[(flat_model, flat_model) for flat_model in FLAT_MODELS],
        error_messages={
            "required": "Choose a flat model.",
            "invalid_choice": "Choose a flat model from the list.",
        },
    )

    class Meta:
        model = Listing
        fields = ["askingPrice", "floorAreaSqm", "storey", "flatModel"]


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


class TicketForm(forms.ModelForm):
    order = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = Ticket
        fields = ["email", "topic", "message"]
        labels = {
            "email": "Your email",
            "topic": "What it is about",
            "message": "Your message",
        }
        widgets = {
            "email": forms.EmailInput(attrs=EMAIL_ATTRS),
            "message": forms.Textarea(
                attrs={
                    "rows": 4,
                    "maxlength": 2000,
                    "placeholder": "The flat’s block and town, what you expected, and what you saw",
                }
            ),
        }
        error_messages = {
            "email": {
                "required": "Enter your email so we can reply.",
                "invalid": EMAIL_INVALID,
                "max_length": "That email is too long. Please use a shorter one.",
            },
            "topic": {
                "required": "Choose what your message is about.",
                "invalid_choice": "Choose a topic from the list.",
            },
            "message": {
                "required": "Write your message.",
                "max_length": "Keep your message to 2,000 characters or fewer.",
            },
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)



        if "message" in self.data:
            self.data = self.data.copy()


            self.data["message"] = self.data["message"].replace("\r\n", "\n")

        self.user = None

        if user is not None and user.is_authenticated:
            self.user = user

        self.fields["topic"].choices = [("", "Choose a topic")] + Ticket.TOPICS

        if self.user:
            del self.fields["email"]
        else:
            del self.fields["order"]

    def linkedOrder(self):
        if "order" not in self.fields:
            return None

        try:
            orderId = object_id(self["order"].value())
        except Exception as error:
            return None

        return Order.objects.filter(pk=orderId, seller=self.user).first()

    def clean_order(self):
        return self.linkedOrder()

    def clean(self):
        data = super().clean()
        order = data.get("order")



        if data.get("topic") == "refund" and order and not order.canRefund():
            self.add_error(
                "topic",
                (
                    "This order can't be refunded. Packages start as soon as you pay, and a service can only be refunded before our team starts it."
                ),
            )

        return data

    def save(self, commit=True):
        ticket = super().save(commit=False)

        if self.user:
            ticket.customer = self.user
            ticket.email = self.user.email or self.user.username
            ticket.order = self.cleaned_data.get("order")

        if commit:
            ticket.save()

        return ticket


class ReplyForm(forms.Form):
    body = forms.CharField(
        label="Your reply",
        max_length=2000,
        error_messages={
            "required": "Write your reply first.",
            "max_length": "Keep your reply to 2,000 characters or fewer.",
        },
        widget=forms.Textarea(attrs={"rows": 4, "maxlength": 2000}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if "body" in self.data:
            self.data = self.data.copy()
            self.data["body"] = self.data["body"].replace("\r\n", "\n")
