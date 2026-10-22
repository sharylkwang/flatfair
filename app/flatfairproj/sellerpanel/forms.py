import re
from datetime import timedelta
from django import forms
from django.utils import timezone
from storefront.address import get_postal
from storefront.models import FLAT_RULES, flatIssues, Listing, Review
from storefront.forms import FLAT_TYPES, LEASE_MIN, MOP_YEARS, AddressChecksMixin, knownModel
from storefront.presentation import RATING_CHOICES
from storefront.templatetags.estate_tags import flat_label
from storefront.valuation import TOWNS, FLAT_MODELS

class ListingForm(ListingValidationMixin, forms.ModelForm):
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    town = forms.ChoiceField(
        required=False,
        choices=[("", "Choose…")] + [(town, town.title()) for town in TOWNS],
        error_messages={"invalid_choice": ("Choose a town from the list.")},
    )
    askingPrice = forms.IntegerField(
        label="Asking price ($)",
        min_value=50000,
        max_value=3000000,
        error_messages={
            "required": "Type your asking price, like 650000.",
            "invalid": PRICE_ERROR,
            "min_value": PRICE_ERROR,
            "max_value": PRICE_ERROR,
        },
        widget=forms.NumberInput(
            attrs={"inputmode": "numeric", "autocomplete": "off", "placeholder": "e.g. 650000"}
        ),
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
        widget=forms.NumberInput(attrs={"inputmode": "numeric", "placeholder": "e.g. 8"}),
    )
    floorAreaSqm = forms.FloatField(
        label="Size (sqm)",
        min_value=25,
        max_value=300,
        error_messages={
            "required": "Type the size in sqm, like 92. It’s on your HDB papers.",
            "invalid": SIZE_ERROR,
            "min_value": SIZE_ERROR,
            "max_value": SIZE_ERROR,
        },
        widget=forms.NumberInput(attrs={"inputmode": "decimal", "placeholder": "e.g. 92"}),
    )
    bedrooms = pick_field(
        "Bedrooms",
        [(bedroom_count, bedroom_count) for bedroom_count in range(1, 6)],
        "Choose how many bedrooms your flat has.",
    )
    bathrooms = pick_field(
        "Bathrooms",
        [(bathroom_count, bathroom_count) for bathroom_count in range(1, 5)],
        "Choose how many bathrooms your flat has.",
    )
    hasBalcony = pick_field(
        "Balcony",
        [("yes", "Has a balcony"), ("no", "No balcony")],
        "Choose whether your flat has a balcony.",
        coerce=lambda v: v == "yes",
    )
    intentToSellDate = forms.DateField(
        label="HDB Intent to Sell registered on",
        error_messages={
            "required": "Type the date you registered your Intent to Sell with HDB.",
            "invalid": "Type a date, like 1 Nov 2026.",
        },
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        help_text="Register on the HDB Resale Portal first. It lasts 12 months.",
    )
    ownsFlat = forms.BooleanField(
        label="I own this flat (or co-own it) and I’m allowed to sell it.",
        error_messages={"required": "Please tick this box to confirm you can sell this flat."},
    )

    class Meta:
        model = Listing
        fields = [
            "postalCode",
            "block",
            "town",
            "flatType",
            "flatModel",
            "floorAreaSqm",
            "bedrooms",
            "bathrooms",
            "hasBalcony",
            "storey",
            "leaseCommenceYear",
            "intentToSellDate",
            "askingPrice",
            "description",
        ]
        widgets = {
            "flatType": forms.Select(
                choices=[("", "Choose…")]
                + [(flat_type, flat_label(flat_type)) for flat_type in FLAT_TYPES]
            ),
            "flatModel": forms.Select(
                choices=[("", "Not sure")]
                + [(flat_model, flat_model) for flat_model in FLAT_MODELS]
            ),
            "block": forms.TextInput(
                attrs={
                    "placeholder": "e.g. 123A",
                    "required": True,
                    "autocomplete": "address-line1",
                }
            ),
            "postalCode": forms.TextInput(
                attrs={
                    "placeholder": "e.g. 560123",
                    "inputmode": "numeric",
                    "autocomplete": "postal-code",
                    "data-address": True,
                }
            ),
            "leaseCommenceYear": forms.NumberInput(
                attrs={"inputmode": "numeric", "placeholder": "e.g. 1988", "min": LEASE_MIN}
            ),
            "description": forms.Textarea(attrs={"rows": 4, "maxlength": 1000}),
        }
        labels = {
            "block": "Block",
            "postalCode": "Postal code",
            "flatType": "Flat type",
            "flatModel": "Flat model",
            "leaseCommenceYear": "Lease start year",
            "description": "About your flat (optional)",
        }
        help_texts = {
            "flatModel": "Not sure? Leave this as it is. It’s on your HDB lease papers.",
            "leaseCommenceYear": "The year your flat’s 99-year lease began. It’s on your HDB lease papers.",
            "description": "A few lines on renovation, views or what’s nearby.",
        }
        error_messages = {
            "flatType": {
                "required": "Choose your flat type, like 4-room.",
                "max_length": "Choose a flat type from the list.",
            },
            "flatModel": {"max_length": "Choose a flat model from the list, or pick Not sure."},
            "leaseCommenceYear": {
                "required": "Type the year your lease started, like 1995. It’s on your HDB lease papers.",
                "invalid": "Type the year your lease started, like 1995.",
            },
            "postalCode": {"required": POSTAL_HELP, "max_length": POSTAL_HELP},
            "block": {"max_length": "Type the block number, like 123A."},
            "description": {"max_length": "Please keep it to 1,000 characters or fewer."},
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.is_bound and " " in self.data.get("postalCode", ""):
            self.data = self.data.copy()
            self.data["postalCode"] = self.data["postalCode"].replace(" ", "")

        self.fields["postalCode"].widget.attrs["maxlength"] = "7"
        self.fields["block"].required = False
        self.fields["flatModel"].required = False
        self.fields["flatType"].widget.attrs.pop("maxlength", None)
        self.fields["flatModel"].widget.attrs.pop("maxlength", None)

        if not self.is_bound and not self.instance.pk:
            initial = kwargs.get("initial") or {}
            self.initial.update(
                {
                    "flatModel": initial.get("flatModel", ""),
                    "leaseCommenceYear": initial.get("leaseCommenceYear"),
                }
            )

        today = timezone.localdate()
        self.fields["intentToSellDate"].widget.attrs["max"] = today.isoformat()

        if not self.instance.intentToSellDate:
            self.fields["intentToSellDate"].widget.attrs["min"] = (
                today - timedelta(days=INTENT_DAYS)
            ).isoformat()

        if self.instance.pk:
            self.initial["revision"] = self.instance.revision

            del self.fields["ownsFlat"]
        else:
            del self.fields["revision"]

    def clean_block(self):
        block = self.cleaned_data["block"].strip().upper()

        if block and not re.fullmatch(r"\d{1,4}[A-Z]?", block):
            raise forms.ValidationError("Use the block number only, like 123 or 123A.")

        return block

    def clean_postalCode(self):
        code = self.cleaned_data["postalCode"].strip()

        if not re.fullmatch(r"\d{6}", code):
            raise forms.ValidationError(POSTAL_HELP)

        return code

    def clean_intentToSellDate(self):
        date = self.cleaned_data["intentToSellDate"]
        today = timezone.localdate()

        if date > today:
            raise forms.ValidationError("The date can’t be in the future.")

        if date == self.instance.intentToSellDate:
            return date

        if date < today - timedelta(days=INTENT_DAYS):
            raise forms.ValidationError(
                "An Intent to Sell lasts 12 months. Please register again with HDB, then type the new date."
            )

        return date

    def clean_leaseCommenceYear(self):
        year = self.cleaned_data["leaseCommenceYear"]
        yearNow = timezone.localdate().year

        if year < LEASE_MIN or year > yearNow:
            raise forms.ValidationError(
                f"Type a year from {LEASE_MIN} to {yearNow}. It’s on your HDB lease papers."
            )

        if year > yearNow - MOP_YEARS:
            raise forms.ValidationError(
                "HDB flats can only be sold after the 5-year minimum occupation period. "
                f"A flat from {year} can be listed from {year + MOP_YEARS}."
            )

        return year


