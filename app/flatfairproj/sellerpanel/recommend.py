import math
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from storefront.models import lock_user, Product, OrderItem
from django.utils import timezone
from storefront.listing_rules import OPEN_LISTING_STATUSES
from .models import PackageRecommendation
from pretrained import (
    FEATURE_NAMES, MODEL_VERSION, PACKAGE_TIERS, packTraits,
    ArtifactUnavailable, warnGone, file_cache,
)
from .plans import packsNow

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def loadPackageModel():
    try:
        import joblib

        bundle = joblib.load(settings.BASE_DIR / "ml" / "package_model.joblib")

        return check_pack(bundle)
    except (OSError, EOFError, ValueError) as error:
        warnGone(log, f"Package model unavailable: {error}")

        return None
    except Exception:
        log.exception("Package model loading failed")

        return None


def check_pack(artifact):
    try:
        if artifact["version"] != MODEL_VERSION or artifact["features"] != FEATURE_NAMES:
            raise ArtifactUnavailable("Incompatible package version or features")

        model = artifact["model"]
        encoded = model.named_steps["vectorizer"].get_feature_names_out().tolist()

        if encoded != artifact["encodedFeatures"]:
            raise ArtifactUnavailable("Incompatible encoded package features")

        if model.named_steps["classifier"].n_features_in_ != len(encoded):
            raise ArtifactUnavailable("Incompatible package feature count")

        if set(model.classes_) != set(PACKAGE_TIERS):
            raise ArtifactUnavailable("Incompatible package classes")

        if not all(name in encoded for name in FEATURE_NAMES if name not in ("town", "flatType")):
            raise ArtifactUnavailable("Missing package features")

        sales = artifact["townTransactions"]

        if not sales or not all(
            isinstance(town, str) and math.isfinite(count) and count >= 0
            for town, count in sales.items()
        ):
            raise ArtifactUnavailable("Invalid town transactions")

        if (
            not math.isfinite(artifact["competitionThreshold"])
            or artifact["competitionThreshold"] <= 0
        ):
            raise ArtifactUnavailable("Invalid competition threshold")

        return artifact
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ArtifactUnavailable("Invalid package model schema") from error


def makeTraits(listing, artifact, now=None):
    now = now or timezone.now()
    today = timezone.localtime(now)
    town = listing.town.strip().upper()

    if town not in artifact["townTransactions"]:
        raise ValueError("No transaction proxy is available for this town.")

    return packTraits(
        town,
        listing.flatType,
        listing.floorAreaSqm,
        listing.storey,
        listing.leaseCommenceYear,
        listing.askingPrice,
        listing.estimatedPrice,
        listing.bandLow,
        listing.bandHigh,
        (now - listing.createdAt).total_seconds() / 86400,
        today.year + (today.month - 1) / 12,
        artifact["townTransactions"][town],
    )


def recommendationReason(features, artifact):
    reasons = []

    if features["bandPosition"] > 1:
        reasons.append("your asking price is above the fair range")

    if features["townTransactions"] >= artifact["competitionThreshold"]:
        town = features["town"].title()
        reasons.append(f"{town} has a lot of resale activity")

    if features["daysListed"] >= 30:
        reasons.append("your flat has been listed for over a month")

    if features["leaseRemaining"] < 50:
        reasons.append("your flat has under 50 years of lease remaining")

    if not reasons:
        return "This package gives your flat priority when buyers choose Recommended."

    reason = " and ".join(reasons[:2])

    return (
        reason[0].upper()
        + reason[1:]
        + ". This package gives your flat priority when buyers choose Recommended."
    )


def bestPack(listing):
    if listing is None or listing.status not in OPEN_LISTING_STATUSES:
        return None

    try:
        bundle = loadPackageModel()

        if bundle is None or packsNow([listing]):
            return None

        traits = makeTraits(listing, bundle)
        label = bundle["model"].predict([traits])[0]
        tier = PACKAGE_TIERS.get(label, 0)

        if not tier:
            return None

        product = (
            Product.objects.filter(kind="package", tier=tier, isActive=True)
            .order_by("price", "pk")
            .first()
        )

        if product is None:
            return None

        return {
            "listing": listing,
            "product": product,
            "reason": recommendationReason(traits, bundle),
            "modelVersion": bundle["version"],
        }
    except Exception:
        log.exception("Package recommendation failed")

        return None


def showTip(listing):
    tip = bestPack(listing)

    if tip:
        now = timezone.now()

        start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)

        with transaction.atomic():
            lock_user(listing.seller_id)

            if not PackageRecommendation.objects.filter(
                listing=listing,
                product=tip["product"],
                modelVersion=tip["modelVersion"],
                createdAt__gte=start,
                createdAt__lt=start + timedelta(days=1),
            ).exists():
                PackageRecommendation.objects.create(**tip)

    return tip


def tipStats():
    now = timezone.now()
    buys = Q(converted=True, orderItem__isnull=False)

    stats = PackageRecommendation.objects.filter(
        createdAt__gte=now - timedelta(days=30), createdAt__lte=now
    ).aggregate(
        shown=Count("listing", distinct=True),
        sellers=Count("listing__seller", distinct=True),
        convertedListings=Count("listing", distinct=True, filter=buys),
        converted=Count("orderItem", distinct=True, filter=buys),
    )

    convertedListings = stats.pop("convertedListings")

    stats["conversionRate"] = (
        round(convertedListings / stats["shown"] * 100, 1) if stats["shown"] else 0
    )

    return stats


def markedPics(flat):
    from .photocheck import doubtQ

    return (
        flat.photos.filter(doubtQ())
        .exclude(aiIrrelevant__isnull=True)
        .exclude(aiGenerated__isnull=True)
    )


def picPitch(flat):
    if flat is None or flat.status not in ("live", "review"):
        return None

    product = Product.objects.filter(name=("Pro photography"), kind="addon", isActive=True).first()

    if product is None:
        return None

    if OrderItem.objects.filter(
        listing=flat, product=product, order__status__in=(("paid", "processing", "completed"))
    ).exists():
        return None

    photos = list(markedPics(flat))

    if not photos:
        return None

    rooms = []

    for photo in photos:
        rooms.append(photo.get_room_display().lower())

    return {"product": product, "count": len(photos), "rooms": rooms, "flat": flat}
