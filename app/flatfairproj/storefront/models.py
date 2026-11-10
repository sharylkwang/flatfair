import os
import uuid
from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.db import models, transaction, connection
from django.db.models import Q, Case, When, Value, Count, Prefetch, F
from django.db.models.functions import Floor, Round
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from functools import wraps
from uuid import UUID
from django.contrib import messages
from django.shortcuts import redirect, render

FLAT_RULES = {
    "2 ROOM": {
        "sqm": (32, 68),
        "models": [
            "Model A",
            "Standard",
            "Improved",
            "2-room",
            "Premium Apartment",
            "DBSS",
        ],
    },
    "3 ROOM": {
        "sqm": (48, 109),
        "models": [
            "New Generation",
            "Model A",
            "Improved",
            "Standard",
            "Simplified",
            "Premium Apartment",
            "DBSS",
            "Terrace",
        ],
    },
    "4 ROOM": {
        "sqm": (66, 125),
        "models": [
            "Model A",
            "Premium Apartment",
            "New Generation",
            "Simplified",
            "Model A2",
            "Improved",
            "DBSS",
            "Type S1",
            "Premium Apartment Loft",
            "Standard",
            "Adjoined flat",
            "Terrace",
        ],
    },
    "5 ROOM": {
        "sqm": (94, 158),
        "models": [
            "Improved",
            "Premium Apartment",
            "Model A",
            "Standard",
            "DBSS",
            "Model A-Maisonette",
            "Adjoined flat",
            "Type S2",
            "3Gen",
            "Improved-Maisonette",
            "Premium Apartment Loft",
        ],
    },
    "EXECUTIVE": {
        "sqm": (117, 202),
        "models": [
            "Apartment",
            "Maisonette",
            "Premium Apartment",
            "Adjoined flat",
            "Premium Maisonette",
        ],
    },
}

REQUIRED_ROOMS = [
    ("living_room", "Living room", None),
    ("kitchen", "Kitchen", None),
    ("master_bedroom", "Master bedroom", ("bed", 1)),
    ("bedroom_2", "Bedroom 2", ("bed", 2)),
    ("bedroom_3", "Bedroom 3", ("bed", 3)),
    ("bedroom_4", "Bedroom 4", ("bed", 4)),
    ("bedroom_5", "Bedroom 5", ("bed", 5)),
    ("common_toilet", "Common toilet", ("bath", 1)),
    ("master_toilet", "Master bedroom toilet", ("bath", 2)),
    ("common_toilet_2", "Common toilet 2", ("bath", 3)),
    ("common_toilet_3", "Common toilet 3", ("bath", 4)),
]

OPTIONAL_ROOMS = [
    ("balcony", "Balcony", ("balcony", 1)),
    ("service_yard", "Service yard", None),
    ("view", "View from window", None),
    ("other", "Anything else", None),
]

ROOM_CHOICES = [(key, label) for key, label, _ in REQUIRED_ROOMS + OPTIONAL_ROOMS]
ROOM_ORDER = {key: i for i, (key, _) in enumerate(ROOM_CHOICES)}
ROOM_LABELS = dict(ROOM_CHOICES)


def flatIssues(flatType, flatModel, area):
    from storefront.templatetags.estate_tags import flat_label

    rule = FLAT_RULES.get(flatType)

    if rule is None:
        return {"flatType": "Pick a flat type from the list."}

    issues = {}
    kind = flat_label(flatType)

    if flatModel not in rule["models"]:
        issues["flatModel"] = (
            f"{kind} flats don’t come in {flatModel}. Pick a flat model from the list."
        )

    if area is not None and not 25 <= area <= 300:
        issues["area"] = "Enter a size from 25 to 300 sqm."

    return issues


def sizeHint(flatType, area):
    from storefront.templatetags.estate_tags import flat_label

    rule = FLAT_RULES.get(flatType)

    if rule is None or area is None:
        return ""

    low_area, top_area = rule["sqm"]

    if not low_area <= area <= top_area:
        return f"{flat_label(flatType)} flats are usually {low_area} to {top_area} sqm. Check your HDB papers or change the flat type."

    return ""


def sizeHints():
    from storefront.templatetags.estate_tags import flat_label

    hints = {}

    for flatType, rule in FLAT_RULES.items():
        hints[flatType] = {
            "label": flat_label(flatType),
            "lo": rule["sqm"][0],
            "hi": rule["sqm"][1],
        }

    return hints


def counts(bedrooms, bathrooms, hasBalcony):
    return {"bed": bedrooms or 0, "bath": bathrooms or 0, "balcony": 1 if hasBalcony else 0}


def applies(needs, have):
    return needs is None or have[needs[0]] >= needs[1]


