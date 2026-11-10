from decimal import Decimal
from datetime import date, timedelta
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Avg, Case, Count, IntegerField, Value, When
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from storefront.models import (
    needEmail, Sale, Offer, FlatView,
    Listing, PriceSuggestion, SavedListing,
)
from .notices import offer_note, _place, notify

SUGGEST_LIMIT = 3
WINDOW_DAYS = 14


def round1000(value):
    price = Decimal(value)
    part = price / 1000
    whole = part.quantize(Decimal("1"))
    price = whole * 1000

    return int(price)


def dollars(value):
    return f"${int(value):,}"


def offerFloor(flat):
    if flat.autoDeclineLow and flat.estimatedPrice and flat.bandLow:
        return round1000(flat.bandLow)

    return None


def counter_tip(amount, target, asking):
    price = round1000(target)
    ask = int(asking)

    if ask < price:
        price = ask

    if price <= amount:
        return None

    return price


def advise(offer):
    flat = offer.listing

    if not (flat.estimatedPrice and flat.bandLow and flat.askingPrice):
        return None



    worth = round1000(flat.estimatedPrice)
    low = round1000(flat.bandLow)
    asking = flat.askingPrice
    amount = int(offer.amount)
    gap = amount - worth

    if abs(gap) >= 1000:
        gap = round1000(gap)

    percent = abs(gap) * 100
    percent = percent / worth



    percent = round(percent)

    if amount >= asking:
        return {
            "tone": "good",
            "action": "accept",
            "counter": None,
            "headline": "This offer meets your asking price.",
            "detail": (
                f"It is {dollars(gap)} above our estimate of {dollars(worth)}."
                if gap > 0
                else f"Our estimate for your flat is {dollars(worth)}."
            ),
        }

    if amount >= worth:
        return {
            "tone": "good",
            "action": "accept",
            "counter": None,
            "headline": "A strong offer: at or above our estimate.",
            "detail": f"Our estimate for your flat is {dollars(worth)}. Similar flats sold from {dollars(low)}.",
        }

    if amount >= low:
        counter = counter_tip(amount, (amount + min(worth, asking)) / 2, asking)

        return {
            "tone": "fair",
            "action": "counter" if counter else "accept",
            "counter": counter,
            "headline": f"{dollars(-gap)} ({percent}%) below our estimate, but inside the fair range.",
            "detail": (
                f"Meeting halfway between this offer and our estimate of {dollars(worth)} gives {dollars(counter)}."
                if counter
                else f"Our estimate for your flat is {dollars(worth)}."
            ),
        }

    counter = counter_tip(amount, min(worth, asking), asking)

    return {
        "tone": "low",
        "action": "counter" if counter else "decline",
        "counter": counter,
        "headline": f"{dollars(-gap)} ({percent}%) below our estimate, and below what similar flats sold for.",
        "detail": (
            f"Similar flats sold from {dollars(low)}. A counter at our estimate of {dollars(min(worth, asking))} keeps the talk going."
            if counter
            else f"Similar flats sold from {dollars(low)}."
        ),
    }


def nearbySales(flat, count=3):
    return list(
        lastSales(
            flat.town,
            flat.flatType,
            flat.floorAreaSqm,
            flat.block,
            flat.leaseCommenceYear,
            flat.storey,
        )[:count]
    )


def floor_bands(storey):
    bands = []
    floor = storey - 1
    group = floor // 3
    start = group * 3
    start = start + 1

    for bandFrom in (start - 3, start, start + 3):
        if bandFrom >= 1:
            bandTo = bandFrom + 2
            bands.append(f"{bandFrom:02d} TO {bandTo:02d}")

    return bands


