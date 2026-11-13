from urllib.parse import urlencode

from django import template
from django.db.models import Count
from django.urls import reverse

from storefront.forms import FLAT_TYPES
from storefront.models import Listing
from storefront.search import SEARCH_FIELDS
from storefront.interiors import (
    REGIONS,
    areaPaths,
    nearAreas,
    regionName,
    townAreas,
    mapBounds,
    townPoint,
)
from storefront.valuation import TOWNS

register = template.Library()


@register.simple_tag
def saved_count(user):
    if not user.is_authenticated:
        return 0

    return Listing.objects.public().filter(savedBy__collection__user=user).distinct().count()


@register.simple_tag
def flat_types():
    return FLAT_TYPES


def flatsLabel(name, count, joiner=" · "):
    return f"{name}{joiner}{count:,} flat{'' if count == 1 else 's'}"


@register.inclusion_tag("storefront/town_map.html")
def town_map(mode="link", selected="", region="", query=None, field="", list=True, prompt=None):
    selected = str(selected or "").upper()

    if selected:
        region = ""
    else:
        region = regionName(region)

    kept = {}

    for key in (*SEARCH_FIELDS, "sort"):
        if query and query.get(key) and key not in ("q", "town", "region"):
            kept[key] = query.get(key)

    browse = reverse("listings")
    counts = {town: 0 for town in TOWNS}

    if mode == "link":
        live = Listing.objects.public().filter(status="live")

        counts = {
            row["town"]: row["total"] for row in live.values("town").annotate(total=Count("pk"))
        }

    paths = areaPaths()
    used = set()
    regions = []

    for name, regionTowns in REGIONS.items():
        slug = name.lower()
        towns = []

        for town in regionTowns:
            outlines = [paths[area] for area in townAreas(town) if area in paths]
            point = townPoint(town)

            if town not in TOWNS or town not in counts or not outlines or not point:
                continue

            label = town.title()
            words = label.replace("/", "/ ").rsplit(" ", 1)

            lines = [
                {
                    "text": word,
                    "dy": "1.1em" if index else f"{(1 - len(words)) * 0.55:.2f}em",
                }
                for index, word in enumerate(words)
            ]

            used.update(townAreas(town))
            used.update(nearAreas(town))

            towns.append(
                {
                    "town": town,
                    "label": label,
                    "count": counts[town],
                    "paths": outlines,
                    "near": [paths[area] for area in nearAreas(town) if area in paths],
                    "lines": lines,
                    "x": point[0],
                    "y": point[1],
                    "slug": slug,
                    "selected": town == selected,
                    "inRegion": name == region,
                    "href": browse + "?" + urlencode({**kept, "town": town}),
                    "caption": (flatsLabel(label, counts[town]) if mode == "link" else label),
                    "spoken": (flatsLabel(label, counts[town], ", ") if mode == "link" else label),
                }
            )

        total = sum(town["count"] for town in towns)

        regions.append(
            {
                "name": name,
                "slug": slug,
                "towns": towns,
                "current": name == region,
                "href": browse + "?" + urlencode({**kept, "region": name}),
                "caption": flatsLabel(name, total) if mode == "link" else name,
            }
        )

    if prompt is None:
        if mode == "link":
            prompt = "Pick an area"
        else:
            prompt = "Pick your town on the map"

    rest = prompt
    chosen = False

    for group in regions:
        if group["current"]:
            rest = group["caption"]
            chosen = True

        for town in group["towns"]:
            if town["selected"]:
                if mode == "link":
                    rest = town["caption"]
                else:
                    rest = town["label"] + " selected"

                chosen = True

    return {
        "mode": mode,
        "field": field,
        "showList": bool(list) and mode == "link",
        "prompt": prompt,
        "view": mapBounds(),
        "regions": regions,
        "others": [path for area, path in paths.items() if area not in used],
        "kept": kept,
        "allHref": browse + ("?" + urlencode(kept) if kept else ""),
        "allCurrent": not chosen,
        "rest": rest,
    }