def needRooms(have):
    rooms = []

    for key, label, needs in REQUIRED_ROOMS:
        if applies(needs, have):
            rooms.append(key)

    return rooms


def extraRooms(have):
    rooms = []

    for key, label, needs in OPTIONAL_ROOMS:
        if applies(needs, have):
            rooms.append(key)

    return rooms


def hasRole(user, role):
    if not user.is_authenticated or not user.is_active or user.is_staff:
        return False

    profile = getattr(user, "profile", None)

    return bool(profile and {"buyer": profile.canBuy, "seller": profile.canSell}.get(role, False))


def currentMode(request):
    mode = getattr(request, "session", {}).get("marketplaceMode")

    if hasRole(request.user, mode):
        return mode

    profile = getattr(request.user, "profile", None)

    if profile and hasRole(request.user, profile.role):
        return profile.role

    return next((role for role in ("buyer", "seller") if hasRole(request.user, role)), None)


def modeContext(request):
    mode = currentMode(request)

    return {
        "currentMode": mode,
        "headerMode": "staff" if request.user.is_staff else mode,
        "canBuy": hasRole(request.user, "buyer"),
        "canSell": hasRole(request.user, "seller"),
    }


def notAllowed(request, role):
    return render(request, "storefront/forbidden.html", {"role": role}, status=403)


def needEmail(view):
    @wraps(view)
    def checked(request, *args, **kwargs):
        from accounts.verification import emailVerified
        from accounts.verification import saveBack

        if (
            request.user.is_authenticated
            and not request.user.is_staff
            and not emailVerified(request.user)
        ):
            saveBack(request)
            messages.info(request, "Verify your email before buying or selling.")

            return redirect("email_check")

        return view(request, *args, **kwargs)

    return checked


def object_id(value):
    value = str(value or "")

    if not value.isascii() or not value.isdecimal() or len(value) > 18 or int(value) < 1:
        raise ValidationError("Choose a valid object ID.")

    return int(value)


def listing_id(value):
    try:
        return UUID(str(value))
    except Exception as error:
        raise ValidationError("Choose a valid object ID.")


def lock_user(pk):
    if connection.vendor == "sqlite":
        User.objects.filter(pk=pk).update(last_login=F("last_login"))

    return User.objects.select_for_update().filter(pk=pk).first()


PAUSE_REASONS = [
    ("travel", "I’m travelling"),
    ("renovate", "I’m doing some renovation"),
    ("break", "I need a break from viewings"),
    ("other", "Something else"),
]

WITHDRAW_REASONS = [
    ("agent", "I sold it through an agent"),
    ("known", "I sold it to someone I know"),
    ("mind", "I changed my mind"),
    ("stopped", "I’m not selling any more"),
    ("other", "Something else"),
]

ACTIVE_BOOKINGS = ("requested", "confirmed")
RATE_PACKAGE_AFTER_DAYS = 7


def roomOrder():
    roomCases = [When(room=room, then=Value(index)) for room, index in ROOM_ORDER.items()]

    return Case(*roomCases, default=Value(len(ROOM_ORDER)))


def picsByRoom(lookup="photos"):
    photos = ListingPhoto.objects.defer("embedding").order_by(roomOrder(), "pk")

    return Prefetch(lookup, queryset=photos, to_attr="roomPhotos")


def withUsed(slots):
    return slots.annotate(
        usedCount=Count("bookings", filter=Q(bookings__status__in=ACTIVE_BOOKINGS))
    )


def publicQ(prefix=""):
    live = Q(**{prefix + "status": "live"})
    soldLive = Q(**{prefix + "status": "sold", prefix + "statusBeforeSold": "live"})
    sellerOn = Q(**{prefix + "seller__is_active": True})

    return (live | soldLive) & sellerOn


LISTING_DETAILS = {
    "town",
    "block",
    "postalCode",
    "flatType",
    "flatModel",
    "floorAreaSqm",
    "storey",
    "leaseCommenceYear",
    "bedrooms",
    "bathrooms",
    "hasBalcony",
    "askingPrice",
    "description",
    "intentToSellDate",
}


class ResourceCacheEntry(models.Model):
    cacheKey = models.CharField(max_length=255, primary_key=True, db_column="cache_key")
    value = models.TextField()
    expires = models.DateTimeField(db_index=True)


class ListingQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if LISTING_DETAILS.intersection(kwargs):
            kwargs["revision"] = F("revision") + 1

        return super().update(**kwargs)

    def public(self):
        return self.filter(publicQ())