def lastSales(town, flatType, area, block=None, lease=None, storey=None):
    size_range = 8
    lease_range = 5

    if not (town and flatType and area):
        return Sale.objects.none()

    similar = Sale.objects.filter(
        town=town, flatType=flatType, floorAreaSqm__range=(area - size_range, area + size_range)
    )

    if lease:
        similar = similar.filter(
            leaseCommenceYear__range=(lease - lease_range, lease + lease_range)
        )

    sameBlock = Case(
        When(block=block or "", then=Value(0)), default=Value(1), output_field=IntegerField()
    )



    nearFloor = Value(0)

    if storey:
        nearFloor = Case(
            When(storeyRange__in=floor_bands(storey), then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        )

    similar = similar.annotate(sameBlock=sameBlock, nearFloor=nearFloor)

    return similar.order_by("sameBlock", "nearFloor", "-month", "-pk")[:5]


def usualSales(town, flatType):
    sales = Sale.objects.filter(town=town, flatType=flatType)
    count = sales.count()

    if count < 10:
        return {}

    prices = sales.order_by("price", "pk").values_list("price", flat=True)


    return {
        "lo": prices[count // 10],
        "est": prices[count // 2],
        "hi": prices[count * 9 // 10],
        "saleCount": count,
    }


def floorCosts(town, flatType, storey):
    today = timezone.localdate()
    since = date(today.year - 1, today.month, 1)



    bands = (
        Sale.objects.filter(town=town, flatType=flatType, month__gte=since)
        .values("storeyRange")
        .annotate(sales=Count("pk"), average=Avg("price"))
        .filter(sales__gte=5)
        .order_by("-storeyRange")
    )

    rows = []


    for band in bands:
        low = int(band["storeyRange"][:2])
        high = int(band["storeyRange"][-2:])
        rows.append(
            {
                "label": f"{low}–{high}",
                "sales": band["sales"],
                "average": int(round(band["average"], -3)),
                "mine": low <= storey <= high,
            }
        )

    if len(rows) < 2:
        return {}

    highest = max(row["average"] for row in rows)



    for row in rows:
        row["width"] = round(row["average"] * 100 / highest)

    return {"rows": rows, "gap": rows[0]["average"] - rows[-1]["average"]}


def priceSteps(offer):
    steps = list(offer.priceSteps or [])

    if not steps:
        steps.append({"by": "buyer", "amount": int(offer.amount)})

        if offer.counterAmount:
            steps.append({"by": "seller", "amount": int(offer.counterAmount)})

    return steps


def hints_used(offer):
    buyerSteps = []

    for step in offer.priceSteps or []:
        if step["by"] == "buyer":
            buyerSteps.append(step)


    used = len(buyerSteps) - 1

    if used < 0:
        used = 0

    return used


def hintsLeft(offer):
    return max(0, SUGGEST_LIMIT - hints_used(offer))


def sellerAsk(offer):
    asks = []

    for step in offer.priceSteps or []:
        if step["by"] == "seller":
            asks.append(step["amount"])

    if asks:
        return asks[-1]

    return None


def stepsAfter(offer, amount):
    steps = priceSteps(offer)

    if offer.status == "countered" and steps[-1]["by"] == "seller":
        steps[-1] = {"by": "seller", "amount": int(amount)}
    else:
        steps.append({"by": "seller", "amount": int(amount)})

    return steps


def counterOk(offer, amount):
    ceiling = sellerAsk(offer)

    if ceiling and offer.status == "pending" and amount > ceiling:
        raise ValidationError(
            f"Your new price can’t be more than the ${ceiling:,} you asked before."
        )


def hintPrice(offer, amount):
    if offer.status != "countered" or not offer.counterAmount:
        raise ValidationError(
            "You can only suggest a price after the seller has replied with theirs."
        )

    if hintsLeft(offer) == 0:
        raise ValidationError(
            f"You’ve already suggested {SUGGEST_LIMIT} prices on this offer. "
            f"You can agree on ${offer.counterAmount:,.0f} or close your offer."
        )

    if amount <= offer.amount or amount >= offer.counterAmount:
        raise ValidationError(
            f"Please suggest a price above your ${offer.amount:,.0f} offer "
            f"and below the seller’s ${offer.counterAmount:,.0f}."
        )

    steps = priceSteps(offer)


    steps.append({"by": "buyer", "amount": int(amount)})

    updated = Offer.objects.filter(
        pk=offer.pk, status="countered", counterAmount=offer.counterAmount
    ).update(
        amount=amount,
        counterAmount=None,
        status="pending",
        priceSteps=steps,
        respondedAt=timezone.now(),
    )

    if not updated:
        raise ValidationError(
            "The seller changed their price. Please reload the page to see it."
        )



    offer.amount = amount
    offer.counterAmount = None


    offer.status = "pending"
    offer.priceSteps = steps
    offer_note(offer, "offer_suggested", "seller")

    return f"We’ve sent your price of ${amount:,.0f} to the seller."


def stepsLine(offer, viewer):
    steps = offer.priceSteps or []

    if len(steps) < 3:
        return ""

    words = []


    for index, step in enumerate(steps):
        if step["by"] == viewer:
            who = "You"
        else:
            who = step["by"].capitalize()

        if step["by"] == "seller":
            verb = "asked"
        elif index == 0:
            verb = "offered"
        else:
            verb = "suggested"

        words.append(f"{who} {verb} ${step['amount']:,}")

    return " · ".join(words)


def interest(flat, since):
    return {
        "views": FlatView.objects.filter(listing=flat, lastViewedAt__gte=since).count(),
        "saves": SavedListing.objects.filter(listing=flat, createdAt__gte=since).count(),
        "offers": Offer.objects.filter(listing=flat, createdAt__gte=since).count(),
        "agreed": Offer.objects.filter(listing=flat, status="accepted").exists(),
    }


def suggestion(flat, now=None):
    now = now or timezone.now()

    if (
        flat.status != "live"
        or not flat.estimatedPrice
        or flat.createdAt > now - timedelta(days=WINDOW_DAYS)
    ):
        return None

    target = round1000(flat.estimatedPrice)



    if (
        flat.askingPrice <= target
        or (flat.askingPrice - flat.estimatedPrice) / flat.estimatedPrice < 0.01
    ):
        return None

    seen = interest(flat, now - timedelta(days=WINDOW_DAYS))

    if seen["agreed"] or seen["offers"] or seen["saves"] >= 2 or seen["views"] >= 10:
        return None

    return {"price": target, "views": seen["views"], "saves": seen["saves"]}


def openHint(flat):
    found = (
        PriceSuggestion.objects.filter(listing=flat, status="open").order_by("-createdAt").first()
    )

    if found and (flat.askingPrice <= found.price or flat.status != "live"):
        status = "accepted" if flat.askingPrice <= found.price else "expired"
        PriceSuggestion.objects.filter(pk=found.pk).update(status=status, closedAt=timezone.now())

        return None

    return found


@transaction.atomic
def coachSlow(now=None):
    now = now or timezone.now()
    recent = PriceSuggestion.objects.filter(createdAt__gte=now - timedelta(days=30)).values(
        "listing_id"
    )

    flats = (
        Listing.objects.filter(
            status="live",
            createdAt__lte=now - timedelta(days=WINDOW_DAYS),
            estimatedPrice__isnull=False,
            seller__is_active=True,
            seller__last_login__gte=now - timedelta(days=60),
        )
        .exclude(pk__in=recent)
        .select_related("seller")
        .order_by("createdAt", "pk")[:50]
    )

    made = 0

    for flat in flats:
        idea = suggestion(flat, now)

        if not idea:
            continue


        found = PriceSuggestion.objects.create(
            listing=flat,
            price=idea["price"],
            oldPrice=flat.askingPrice,
            estimate=flat.estimatedPrice,
            views=idea["views"],
            saves=idea["saves"],
            createdAt=now,
        )

        notify(
            flat.seller_id,
            "price_coach",
            f"Few buyers have shown interest in {_place(flat)} for {WINDOW_DAYS} days. Our estimate suggests ${found.price:,}. You can lower your price in one press",
            reverse("seller_listing", args=[flat.publicId]) + "#price-coach",
        )

        made += 1



    return made


@login_required
@require_POST
@needEmail
def keep_price(request, pk):
    found = get_object_or_404(
        PriceSuggestion.objects.select_related("listing"),
        pk=pk,
        listing__seller=request.user,
        status="open",
    )
    PriceSuggestion.objects.filter(pk=found.pk).update(status="kept", closedAt=timezone.now())



    messages.success(
        request, "Got it. We’ll keep your price and won’t suggest another for 30 days."
    )



    return redirect(reverse("seller_listing", args=[found.listing.publicId]))
