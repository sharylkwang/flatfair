import json
from hashlib import sha256
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from statistics import median
from urllib.parse import urlencode
from django import template
from django.urls import reverse
from storefront.floorplan import sellerPlan, planTitle, planUrl
from storefront.valuation import pastCheck, modelInfo
from storefront.offeradvice import floorCosts, lastSales
from storefront.interiors import ALIASES, centroids
from pretrained import TRAINING
from storefront.notices import new_notes, count_new
from adminpanel.models import TicketMessage

register = template.Library()

FIXED_FEATURES = {
    "wood floor": ("Wood flooring", "flooring"),
    "tiled floor": ("Tiled flooring", "flooring"),
    "marble floor": ("Marble flooring", "flooring"),
    "built-in wardrobe": ("Built-in wardrobes", "wardrobe"),
    "built-in wardrobes": ("Built-in wardrobes", "wardrobe"),
    "built-in cabinets": ("Built-in cabinets", "cabinet"),
    "kitchen cabinets": ("Kitchen cabinets", "cabinet"),
    "air conditioner": ("Air conditioning", "cooling"),
    "ceiling fan": ("Ceiling fan", "fan"),
    "window grilles": ("Window grilles", "grilles"),
    "bathtub": ("Bathtub", "bath"),
    "shower screen": ("Shower screen", "shower"),
    "water heater": ("Water heater", "heater"),
    "toilet bowl": ("Toilet", "toilet"),
}


@register.filter
def fixedFeatures(tags):
    traits = []
    seen = set()

    for tag in tags or []:
        feature = FIXED_FEATURES.get(str(tag).strip().lower())

        if feature and feature[0] not in seen:
            label, icon = feature
            traits.append({"label": label, "icon": icon})
            seen.add(label)

            if len(traits) == 6:
                break

    return traits


@register.simple_tag
def typicalGap(asking, estimate):
    typical = Decimal(str(estimate)).quantize(Decimal("1E3"), rounding=ROUND_HALF_UP)
    gap = Decimal(str(asking)) - typical

    if gap == 0:
        return "Matches our estimate"

    if gap > 0:
        direction = "above"
    else:
        direction = "below"

    return f"${abs(gap):,.0f} {direction} our estimate"


@register.simple_tag
def nearest_mrt(flat):
    from storefront.address import nearest_mrt

    return nearest_mrt(flat.postalCode, flat.block, flat.town)


@register.inclusion_tag("storefront/mrt_badges.html")
def mrt_badges(badges):
    return {"badges": badges}


@register.simple_tag
def notice_unread(user):
    if user.is_authenticated:
        return count_new(user)

    return 0


@register.simple_tag
def latest_notices(user):
    if user.is_authenticated:
        return list(new_notes(user))

    return []


@register.simple_tag
def support_unread(user):
    if not getattr(user, "is_authenticated", False) or user.is_staff:
        return 0

    return TicketMessage.objects.unseen().filter(ticket__customer=user).count()


@register.filter
def flat_label(flat_type):
    kind = str(flat_type).upper().strip()

    if kind.endswith(" ROOM"):
        return kind.replace(" ROOM", "-room")

    return kind.title()


@register.simple_tag(takes_context=True)
def page_url(context, param, number):
    query = context["request"].GET.copy()
    query[param] = number

    return "?" + query.urlencode()


@register.simple_tag(takes_context=True)
def cart_size(context):
    from storefront.models import Product

    cart = context["request"].session.get("cart", [])

    if not cart:
        return 0

    return Product.objects.filter(pk__in=cart, isActive=True).count()


@register.filter
def nobreak(text):
    return str(text).replace("-", "\u2011").replace(" ", "\u00a0")


@register.simple_tag
def fair_scale(low, estimate, high):
    try:
        low, estimate, high = float(low), float(estimate), float(high)
        start, end = low * 0.85, high * 1.15
        span = end - start

        if span <= 0:
            raise ValueError

        scale = {}

        for key, value in [("lo", low), ("est", estimate), ("hi", high)]:
            scale[key] = round(max(0, min(100, (value - start) / span * 100)), 2)

        return scale
    except Exception as error:
        return {"lo": 22, "est": 50, "hi": 78}


@register.filter
def buyer_name(user):
    first = user.first_name.strip() or "Buyer"
    initial = user.last_name.strip()[:1]

    if initial:
        return f"{first} {initial}."

    return first


@register.simple_tag
def evidence_scale(low, estimate, high, asking=None):
    try:
        low, high = float(low), float(high)

        if estimate:
            estimate = float(estimate)
        else:
            estimate = (low + high) / 2

        if asking:
            ask = float(asking)
        else:
            ask = None

        if ask:
            start = min(low * 0.85, ask * 0.95)
            end = max(high * 1.15, ask * 1.05)
        else:
            start = low * 0.85
            end = high * 1.15

        if not 0 < low <= estimate <= high or end <= start:
            return None

        scale = {
            key: round((value - start) / (end - start) * 100, 2)
            for key, value in [("lo", low), ("est", estimate), ("hi", high)]
        }

        if ask:
            scale["asking"] = round((ask - start) / (end - start) * 100, 2)

        return scale
    except Exception as error:
        return None


@register.simple_tag
def model_evidence(version="", current=False):
    info = modelInfo()

    if info.get("available") and (current or version == info["version"]):
        return {"version": info["version"], "month": month_label(info.get("month"))}

    if version:
        return {"version": version}

    return {}