class Listing(models.Model):
    objects = ListingQuerySet.as_manager()
    seedKey = models.CharField(max_length=100, unique=True, null=True, blank=True)
    publicId = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    revision = models.PositiveIntegerField(default=0, editable=False)
    publicationToken = models.CharField(max_length=64, unique=True, null=True, blank=True)

    STATUS_CHOICES = [
        ("live", "Live"),
        ("review", "In review"),
        ("sold", "Sold"),
        ("rejected", "Rejected"),
        ("hidden", "Hidden"),
        ("paused", "Paused"),
        ("withdrawn", "Deleted by seller"),
    ]

    seller = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="listings", null=True, blank=True
    )
    town = models.CharField(max_length=30)
    block = models.CharField(max_length=5, default="")
    postalCode = models.CharField(max_length=6, default="")
    street = models.CharField(max_length=60, blank=True)
    flatType = models.CharField(max_length=10)
    flatModel = models.CharField(max_length=30, default="Model A")
    floorAreaSqm = models.FloatField()
    storey = models.IntegerField(default=8)
    leaseCommenceYear = models.IntegerField(default=1995)
    bedrooms = models.PositiveSmallIntegerField()
    bathrooms = models.PositiveSmallIntegerField()
    hasBalcony = models.BooleanField()
    askingPrice = models.IntegerField()
    description = models.TextField(max_length=1000, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="live")
    statusBeforeSold = models.CharField(max_length=10, blank=True)
    estimatedPrice = models.IntegerField(null=True, blank=True)
    bandLow = models.IntegerField(null=True, blank=True)
    bandHigh = models.IntegerField(null=True, blank=True)
    autoDeclineLow = models.BooleanField(default=False)
    nextStepsSeenAt = models.DateTimeField(null=True, blank=True)
    modelVersion = models.CharField(max_length=10, blank=True)
    createdAt = models.DateTimeField(default=timezone.now)
    reviewSince = models.DateTimeField(null=True, blank=True)
    viewCount = models.PositiveIntegerField(default=0)
    intentToSellDate = models.DateField(null=True, blank=True)
    pausedAt = models.DateTimeField(null=True, blank=True)
    pauseReason = models.CharField(max_length=12, choices=PAUSE_REASONS, blank=True)
    withdrawnAt = models.DateTimeField(null=True, blank=True)
    withdrawReason = models.CharField(max_length=12, choices=WITHDRAW_REASONS, blank=True)
    withdrawNote = models.CharField(max_length=200, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "-createdAt", "-id"], name="listing_status_created"),
            models.Index(
                fields=["status", "town", "askingPrice"], name="listing_status_town_price"
            ),
            models.Index(
                fields=["status", "flatType", "askingPrice"], name="listing_status_type_price"
            ),
            models.Index(fields=["status", "askingPrice"], name="listing_status_price"),
            models.Index(fields=["town"], name="listing_town"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(floorAreaSqm__gt=0), name="listing_positive_area"),
            models.CheckConstraint(condition=Q(askingPrice__gt=0), name="listing_positive_price"),
            models.UniqueConstraint(
                fields=["seller"],
                condition=Q(status__in=["live", "review", "paused"]),
                name="one_open_listing_per_seller",
            ),
        ]

    def save(self, *args, **kwargs):
        fields = kwargs.get("update_fields")
        watches = fields is None or bool({"status", "askingPrice"}.intersection(fields))
        old = None

        if watches and not self._state.adding:
            old = type(self).objects.filter(pk=self.pk).values("status", "askingPrice").first()

        if self.status != "review":
            self.reviewSince = None
        elif self.reviewSince is None:
            self.reviewSince = timezone.now()

        bump = not self._state.adding and (
            fields is None or bool(LISTING_DETAILS.intersection(fields))
        )

        if bump:
            self.revision = F("revision") + 1

            if fields is not None:
                fields = [*fields, "revision"]
                kwargs["update_fields"] = fields

        if fields is not None and "status" in fields and "reviewSince" not in fields:
            kwargs["update_fields"] = [*fields, "reviewSince"]

        super().save(*args, **kwargs)

        if bump:
            self.refresh_from_db(fields=["revision"])

        if watches:
            from .search import huntAlerts

            kinds = []

            if (
                self.status == "live"
                and (old is None or old["status"] != "live")
                and (fields is None or "status" in fields)
            ):
                kinds.append("new_match")

            if (
                old
                and self.askingPrice < old["askingPrice"]
                and (fields is None or "askingPrice" in fields)
            ):
                kinds.append("price_drop")

            if kinds:
                transaction.on_commit(lambda listingId=self.pk: huntAlerts(listingId, kinds))

            saved = []

            if "price_drop" in kinds:
                saved.append("saved_price_drop")

            if old and old["status"] != "sold" and self.status == "sold":
                saved.append("saved_sold")

            if saved:
                from .notices import fanAlerts

                transaction.on_commit(lambda listingId=self.pk: fanAlerts(listingId, saved))

    @property
    def underOffer(self):
        if self.status == "sold":
            return False

        if hasattr(self, "acceptedCount"):
            return self.acceptedCount > 0

        return self.offers.filter(status="accepted").exists()

    def intentToSellValid(self):
        if not self.intentToSellDate:
            return False

        return self.intentToSellDate >= timezone.localdate() - timedelta(days=365)

    def pricePerSqm(self):
        return round(self.askingPrice / self.floorAreaSqm)

    def floorAreaSqft(self):
        return round(self.floorAreaSqm * (10.764))

    def remainingLease(self):
        return 99 - (timezone.localdate().year - self.leaseCommenceYear)

    def orderedPhotos(self):
        if hasattr(self, "roomPhotos"):
            return self.roomPhotos

        return list(self.photos.defer("embedding").order_by(roomOrder(), "pk"))

    def photoTags(self):
        tags = []

        for photo in self.orderedPhotos():
            for tag in photo.tags:
                if tag not in tags:
                    tags.append(tag)

        return tags

    def coverPhoto(self):
        photos = self.orderedPhotos()

        for photo in photos:
            if photo.room not in ("view", "other"):
                return photo

        if photos:
            return photos[0]

        return None


