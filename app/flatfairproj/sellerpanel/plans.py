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

RANK = {"live": 0, "review": 1, "paused": 1, "sold": 2}

FOCUS_ORDER = Case(
    *[When(status=status, then=Value(rank)) for status, rank in RANK.items()],
    default=Value(len(RANK)),
    output_field=IntegerField(),
)


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
