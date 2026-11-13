import math
from collections import Counter, defaultdict
from datetime import timedelta
from heapq import nsmallest

import numpy
from django.db.models import Prefetch
from django.utils import timezone
from sellerpanel.photocheck import MODEL_VERSION
from storefront.models import (
    currentMode, hasRole, FlatView, Listing,
    ListingPhoto, Offer, SavedListing, SavedSearch,
    ViewingBooking, picsByRoom,
)
from .lens import validEmbedding
from .presentation import decorate, hasDeal, withTier
from .search import tidySearch

VIEW_WEIGHT = 1
VIEW_CAP = 3
OLD_VIEW_DAYS = 30
OLD_VIEW_FACTOR = 0.5
SAVE_WEIGHT = 3
OFFER_WEIGHT = 5
BOOKING_WEIGHT = 4
SEARCH_WEIGHT = 2
TOWN_WEIGHT = 3
TYPE_WEIGHT = 2.5
PRICE_WEIGHT = 2
SIZE_WEIGHT = 1
STOREY_WEIGHT = 0.5
LEASE_WEIGHT = 0.75
PHOTO_WEIGHT = 2
FAIR_WEIGHT = 0.2
MIN_LOG_SPREAD = 0.2
RESULTS = 6
MAX_CANDIDATES = 500


def photo_mean(photos):
    vectors = [
        vector
        for photo in photos
        if photo.modelVersion == MODEL_VERSION
        and (vector := validEmbedding(photo.embedding)) is not None
    ]

    return validEmbedding(numpy.mean(vectors, axis=0)) if vectors else None