def photoPath(photo, filename):
    ending = os.path.splitext(filename)[1].lower()

    return f"listings/{photo.listing.publicId}/{photo.room}{ending}"


class ListingPhotoQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if {"image", "room", "listing", "listing_id"}.intersection(kwargs):
            kwargs.update(staffReviewedBy=None, staffReviewedAt=None, staffReviewedRevision=None)

        kwargs.setdefault("updatedAt", timezone.now())

        return super().update(**kwargs)


class ListingPhoto(models.Model):
    objects = ListingPhotoQuerySet.as_manager()
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="photos")
    room = models.CharField(max_length=20, choices=ROOM_CHOICES)
    image = models.ImageField(upload_to=photoPath)
    createdAt = models.DateTimeField(default=timezone.now)
    updatedAt = models.DateTimeField(default=timezone.now)
    aiLabel = models.CharField(max_length=20, blank=True)
    aiIrrelevant = models.FloatField(null=True, blank=True)
    aiGenerated = models.FloatField(null=True, blank=True)
    photoReview = models.JSONField(default=list, blank=True)
    staffChecked = models.BooleanField(default=False)
    staffReviewedBy = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="manuallyReviewedPhotos",
    )
    staffReviewedAt = models.DateTimeField(null=True, blank=True)
    staffReviewedRevision = models.PositiveIntegerField(null=True, blank=True)
    tags = models.JSONField(default=list, blank=True)
    embedding = models.JSONField(null=True, blank=True)
    modelVersion = models.CharField(max_length=64, null=True, blank=True)

    def save(self, *args, **kwargs):
        fields = kwargs.get("update_fields")

        if not self._state.adding and (
            fields is None or {"image", "room", "listing", "listing_id"}.intersection(fields)
        ):
            old = (
                type(self)
                .objects.filter(pk=self.pk)
                .values_list("image", "room", "listing_id")
                .first()
            )

            if old != (self.image.name, self.room, self.listing_id):
                self.staffReviewedBy = None
                self.staffReviewedAt = None
                self.staffReviewedRevision = None

                if fields is not None:
                    kwargs["update_fields"] = set(fields) | {
                        "staffReviewedBy",
                        "staffReviewedAt",
                        "staffReviewedRevision",
                    }

        self.updatedAt = timezone.now()

        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"updatedAt"}

        return super().save(*args, **kwargs)

    @property
    def publicUrl(self):
        return reverse("listing_photo", args=[self.listing.publicId, self.room])

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["listing", "room"], name="one_photo_per_room")
        ]


class Profile(models.Model):
    ROLE_CHOICES = [("buyer", "Buyer"), ("seller", "Seller")]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default="buyer")
    canBuy = models.BooleanField(default=False)
    canSell = models.BooleanField(default=False)
    reactivatedAt = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if self._state.adding and not self.user.is_staff:
            self.canBuy = self.canBuy or self.role in ("buyer", "seller")
            self.canSell = self.canSell or self.role == "seller"

        super().save(*args, **kwargs)


def productImagePath(product, filename):
    return f"products/{uuid.uuid4().hex}.jpg"


class Product(models.Model):
    KIND_CHOICES = [("package", "Listing package"), ("addon", "Add-on service")]
    TIER_CHOICES = [
        (0, "No search boost"),
        (1, "Boost 30 days"),
        (2, "Boost 90 days"),
        (3, "Top spot"),
    ]

    name = models.CharField(max_length=40)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    tier = models.PositiveSmallIntegerField(choices=TIER_CHOICES, default=0)
    price = models.IntegerField()
    description = models.CharField(max_length=120, blank=True)
    durationDays = models.PositiveIntegerField(null=True, blank=True)
    isActive = models.BooleanField(default=True)
    image = models.ImageField(upload_to=productImagePath, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(price__gte=0), name="product_nonnegative_price")
        ]

    def __str__(self):
        return self.name


