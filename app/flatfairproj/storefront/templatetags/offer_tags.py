from django import template

from storefront.offeradvice import advise, nearbySales, offerFloor
from storefront.activity import openAsk

register = template.Library()


@register.simple_tag
def offer_advice(offer):
    return advise(offer)


@register.simple_tag(takes_context=True)
def nearby_sales(context, flat):
    cache = context.render_context.setdefault("nearbySales", {})

    if flat.pk not in cache:
        cache[flat.pk] = nearbySales(flat)

    return cache[flat.pk]


@register.simple_tag
def offer_floor(flat):
    return offerFloor(flat)


@register.simple_tag
def deal_progress(offer):
    from storefront.deals import progress
    from storefront.models import Deal

    deal = Deal.objects.filter(offer=offer).first()

    if deal:
        return progress(deal)

    return None


@register.simple_tag
def try_price(raw):
    try:
        value = int(str(raw).replace(",", "").replace("$", "").strip())
    except Exception as error:
        return None

    if 50000 <= value <= 3000000:
        return value

    return None


@register.simple_tag(takes_context=True)
def asked_viewing(context, flat):
    user = context["request"].user

    return bool(user.is_authenticated and openAsk(flat, user))


@register.simple_tag(takes_context=True)
def share_links(context, flat):
    from urllib.parse import quote, urlencode

    from django.conf import settings
    from django.urls import reverse

    path = reverse("listing_detail", args=[flat.publicId])

    if settings.PUBLIC_URL:
        base = settings.PUBLIC_URL.rstrip("/")
    else:
        base = context["request"].build_absolute_uri("/").rstrip("/")

    url = base + path

    if flat.block:
        place = f"Blk {flat.block} {flat.town.title()}"
    else:
        place = flat.town.title()

    words = f"Have a look at this flat on FlatFair: {place}, ${flat.askingPrice:,}"

    return {
        "whatsapp": "https://wa.me/?" + urlencode({"text": f"{words} {url}"}),
        "email": "mailto:?subject="
        + quote(f"A flat on FlatFair: {place}")
        + "&body="
        + quote(f"{words}\n{url}"),
        "url": url,
    }


@register.simple_tag
def price_coach(flat):
    from storefront.offeradvice import openHint

    return openHint(flat)


@register.simple_tag
def photo_upsell(flat):
    from sellerpanel.recommend import picPitch

    return picPitch(flat)


@register.simple_tag
def estimate_gap(asking, raw):
    worth = try_price(raw)

    if not worth or not asking:
        return ""

    gap = int(asking) - worth

    if abs(gap) < 5000:
        return "About your estimate"

    words = f"${abs(gap) / 1000:,.0f}k"

    if gap > 0:
        return f"{words} above your estimate"

    return f"{words} below your estimate"


@register.filter
def cart_points(item):
    from storefront.views import PACKAGE_COPY

    copy = PACKAGE_COPY.get(item.name)

    if copy:
        if item.durationDays:
            duration = f"for {item.durationDays} days"
        else:
            duration = "until your flat is sold"

        return [line.format(duration=duration).strip() for line in copy["benefits"]]

    return [
        part.strip().rstrip(".") for part in (item.description or "").split(". ") if part.strip()
    ]


@register.filter
def cart_icon(item):
    if item.name.startswith("Boost"):
        return "boost"

    return {
        "Top spot": "top",
        "Pro photography": "camera",
        "Floor plan": "plan",
        "Viewing concierge": "host",
    }.get(item.name, "extra")


@register.simple_tag
def offer_steps(offer, viewer):
    from storefront.offeradvice import stepsLine

    return stepsLine(offer, viewer)


@register.simple_tag
def suggest_room(offer):
    from storefront.offeradvice import SUGGEST_LIMIT, hintsLeft

    left = hintsLeft(offer)
    low = int(offer.amount) + 1
    high = int(offer.counterAmount or 0) - 1

    return {
        "left": left,
        "limit": SUGGEST_LIMIT,
        "low": low,
        "high": high,
        "open": left > 0 and low <= high,
    }


@register.simple_tag
def seller_round(offer):
    from storefront.offeradvice import sellerAsk, hintsLeft

    lastAsk = sellerAsk(offer)
    high = int(offer.counterHigh)

    if offer.status == "pending" and lastAsk:
        high = min(high, lastAsk)

    lastRound = offer.status == "pending" and bool(offer.priceSteps) and hintsLeft(offer) == 0

    return {"lastRound": lastRound, "high": high, "open": offer.counterLow <= high}


@register.simple_tag(takes_context=True)
def seller_next_steps(context):
    from sellerpanel.views import stepsFor

    request = context.get("request")

    if request:
        return stepsFor(request)

    return None
