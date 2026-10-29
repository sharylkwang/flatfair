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

@register.simple_tag
def nearest_mrt(flat):
    from storefront.address import nearest_mrt

    return nearest_mrt(flat.postalCode, flat.block, flat.town)


@register.inclusion_tag("storefront/mrt_badges.html")
def mrt_badges(badges):
    return {"badges": badges}


@register.filter
def flat_label(flat_type):
    kind = str(flat_type).upper().strip()

    if kind.endswith(" ROOM"):
        return kind.replace(" ROOM", "-room")

    return kind.title()


@register.filter
def buyer_name(user):
    first = user.first_name.strip() or "Buyer"
    initial = user.last_name.strip()[:1]

    if initial:
        return f"{first} {initial}."

    return first


register.filter("short_money", money_short)