class Payment(models.Model):
    METHOD_CHOICES = [("stripe", "Stripe (test)"), ("demo", "Demo card")]
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("paid", "Paid"),
        ("failed", "Failed"),
        ("refunded", "Refunded"),
        ("partly_refunded", "Partly refunded"),
    ]

    seller = models.ForeignKey(User, on_delete=models.PROTECT, related_name="payments")
    method = models.CharField(max_length=10, choices=METHOD_CHOICES)
    providerSessionId = models.CharField(max_length=255, blank=True, null=True, unique=True)
    paymentIntentId = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="sgd")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    cardBrand = models.CharField(max_length=30, blank=True)
    cardLast4 = models.CharField(max_length=4, blank=True)
    checkoutToken = models.CharField(max_length=64, unique=True)
    snapshot = models.JSONField(default=dict)
    needsAttention = models.BooleanField(default=False)
    createdAt = models.DateTimeField(default=timezone.now)
    paidAt = models.DateTimeField(null=True, blank=True)

    @property
    def reference(self):
        return f"FF-P{self.pk}"

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gte=0), name="payment_nonnegative_amount")
        ]


class Order(models.Model):
    STATUS_CHOICES = [
        ("paid", "Paid"),
        ("processing", "Processing"),
        ("completed", "Completed"),
        ("refunded", "Refunded"),
    ]

    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name="orders")
    payment = models.ForeignKey(
        Payment, on_delete=models.PROTECT, null=True, blank=True, related_name="orders"
    )
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="paid")
    checkoutToken = models.CharField(max_length=64, unique=True, null=True, blank=True)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    gst = models.DecimalField(max_digits=10, decimal_places=2)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    createdAt = models.DateTimeField(default=timezone.now)
    reminderSentAt = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(subtotal__gte=0), name="order_nonnegative_subtotal"),
            models.CheckConstraint(condition=Q(gst__gte=0), name="order_nonnegative_gst"),
            models.CheckConstraint(condition=Q(total__gte=0), name="order_nonnegative_total"),
            models.CheckConstraint(
                condition=Q(total=Round(F("subtotal") + F("gst"), 2)),
                name="order_total_matches",
            ),
        ]

    def canConfirmDelivery(self):
        items = [item for item in self.items.all() if item.kind == "addon"]

        return (
            self.status == "processing"
            and bool(items)
            and all(item.deliveredAt and (item.deliveryFile or item.deliveryNote) for item in items)
        )

    def statusIndex(self):
        if self.status == "refunded":
            return -1

        return ["paid", "processing", "completed"].index(self.status)

    def canRefund(self):
        if self.status != "paid":
            return False

        for item in self.items.all():
            if item.kind == "package":
                return False

        return True


class Refund(models.Model):
    STATUS_CHOICES = [("pending", "Pending"), ("succeeded", "Succeeded"), ("failed", "Failed")]
    order = models.ForeignKey(
        Order, on_delete=models.PROTECT, related_name="refunds", null=True, blank=True
    )
    payment = models.ForeignKey(
        Payment, on_delete=models.PROTECT, related_name="refunds", null=True, blank=True
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    providerRefundId = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending")
    createdAt = models.DateTimeField(default=timezone.now)
    staff = models.ForeignKey(User, on_delete=models.PROTECT, related_name="refunds")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gte=0), name="refund_nonnegative_amount"),
            models.UniqueConstraint(
                fields=["order"],
                condition=Q(status__in=["pending", "succeeded"]),
                name="one_open_or_successful_refund",
            ),
            models.CheckConstraint(
                condition=Q(order__isnull=False, payment__isnull=True)
                | Q(order__isnull=True, payment__isnull=False),
                name="refund_exactly_one_target",
            ),
            models.UniqueConstraint(
                fields=["payment"],
                condition=Q(order__isnull=True, status__in=["pending", "succeeded"]),
                name="one_unplaced_payment_refund",
            ),
        ]


