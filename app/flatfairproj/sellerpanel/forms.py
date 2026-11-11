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

POSTAL_HELP = "Type your 6-digit postal code, like 560123."
PRICE_ERROR = "Type a price between $50,000 and $3,000,000."
FLOOR_ERROR = "Type a floor from 1 to 50."
SIZE_ERROR = "Type a size from 25 to 300 sqm."
INTENT_DAYS = 365


def pick_field(label, choices, error, coerce=int):
    return forms.TypedChoiceField(
        coerce=coerce,
        label=label,
        choices=[("", "Choose…")] + choices,
        error_messages={"required": error, "invalid_choice": error},
    )


class ListingValidationMixin(AddressChecksMixin):
    address_fields = {
        "postal": "postalCode",
        "town": "town",
        "block": "block",
        "flatType": "flatType",
        "area": "floorAreaSqm",
        "lease": "leaseCommenceYear",
        "storey": "storey",
    }


    def clean(self):
        data = super().clean()

        for field in self.address_fields.values():
            if field not in self.fields:
                data[field] = getattr(self.instance, field)

        notSure = not data.get("flatModel") and data.get("flatType") in FLAT_RULES

        if notSure:
            data["flatModel"] = FLAT_RULES[data["flatType"]]["models"][0]

        if data.get("flatType") and data.get("flatModel") and data.get("floorAreaSqm") is not None:
            issues = flatIssues(data["flatType"], data["flatModel"], data["floorAreaSqm"])

            for field, message in issues.items():
                if field == "area":
                    field = "floorAreaSqm"


                self.add_error(field if field in self.fields else None, message)

        data = self.check_address(data)
        knownModel(self, data, notSure)


        return data


class ListingForm(ListingValidationMixin, forms.ModelForm):
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    town = forms.ChoiceField(
        required=False,
        choices=[("", "Choose…")] + [(town, town.title()) for town in TOWNS],
        error_messages={"invalid_choice": "Choose a town from the list."},
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


class PriceCheckForm(forms.Form):
    postalCode = forms.RegexField(regex=r"^\d{6}$", required=False)
    block = forms.RegexField(regex=r"^\d{1,4}[A-Za-z]?$", required=False)
    town = forms.ChoiceField(required=False, choices=[(town, town) for town in TOWNS])
    flatType = forms.ChoiceField(choices=[(flat_type, flat_type) for flat_type in FLAT_TYPES])
    flatModel = forms.ChoiceField(
        required=False, choices=[(flat_model, flat_model) for flat_model in FLAT_MODELS]
    )
    floorAreaSqm = forms.FloatField(min_value=25, max_value=300)
    storey = forms.IntegerField(min_value=1, max_value=50)
    leaseCommenceYear = forms.IntegerField(min_value=LEASE_MIN)
    askingPrice = forms.IntegerField(required=False, min_value=50000, max_value=3000000)

    def clean(self):
        data = super().clean()



        if not data.get("town") and data.get("postalCode"):
            record = get_postal(data["postalCode"])

            if record:
                data["town"] = record["town"]

        if not data.get("town"):
            raise forms.ValidationError("Town or postal code needed.")

        rule = FLAT_RULES.get(data.get("flatType"))

        if rule and data.get("flatModel") not in rule["models"]:
            data["flatModel"] = rule["models"][0]

        return data


RATING_ERROR = "Choose from 1 to 5 stars, then press Save rating."


class ReviewForm(forms.ModelForm):
    rating = forms.TypedChoiceField(
        coerce=int,
        choices=[(rating, rating) for rating in RATING_CHOICES],
        error_messages={"required": RATING_ERROR, "invalid_choice": RATING_ERROR},
    )

    class Meta:
        model = Review
        fields = ["rating", "comment"]
        error_messages = {
            "comment": {"max_length": "Please keep your comment to 500 characters or fewer."}
        }


DECLINED_CARDS = {
    "4000000000000002": "Your card was declined. This is a test card that is always declined."
}


def luhnValid(number):
    total = 0

    for index, digit in enumerate(reversed(number)):
        value = int(digit)

        if index % 2 == 1:
            value = value * 2

            if value > 9:
                value = value - 9

        total = total + value

    return total % 10 == 0


def cardBrand(number):
    card_brands = [
        ("American Express", r"^3[47]", (15,), 4),
        ("Visa", r"^4", (13, 16, 19), 3),
        (
            "Mastercard",
            r"^(5[1-5]|222[1-9]|22[3-9][0-9]|2[3-6][0-9]{2}|27[01][0-9]|2720)",
            (16,),
            3,
        ),
    ]



    for name, pattern, lengths, cvcSize in card_brands:
        if re.match(pattern, number) and len(number) in lengths:
            return name, cvcSize

    return None, 0


class CardForm(forms.Form):
    name = forms.CharField(
        max_length=80,
        label="Name on card",
        error_messages={"required": "Enter the name on the card."},
        widget=forms.TextInput(attrs={"autocomplete": "cc-name"}),
    )
    number = forms.CharField(
        max_length=23,
        label="Card number",
        error_messages={"required": "Enter the card number."},
        widget=forms.TextInput(
            attrs={
                "autocomplete": "cc-number",
                "inputmode": "numeric",
                "placeholder": "4242 4242 4242 4242",
            }
        ),
    )
    expiry = forms.CharField(
        max_length=7,
        label="Expiry (MM/YY)",
        error_messages={"required": "Enter the expiry date."},
        widget=forms.TextInput(
            attrs={"autocomplete": "cc-exp", "inputmode": "numeric", "placeholder": "MM/YY"}
        ),
    )
    cvc = forms.CharField(
        max_length=4,
        label="CVC",
        error_messages={"required": "Enter the CVC."},
        widget=forms.TextInput(
            attrs={"autocomplete": "cc-csc", "inputmode": "numeric", "placeholder": "123"}
        ),
    )

    def clean_number(self):
        number = re.sub(r"[ -]", "", self.cleaned_data["number"])

        if not re.fullmatch(r"[0-9]+", number):
            raise forms.ValidationError("Please enter the card number using digits only.")


        brand, cvcSize = cardBrand(number)

        if brand is None:
            raise forms.ValidationError(
                "We take Visa, Mastercard and American Express. Please check the number."
            )

        if not luhnValid(number):
            raise forms.ValidationError(
                "This card number doesn’t look right. Please check for a typo."
            )

        return number

    def clean_expiry(self):
        found = re.fullmatch(r"([0-9]{2}) ?/ ?([0-9]{2})", self.cleaned_data["expiry"].strip())

        if not found:
            raise forms.ValidationError("Write the expiry as MM/YY, for example 08/28.")

        month = int(found.group(1))
        year = 2000 + int(found.group(2))

        if month < 1 or month > 12:
            raise forms.ValidationError("The month should be from 01 to 12.")

        today = timezone.localdate()



        if (year, month) < (today.year, today.month):
            raise forms.ValidationError("This card has expired.")

        return f"{month:02d}/{year % 100:02d}"

    def clean(self):
        data = super().clean()
        number = data.get("number")

        if not number:
            return data

        brand, cvcSize = cardBrand(number)
        cvc = (data.get("cvc") or "").strip()

        if not re.fullmatch(r"[0-9]+", cvc) or len(cvc) != cvcSize:
            self.add_error("cvc", f"Enter the {cvcSize}-digit code on the card.")

            return data



        if number in DECLINED_CARDS:
            raise forms.ValidationError(DECLINED_CARDS[number])

        data["brand"] = brand
        data["last4"] = number[-4:]

        return data