def month_label(month):
    try:
        return datetime.strptime(str(month), "%Y-%m").strftime("%b %Y")
    except Exception as error:
        return month


@register.simple_tag
def band_coverage(percent=False, count=False):
    try:
        report = json.loads((TRAINING / "price_band_eval.json").read_text(encoding="utf-8"))

        if count:
            return report["overall"]["count"]

        share = report["overall"]["servedCoverage"] * 100

        if percent:
            return round(share)

        return round(share / 10)
    except Exception as error:
        return None


@register.simple_tag(name="price_uncertainty")
def priceUncertainty(town="", flatType=""):
    report = pastCheck()

    if not report:
        return ""

    townName = str(town).strip().upper()
    summary = report["byTown"].get(townName)

    if not summary:
        return ""

    count, share = summary["count"], summary["servedCoverage"]
    guide = "Treat this range as a rough guide."

    if count < 30:
        return f"We could only test this on {count} past sales in this town. {guide}"

    if share < 0.8:
        return f"In {townName.title()}, {share:.0%} of {count} past sales fell inside our range. {guide}"

    summary = report["byFlatType"].get(str(flatType).strip().upper())

    if summary and summary["servedCoverage"] < 0.8:
        share, count = summary["servedCoverage"], summary["count"]

        return f"For {flat_label(flatType)} flats, {share:.0%} of {count} past sales fell inside our range. {guide}"

    return ""


@register.simple_tag
def town_pin(town):
    name = str(town).upper()

    return centroids().get(ALIASES.get(name, name))


def money_short(value):
    try:
        value = Decimal(str(value))

        if value < 0:
            sign = "-"
        else:
            sign = ""

        value = abs(value)
        dollars = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        thousands = (value / 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        millions = (value / 1000000).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception as error:
        return ""

    if dollars < 1000:
        return f"{sign}${dollars}"

    if thousands < 1000:
        return f"{sign}${thousands}k"

    return f"{sign}${millions}m"


register.filter("short_money", money_short)


@register.filter
def typical_price(value):
    try:
        return f"about ${nearest_thousand(value):,.0f}"
    except Exception as error:
        return ""


def nearest_thousand(value):
    return Decimal(str(value)).quantize(Decimal("1E3"), rounding=ROUND_HALF_UP)


@register.filter
def range_money(value):
    try:
        return f"${nearest_thousand(value):,.0f}"
    except Exception as error:
        return ""


@register.simple_tag
def price_gap(asking, low, estimate, high, subject="The asking price", short=False):
    try:
        asking = Decimal(str(asking))

        if not (asking and low and estimate and high):
            return ""

        gap = asking - nearest_thousand(estimate)
        place = "above"

        if gap < 0:
            gap = -gap
            place = "below"

        inside = low <= asking <= high
    except Exception as error:
        return ""

    if inside:
        where = "within the fair range"
    else:
        where = "outside the fair range"

    if short and gap == 0:
        return f"matches our estimate, {where}"

    if short:
        return f"${gap:,.0f} {place} our estimate, {where}"

    if gap == 0:
        return f"{subject} matches our estimate."

    return f"{subject} is ${gap:,.0f} {place} our estimate and {where}."


@register.simple_tag
def asking_gap(amount, asking):
    try:
        gap = Decimal(str(amount)) - Decimal(str(asking))
    except Exception as error:
        return ""

    if not gap.is_finite():
        return ""

    if gap > 0:
        return f"Your offer is ${gap:,.0f} above the asking price."

    if gap < 0:
        return f"Your offer is ${-gap:,.0f} below the asking price."

    return "Your offer matches the asking price."


@register.filter
def offer_word(offer):
    offer_words = {
        "pending": "Waiting for seller",
        "countered": "Seller suggested a new price",
        "accepted": "Accepted",
        "declined": "Declined by seller",
        "withdrawn": "Withdrawn",
    }

    if offer.status == "withdrawn" and offer.counterAmount:
        return "You declined"

    return offer_words.get(offer.status, offer.get_status_display())


@register.filter
def viewing_word(booking):
    label = booking.statusLabel

    if label == "Requested":
        return "Waiting for seller to confirm"

    return label


@register.simple_tag
def save_signin(flat):
    target = reverse("listing_detail", args=[flat.publicId]) + f"?save=1#save-{flat.publicId}"

    return reverse("signin") + "?" + urlencode({"next": target})


@register.simple_tag
def recent_sales(town, flatType, area, block=None, lease=None, storey=None):
    return lastSales(town, flatType, area, block, lease, storey)


@register.filter
def sales_median(sales):
    if sales:
        return median(sale.price for sale in sales)

    return None


@register.simple_tag
def floor_prices(flat):
    return floorCosts(flat.town, flat.flatType, flat.storey)


@register.simple_tag
def seller_floor_plan(flat):
    return sellerPlan(flat)


@register.filter
def plan_src(flat):
    return planUrl(flat)


@register.filter
def plan_caption(flat):
    return planTitle(flat)


@register.simple_tag
def townCover(town):
    from storefront.interiors import townCover as cover

    return cover(town)


@register.filter
def townTone(town):
    tones = ("sky", "mint", "periwinkle", "lilac", "good-soft", "warn-soft")
    digest = sha256(str(town).strip().upper().encode()).digest()

    return tones[int.from_bytes(digest[:4], "big") % len(tones)]


@register.simple_tag
def estatePhoto(flat):
    from storefront.interiors import estatePic as photo

    return photo(flat)