def deliveryPath(item, filename):
    return f"deliveries/{item.order_id}/{os.path.basename(filename)}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.RESTRICT, related_name="orderItems")
    price = models.DecimalField(max_digits=10, decimal_places=2)
    name = models.CharField(max_length=40, blank=True)
    kind = models.CharField(max_length=10, choices=Product.KIND_CHOICES, blank=True)
    tier = models.PositiveSmallIntegerField(default=0)
    durationDays = models.PositiveIntegerField(null=True, blank=True)
    listing = models.ForeignKey(
        Listing, on_delete=models.SET_NULL, null=True, blank=True, related_name="orderItems"
    )
    deliveryNote = models.TextField(max_length=1000, blank=True)
    deliveryFile = models.FileField(upload_to=deliveryPath, blank=True)
    deliveredAt = models.DateTimeField(null=True, blank=True)
    downloadedAt = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["listing", "-tier", "order"], name="item_listing_tier_order")
        ]
        constraints = [
            models.CheckConstraint(condition=Q(price__gte=0), name="item_nonnegative_price")
        ]

    @property
    def deliveryFileName(self):
        return os.path.basename(self.deliveryFile.name)

    def canRate(self):
        if self.order.status != "completed":
            return False

        if self.kind != "package":
            return True

        if self.listing and self.listing.status == "sold":
            return True

        return timezone.now() - self.order.createdAt >= timedelta(days=RATE_PACKAGE_AFTER_DAYS)


class Review(models.Model):
    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reviews")
    orderItem = models.OneToOneField(OrderItem, on_delete=models.CASCADE, related_name="review")
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    comment = models.CharField(max_length=500, blank=True)
    hidden = models.BooleanField(default=False)
    createdAt = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(rating__gte=1, rating__lte=5), name="review_rating_one_to_five"
            )
        ]

    @property
    def firstName(self):
        name = self.seller.first_name.strip()

        if not name:
            return "A seller"

        if self.seller.last_name.strip():
            return name

        return name.split()[0]


class Collection(models.Model):
    seedKey = models.CharField(max_length=100, unique=True, null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="collections")
    name = models.CharField(max_length=40)
    createdAt = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "name"], name="collection_name_once")
        ]

    def __str__(self):
        return self.name


class FlatView(models.Model):
    buyer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="flatViews")
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="buyerViews")
    count = models.PositiveIntegerField(default=1)
    firstViewedAt = models.DateTimeField(default=timezone.now)
    lastViewedAt = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["buyer", "listing"], name="flat_view_once_per_buyer")
        ]


class SavedListing(models.Model):
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name="items")
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="savedBy")
    createdAt = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["collection", "listing"], name="save_once_per_collection"
            )
        ]


class SavedSearch(models.Model):
    buyer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="savedSearches")
    name = models.CharField(max_length=200)
    queryParams = models.JSONField(default=dict)
    createdAt = models.DateTimeField(default=timezone.now)
    lastNotifiedAt = models.DateTimeField(null=True, blank=True)
    isActive = models.BooleanField(default=True)

    def save(self, *args, **kwargs):

        from .search import searchName
        from .search import tidySearch

        self.queryParams = tidySearch(self.queryParams)
        self.name = searchName(self.queryParams)

        if not self.queryParams:
            raise ValidationError("Choose at least one filter before saving a search.")

        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"name", "queryParams"}

        with transaction.atomic():
            buyer = lock_user(self.buyer_id)

            if buyer is None or not hasRole(buyer, "buyer"):
                raise ValidationError("Only buyers can save searches.")

            if type(self).objects.filter(buyer=buyer).exclude(pk=self.pk).count() >= 5:
                raise ValidationError("You can save up to 5 searches. Delete one to save another.")

            super().save(*args, **kwargs)

    @property
    def searchUrl(self):
        from urllib.parse import urlencode

        return reverse("listings") + "?" + urlencode(self.queryParams)


class SearchAlert(models.Model):
    savedSearch = models.ForeignKey(SavedSearch, on_delete=models.CASCADE, related_name="alerts")
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="searchAlerts")
    kind = models.CharField(
        max_length=20, choices=[("new_match", "New match"), ("price_drop", "Price drop")]
    )
    askingPrice = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["savedSearch", "listing", "kind", "askingPrice"],
                name="search_alert_once_per_price",
            )
        ]


