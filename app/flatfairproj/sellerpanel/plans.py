import re
from datetime import timedelta
from django.core.exceptions import ValidationError
from django.db.models import (
    DateTimeField, DurationField, ExpressionWrapper, IntegerField,
    OuterRef, Q, Subquery, Value,
    Case, When,
)
from django.db.models.functions import Cast
from django.urls import reverse
from django.utils import timezone
from storefront.models import listing_id, OrderItem, Offer, ViewingBooking

PLAN_ORDER_STATUSES = ["paid", "processing", "completed"]
CART_LISTING_STATUSES = ("live", "review")
RANK = {"live": 0, "review": 1, "paused": 1, "sold": 2}

FOCUS_ORDER = Case(
    *[When(status=status, then=Value(rank)) for status, rank in RANK.items()],
    default=Value(len(RANK)),
    output_field=IntegerField(),
)


def packIssue(product):
    durations = {1: 30, 2: 90, 3: None}
    names = {"Boost 30 days": 1, "Boost 90 days": 2, "Top spot": 3}

    if product.kind != "package":
        return ""

    if product.tier not in durations:
        return "Choose a valid place in search for this package."



    days = durations[product.tier]

    if product.durationDays != days:
        span = "until sold" if days is None else f"{days} days"

        return f"This package must last {span} to match its place in search."

    tier = names.get(product.name)
    dayMatch = re.fullmatch(r"Boost (\d+) days", product.name, re.IGNORECASE)


    if (tier is not None and tier != product.tier) or (dayMatch and int(dayMatch[1]) != days):
        return "The package name must match its duration and place in search."

    return ""


def cartFlat(user, chosen):
    flats = user.listings.filter(status__in=CART_LISTING_STATUSES)
    flat = None

    if chosen:
        try:
            flat = flats.filter(publicId=listing_id(chosen)).first()
        except ValidationError:
            flat = None

    if flat is None and flats.count() == 1:
        flat = flats.first()

    return flat


def planNote(plan):
    place = f"Blk {plan['listing'].block} {plan['listing'].town.title()}"

    if plan["daysLeft"] is None:
        return f"Running on {place}: {plan['name']}, until the flat is sold"

    return f"Running on {place}: {plan['name']}, {plan['daysLeft']} days left"


def goodPacks(now):
    packs = OrderItem.objects.filter(kind="package", order__status__in=PLAN_ORDER_STATUSES).exclude(
        listing__status__in=("sold", "withdrawn")
    )



    span = ExpressionWrapper(
        Cast("durationDays", IntegerField()) * Value(timedelta(days=1)),
        output_field=DurationField(),
    )

    cutoff = ExpressionWrapper(Value(now) - span, output_field=DateTimeField())

    return packs.filter(Q(durationDays__isnull=True) | Q(order__createdAt__gt=cutoff))


def live_packs(listings, now):
    return goodPacks(now).filter(listing__in=listings)


def packInfo(line, now):
    days = line.durationDays
    expiry = line.order.createdAt + timedelta(days=days) if days is not None else None


    daysLeft = max(0, (expiry - now).days) if expiry else None

    return {
        "name": line.name,
        "kind": line.kind,
        "tier": line.tier,
        "expiry": expiry,
        "daysLeft": daysLeft,
        "order": line.order,
        "listing": line.listing,
        "status": packState(line, now),
        "startedAt": line.order.createdAt if expiry else None,
    }


def packsNow(listings, now=None):
    now = now or timezone.now()

    lines = (
        live_packs(listings, now)
        .select_related("order", "listing", "product")
        .order_by("-order__createdAt", "-pk")
    )

    packs = []

    for line in lines:
        packs.append(packInfo(line, now))

    return packs


def plansNow(listings):
    now = timezone.now()
    lines = live_packs(listings, now)

    newest = (
        lines.filter(listing_id=OuterRef("listing_id"))
        .order_by("-order__createdAt", "-pk")
        .values("pk")[:1]
    )

    lines = (
        lines.filter(pk=Subquery(newest))
        .select_related("order", "listing", "product")
        .order_by("-order__createdAt", "-pk")
    )



    plans = {}

    for line in lines:
        flatId = line.listing_id
        plans[flatId] = packInfo(line, now)

    return plans


def myExtras(user):
    steps = {
        "paid": "Being scheduled",
        "processing": "Booked",
        "completed": "Done",
        "refunded": "Refunded",
    }

    lines = (
        OrderItem.objects.filter(order__seller=user, kind="addon")
        .select_related("order")
        .order_by("-order__createdAt")
    )



    extras = []

    for line in lines:
        service = {
            "name": line.name,
            "order": line.order,
            "step": steps[line.order.status],
            "listing_id": line.listing_id,
        }

        extras.append(service)

    return extras


def packState(line, now=None):
    now = now or timezone.now()

    if line.order.status == "refunded":
        return "Refunded"

    if line.listing and line.listing.status == "sold":
        return "Ended"

    days = line.durationDays

    if days is None:
        return "Until sold"

    expiry = line.order.createdAt + timedelta(days=days)



    if expiry <= now:
        return "Ended"

    return f"Active · {max(0, (expiry - now).days)} days left"


def stepFlats(user):
    return user.listings.filter(status__in=RANK).order_by(FOCUS_ORDER, "-createdAt", "-pk")


def focus_flat(user, publicId=None):
    flats = stepFlats(user)

    if publicId:
        chosen = flats.filter(publicId=publicId).first()

        if chosen:
            return chosen

    return (
        user.listings.exclude(status="withdrawn").order_by(FOCUS_ORDER, "-createdAt", "-pk").first()
    )


def sell_steps(flat):
    listed = False
    sold = False
    hosted = False
    agreed = False


    if flat is not None:
        listed = flat.status in ("live", "review", "paused", "sold")
        sold = flat.status == "sold"

        hosted = ViewingBooking.objects.filter(
            slot__listing=flat, status__in=("confirmed", "done")
        ).exists()



        agreed = sold or Offer.objects.filter(listing=flat, status="accepted").exists()

    steps = [
        {"text": "List your flat (free)", "done": listed, "url": "sell", "fragment": ""},
        {
            "text": "Host viewings",
            "done": hosted,
            "url": "seller_viewings",
            "fragment": "#viewings-tab",
        },
        {
            "text": "Accept an offer",
            "done": agreed,
            "url": "seller_viewings",
            "fragment": "#offers-tab",
        },
        {
            "text": "Finish with HDB, then mark it sold",
            "done": sold,
            "url": "my_listings",
            "fragment": "",
        },
    ]

    foundNow = False

    for step in steps:
        step["href"] = reverse(step["url"]) + step["fragment"]

        if step["done"]:
            step["state"] = "done"
        elif not foundNow:
            step["state"] = "current"
            foundNow = True
        else:
            step["state"] = "next"


    if flat is not None:
        steps[-1]["href"] = reverse("seller_listing", args=[flat.publicId])


    return steps