def tasteProfile(buyer, skipListing=None, now=None):
    now = now or timezone.now()
    weights = defaultdict(float)
    viewed = set()

    for row in FlatView.objects.filter(buyer=buyer).exclude(listing_id=skipListing):
        weight = min(row.count, VIEW_CAP) * VIEW_WEIGHT

        if row.lastViewedAt < now - timedelta(days=OLD_VIEW_DAYS):
            weight *= OLD_VIEW_FACTOR

        if weight:
            weights[row.listing_id] += weight
            viewed.add(row.listing_id)

    saved = set(
        SavedListing.objects.filter(collection__user=buyer)
        .exclude(listing_id=skipListing)
        .values_list("listing_id", flat=True)
    )

    offered = set(
        Offer.objects.filter(buyer=buyer)
        .exclude(listing_id=skipListing)
        .values_list("listing_id", flat=True)
    )

    booked = set(
        ViewingBooking.objects.filter(buyer=buyer)
        .exclude(status="cancelled")
        .exclude(slot__listing_id=skipListing)
        .values_list("slot__listing_id", flat=True)
    )

    for ids, weight in ((saved, SAVE_WEIGHT), (offered, OFFER_WEIGHT), (booked, BOOKING_WEIGHT)):
        for flatId in ids:
            weights[flatId] += weight

    photos = ListingPhoto.objects.filter(modelVersion=MODEL_VERSION, embedding__isnull=False)

    flats = list(
        Listing.objects.filter(pk__in=weights)
        .exclude(seller=buyer)
        .prefetch_related(Prefetch("photos", queryset=photos, to_attr="tastePhotos"))
    )

    searches = [
        tidySearch(row.queryParams)
        for row in SavedSearch.objects.filter(buyer=buyer, isActive=True).order_by("pk")
    ]

    searches = [params for params in searches if params]

    if len(flats) < 2 and not (
        flats and searches and viewed.intersection(flat.pk for flat in flats)
    ):
        return None

    groups = {"town": Counter(), "flatType": Counter()}
    numbers = defaultdict(list)
    vectors, savedVectors = [], []

    for flat in flats:
        weight = weights[flat.pk]

        for field in groups:
            groups[field][getattr(flat, field)] += weight

        for field, value in (
            ("price", math.log(flat.askingPrice)),
            ("size", flat.floorAreaSqm),
            ("storey", (flat.storey - 1) // 3),
            ("lease", flat.remainingLease()),
        ):
            numbers[field].append((value, weight))

        vector = photo_mean(flat.tastePhotos)

        if vector is not None:
            vectors.append((vector, weight))

            if flat.pk in saved:
                savedVectors.append((vector, weight))

    for params in searches:
        for field in groups:
            if params.get(field):
                groups[field][params[field]] += SEARCH_WEIGHT

        if params.get("maxPrice"):
            numbers["price"].append((math.log(float(params["maxPrice"])), SEARCH_WEIGHT))

        bounds = [float(params[key]) for key in ("minSqm", "maxSqm") if params.get(key)]

        if bounds:
            numbers["size"].append((sum(bounds) / len(bounds), SEARCH_WEIGHT))

        if params.get("minLease"):
            numbers["lease"].append((float(params["minLease"]), SEARCH_WEIGHT))

    profile = {"weights": dict(weights), "categories": groups, "means": {}, "spreads": {}}

    for field, values in numbers.items():
        total = sum(weight for value, weight in values)
        mean = sum(value * weight for value, weight in values) / total
        profile["means"][field] = mean
        profile["spreads"][field] = math.sqrt(
            sum(weight * (value - mean) ** 2 for value, weight in values) / total
        )

    for key, values in (("embedding", vectors), ("savedEmbedding", savedVectors)):
        profile[key] = (
            validEmbedding(sum(vector * weight for vector, weight in values)) if values else None
        )

    return profile


def gaussian(value, mean, spread):
    return math.exp(-0.5 * ((value - mean) / spread) ** 2)


def scoreFlat(flat, profile):
    factors = {}

    for field, key, weight in (("town", "town", TOWN_WEIGHT), ("flatType", "type", TYPE_WEIGHT)):
        counts = profile["categories"][field]

        if counts:
            factors[key] = weight * counts[getattr(flat, field)] / sum(counts.values())

    for key, value, weight, spread in (
        ("price", math.log(flat.askingPrice), PRICE_WEIGHT, MIN_LOG_SPREAD),
        ("size", flat.floorAreaSqm, SIZE_WEIGHT, (25)),
        ("storey", (flat.storey - 1) // 3, STOREY_WEIGHT, (2)),
        ("lease", flat.remainingLease(), LEASE_WEIGHT, (15)),
    ):
        if key in profile["means"]:
            factors[key] = weight * gaussian(
                value, profile["means"][key], max(spread, profile["spreads"][key])
            )

    vector = photo_mean(flat.tastePhotos)

    if vector is not None and profile["embedding"] is not None:
        factors["photo"] = PHOTO_WEIGHT * max(0, float(vector @ profile["embedding"]))

    if (
        flat.bandLow is not None
        and flat.bandHigh is not None
        and flat.bandLow <= flat.askingPrice <= flat.bandHigh
    ):
        factors["fair"] = FAIR_WEIGHT

    best = max(factors, key=factors.get)

    if best == "photo":
        oldVector = profile["savedEmbedding"]
        reason = (
            "Rooms look like flats you saved"
            if oldVector is not None and float(vector @ oldVector) >= 0.75
            else "Rooms look like flats you explored"
        )
    elif best == "fair":
        reason = "Priced within our fair range"
    elif best in ("town", "type", "price"):
        kind = flat.flatType.replace(" ROOM", "-room").lower()
        typeFit = factors.get("type", 0) >= TYPE_WEIGHT * 0.5
        townFit = factors.get("town", 0) >= TOWN_WEIGHT * 0.5
        subject = f"{kind} flats" if typeFit else "flats"
        place = f" in {flat.town.title()}" if townFit else ""
        price = ""

        if factors.get("price", 0) >= PRICE_WEIGHT * 0.8:
            amount = round(math.exp(profile["means"]["price"]) / 10000) * 10
            price = f" around ${amount:,}k"

        reason = f"Because you looked at {subject}{place}{price}"
    else:
        reason = {
            "size": "Similar in size to flats you explored",
            "lease": "A similar remaining lease to flats you explored",
            "storey": "On floors like flats you explored",
        }[best]

    return sum(factors.values()), reason


def goodFlats(buyer, now=None, skipListing=None):
    now = now or timezone.now()
    saved = (
        SavedListing.objects.filter(collection__user=buyer)
        .exclude(listing_id=skipListing)
        .values("listing_id")
    )
    offers = Offer.objects.filter(buyer=buyer).exclude(listing_id=skipListing).values("listing_id")
    booked = (
        ViewingBooking.objects.filter(buyer=buyer)
        .exclude(slot__listing_id=skipListing)
        .values("slot__listing_id")
    )
    recent = (
        FlatView.objects.filter(buyer=buyer, lastViewedAt__gte=now - timedelta(hours=24))
        .exclude(listing_id=skipListing)
        .values("listing_id")
    )

    return (
        Listing.objects.public()
        .filter(status="live")
        .exclude(seller=buyer)
        .exclude(pk__in=saved)
        .exclude(pk__in=offers)
        .exclude(pk__in=booked)
        .exclude(pk__in=recent)
    )


def maybeFlats(buyer, profile):
    flats = goodFlats(buyer)

    for field in ("town", "flatType"):
        liked = profile["categories"][field]

        if liked:
            flats = flats.filter(
                **{field + "__in": [value for value, weight in liked.most_common(3)]}
            )

    if "price" in profile["means"]:
        mean = profile["means"]["price"]
        spread = max(MIN_LOG_SPREAD, profile["spreads"]["price"]) * 3
        flats = flats.filter(
            askingPrice__gte=math.exp(mean - spread), askingPrice__lte=math.exp(mean + spread)
        )

    return flats.order_by("-createdAt", "-pk")[:MAX_CANDIDATES]


def rankFlats(profile, candidates, limit=RESULTS):
    scored = ((flat, *scoreFlat(flat, profile)) for flat in candidates)

    return nsmallest(limit, scored, key=lambda row: (-row[1], row[0].pk))




def recommendedFlats(buyer):
    if not hasRole(buyer, "buyer"):
        return []

    key = picksKey(buyer)
    cache = caches["default"]
    picked = cache.get(key)

    if picked is None:
        profile = tasteProfile(buyer)
        picked = []

        if profile:
            photos = ListingPhoto.objects.filter(
                modelVersion=MODEL_VERSION, embedding__isnull=False
            )
            pool = maybeFlats(buyer, profile).prefetch_related(
                Prefetch("photos", queryset=photos, to_attr="tastePhotos")
            )
            picked = [
                (flat.pk, score, reason)
                for flat, score, reason in rankFlats(profile, pool.iterator(chunk_size=100))
            ]

        cache.set(key, picked, (600))

    flats = (
        withTier(goodFlats(buyer))
        .filter(pk__in=[row[0] for row in picked])
        .annotate(acceptedCount=hasDeal())
        .prefetch_related(picsByRoom())
    )

    found = {flat.pk: flat for flat in decorate(flats)}
    result = []

    for flatId, score, reason in picked:
        if flatId in found:
            flat = found[flatId]
            flat.recommendationReason = reason
            result.append(flat)

    return result


def picksData(request):
    return {
        "recommendedFlats": (
            recommendedFlats(request.user) if currentMode(request) == "buyer" else []
        )
    }