class Offer(models.Model):
    seedKey = models.CharField(max_length=100, unique=True, null=True, blank=True)
    STATUSES = [
        (status, status.title())
        for status in ("pending", "accepted", "declined", "withdrawn", "countered")
    ]
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="offers")
    buyer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="offers")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    message = models.CharField(max_length=300, blank=True)
    reply = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default="pending")
    counterAmount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    hasHfe = models.BooleanField(default=False)
    createdAt = models.DateTimeField(default=timezone.now)
    respondedAt = models.DateTimeField(null=True, blank=True)
    priceSteps = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="offer_positive_amount"),
            models.CheckConstraint(
                condition=Q(amount__lte=3000000, amount=Floor(F("amount"))),
                name="offer_whole_amount_limit",
            ),
            models.CheckConstraint(
                condition=Q(counterAmount__isnull=True)
                | (
                    Q(status__in=("countered", "accepted", "declined", "withdrawn"))
                    & Q(
                        counterAmount__gt=F("amount"),
                        counterAmount__lte=3000000,
                        counterAmount=Floor(F("counterAmount")),
                    )
                ),
                name="offer_valid_counter_history",
            ),
            models.CheckConstraint(
                condition=Q(status="pending") | Q(respondedAt__isnull=False),
                name="offer_response_timestamp",
            ),
            models.CheckConstraint(
                condition=~Q(status="countered")
                | (Q(counterAmount__isnull=False) & Q(counterAmount__gt=0)),
                name="countered_offer_positive_counter",
            ),
            models.UniqueConstraint(
                fields=["listing", "buyer"],
                condition=Q(status__in=("pending", "countered", "accepted")),
                name="one_active_offer_per_buyer_listing",
            ),
            models.UniqueConstraint(
                fields=["listing"],
                condition=Q(status="accepted"),
                name="one_accepted_offer_per_listing",
            ),
        ]

    def clean(self):
        if self.status == "countered" and self.counterAmount is None:
            raise ValidationError(
                {"counterAmount": "Enter a positive counter amount for a countered offer."}
            )

        costError = "Enter a positive amount between half the asking price and $3,000,000."

        for value in (self.amount, self.counterAmount):
            if value is None:
                continue

            if not value.is_finite() or value <= 0 or value > Decimal("3000000"):
                raise ValidationError(costError)

            if value != value.to_integral_value():
                raise ValidationError(costError)

            if not self.pk and value < Decimal(self.listing.askingPrice) / 2:
                raise ValidationError(costError)

        if self.counterAmount is not None:
            tooLow = self.counterAmount <= self.amount

            if tooLow or self.counterAmount > min(self.listing.askingPrice, 3000000):
                raise ValidationError(
                    "Counter must be higher than the buyer’s offer and no higher than the asking price."
                )

        if self.buyer_id == self.listing.seller_id:
            raise ValidationError("You cannot offer on your own listing.")

    @property
    def askingPercent(self):
        return round(float(self.amount) / self.listing.askingPrice * 100, 1)


class ViewingSlot(models.Model):
    seedKey = models.CharField(max_length=100, unique=True, null=True, blank=True)
    removed = models.BooleanField(default=False)
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="viewingSlots")
    startsAt = models.DateTimeField()
    capacity = models.PositiveSmallIntegerField(default=2)
    reminderSentAt = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(capacity__gte=1), name="viewing_positive_capacity"),
            models.UniqueConstraint(
                fields=["listing", "startsAt"], name="one_slot_per_listing_time"
            ),
        ]

    def clean(self):
        if self.startsAt and timezone.is_naive(self.startsAt):
            raise ValidationError("Viewing times must include a timezone.")

        if self.capacity is not None and self.capacity < 1:
            raise ValidationError("Capacity must be at least one.")

    @property
    def used(self):
        if hasattr(self, "usedCount"):
            return self.usedCount

        return self.bookings.filter(status__in=ACTIVE_BOOKINGS).count()

    @property
    def capacityDots(self):
        return "●" * min(self.used, self.capacity) + "○" * max(0, self.capacity - self.used)

    @property
    def placesLeft(self):
        return max(0, self.capacity - self.used)


class ViewingBooking(models.Model):
    seedKey = models.CharField(max_length=100, unique=True, null=True, blank=True)
    slot = models.ForeignKey(ViewingSlot, on_delete=models.CASCADE, related_name="bookings")
    buyer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="viewingBookings")
    status = models.CharField(
        max_length=10,
        choices=[
            (status, status.title()) for status in ("requested", "confirmed", "cancelled", "done")
        ],
        default="requested",
    )
    createdAt = models.DateTimeField(default=timezone.now)
    reminderSentAt = models.DateTimeField(null=True, blank=True)
    cancelledBy = models.CharField(max_length=6, blank=True)
    cancelReason = models.CharField(max_length=12, blank=True)
    cancelNote = models.CharField(max_length=200, blank=True)
    cancelledAt = models.DateTimeField(null=True, blank=True)
    cancelCounts = models.BooleanField(default=False)
    movedFromStatus = models.CharField(max_length=10, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["slot", "buyer"], name="one_booking_per_buyer_slot")
        ]

    @property
    def statusLabel(self):
        if self.status in ("confirmed", "requested") and self.slot.startsAt <= timezone.now():
            return "Done" if self.status == "confirmed" else "Not confirmed"

        return self.get_status_display()


class Sale(models.Model):
    month = models.DateField()
    town = models.CharField(max_length=30)
    flatType = models.CharField(max_length=20)
    block = models.CharField(max_length=10)
    street = models.CharField(max_length=60)
    storeyRange = models.CharField(max_length=12)
    floorAreaSqm = models.FloatField()
    flatModel = models.CharField(max_length=40)
    leaseCommenceYear = models.PositiveSmallIntegerField()
    price = models.PositiveIntegerField()

    class Meta:
        indexes = [models.Index(fields=["town", "flatType", "-month"])]


class OfferMessage(models.Model):
    offer = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name="messages")
    by = models.CharField(max_length=6)
    text = models.CharField(max_length=300)
    createdAt = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["createdAt", "pk"]


class PriceSuggestion(models.Model):
    STATUSES = [
        ("open", "Waiting"),
        ("accepted", "Price lowered"),
        ("kept", "Kept price"),
        ("expired", "No longer needed"),
    ]
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="priceSuggestions")
    price = models.IntegerField()
    oldPrice = models.IntegerField()
    estimate = models.IntegerField()
    views = models.PositiveIntegerField(default=0)
    saves = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=10, choices=STATUSES, default="open")
    createdAt = models.DateTimeField(default=timezone.now)
    closedAt = models.DateTimeField(null=True, blank=True)


class ViewingAsk(models.Model):
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="viewingAsks")
    buyer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="viewingAsks")
    createdAt = models.DateTimeField(default=timezone.now)
    answeredAt = models.DateTimeField(null=True, blank=True)


class Deal(models.Model):
    STATUSES = [("open", "In progress"), ("complete", "Complete"), ("cancelled", "Cancelled")]
    offer = models.OneToOneField(Offer, on_delete=models.CASCADE, related_name="deal")
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="deals")
    buyer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="buyerDeals")
    price = models.IntegerField()
    status = models.CharField(max_length=10, choices=STATUSES, default="open")
    createdAt = models.DateTimeField(default=timezone.now)
    closedAt = models.DateTimeField(null=True, blank=True)


class DealStep(models.Model):
    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, related_name="steps")
    key = models.CharField(max_length=20)
    position = models.PositiveSmallIntegerField()
    dueOn = models.DateField(null=True, blank=True)
    doneAt = models.DateTimeField(null=True, blank=True)
    doneBy = models.CharField(max_length=6, blank=True)
    note = models.CharField(max_length=200, blank=True)
    reminderSentOn = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["position"]
        constraints = [models.UniqueConstraint(fields=["deal", "key"], name="one_step_per_deal")]


class ActivityNotice(models.Model):
    KINDS = [
        ("offer_new", "New offer"),
        ("offer_countered", "New price"),
        ("offer_accepted", "Price agreed"),
        ("offer_declined", "Offer declined"),
        ("offer_declined_auto", "Offer below the minimum"),
        ("deal_step", "Deal step done"),
        ("price_coach", "Price suggestion"),
        ("offer_buyer_reply", "Buyer replied"),
        ("photo_refused", "Photo removed"),
        ("saved_price_drop", "Saved flat: lower price"),
        ("saved_under_offer", "Saved flat: under offer"),
        ("saved_sold", "Saved flat: sold"),
        ("offer_reopened", "Acceptance undone"),
        ("viewing_asked", "Viewing time asked for"),
        ("viewing_times_added", "New viewing times"),
        ("review_hidden", "Review hidden"),
        ("post_hidden", "Post hidden"),
        ("listing_edited", "Listing changed by our team"),
        ("deal_due", "Deal step due"),
        ("offer_withdrawn", "Offer withdrawn"),
        ("offer_reply", "Seller replied"),
        ("viewing_booked", "Viewing requested"),
        ("viewing_cancelled", "Viewing cancelled"),
        ("viewing_confirmed", "Viewing confirmed"),
        ("viewing_reminder", "Viewing reminder"),
        ("listing_taken_down", "Listing taken down"),
        ("listing_approved", "Listing approved"),
        ("listing_relisted", "Your flat is live again"),
        ("listing_rejected", "Listing not approved"),
        ("listing_documents", "Documents needed"),
        ("service_delivered", "Service done"),
        ("support_reply", "Support replied"),
        ("order_refunded", "Order refunded"),
        ("order_completing", "Order completing soon"),
        ("payment_update", "Payment update"),
        ("community_reply", "New reply"),
        ("search_match", "Search match"),
        ("search_price_drop", "Price dropped"),
        ("offer_suggested", "Buyer suggested a price"),
        ("viewing_moved", "Viewing moved"),
        ("flat_paused", "Flat paused"),
        ("flat_resumed", "Flat back on sale"),
        ("flat_withdrawn", "Flat taken off sale"),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="activity_notices")
    kind = models.CharField(max_length=20, choices=KINDS)
    text = models.CharField(max_length=200)
    url = models.CharField(max_length=200)
    read = models.BooleanField(default=False)
    createdAt = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=["user", "read"])]


