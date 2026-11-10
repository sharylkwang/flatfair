import json
import re
from difflib import get_close_matches
from pretrained import ASSETS
from math import isfinite

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Count, Max, Min, Q
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from storefront.models import hasRole, notAllowed, lock_user, Listing, SavedSearch, SearchAlert
from .address import blocks, street_names
from .forms import FLAT_TYPES
from .notices import searchNote
from .presentation import rankFlats
from .interiors import REGIONS, regionName
from .valuation import TOWNS

SEARCH_DATA = ASSETS / "data" / "search_words.json"


def searchData():
    data = json.loads(SEARCH_DATA.read_text(encoding="utf-8"))
    towns = {entry["s"]: entry["t"] for entry in blocks().values()}
    areas = {}



    for name, streets in data["areas"].items():
        for street in streets:
            if street in towns:
                areas[name] = (towns[street], streets)

                break


    data["areas"] = areas

    return data


SEARCH_FIELDS = (
    "q",
    "town",
    "region",
    "flatType",
    "minPrice",
    "maxPrice",
    "minSqm",
    "maxSqm",
    "bedrooms",
    "bathrooms",
    "minLease",
    "balcony",
    "all",
)
TYPE_WORDS = re.compile(
    r"\b(?:([2-5])[\s-]?(?:room|rm)s?|exec(?:utive)?)\b", re.IGNORECASE
)

AMOUNT = r"\$?(\d[\d,]*(?:\.\d+)?)\s?([km]?)\b"
PRICE_RANGE = re.compile(rf"(?<!\S){AMOUNT}\s?(?:to|-)\s?{AMOUNT}", re.IGNORECASE)

PRICE_LIMIT = re.compile(
    rf"\b(under|below|max|above|over|min)\s+{AMOUNT}", re.IGNORECASE
)

TOWN_CUTOFF = 0.75
LEASE_CHOICES = [50, 60, 70, 80]


def number(value, maximum, decimal=False):
    try:
        value = float(value)


        if not isfinite(value) or not 0 < value <= maximum:
            return None

        if decimal:
            return value

        return int(value)
    except Exception as error:
        return None


def costRange():
    prices = (
        Listing.objects.public()
        .filter(status="live")
        .aggregate(lowest=Min("askingPrice"), highest=Max("askingPrice"))
    )

    if prices["lowest"] is None:
        return None, None


    return (
        prices["lowest"] // 10000 * 10000,
        (prices["highest"] + 9999) // 10000 * 10000,
    )


def costCap():
    return costRange()[1]


def roadWords(text):
    street_words = searchData()["street_words"]
    number_words = searchData()["number_words"]

    words = []

    for word in text.upper().split():
        if words and words[-1] in street_words.values() and word in number_words:
            word = number_words[word]

        words.append(street_words.get(word, word))

    return " ".join(words)


def roadStarts():
    starts = set()

    for street in street_names():
        words = street.split()

        for size in range(1, len(words) + 1):
            starts.add(" ".join(words[:size]))

    return frozenset(starts)


def town_words(text):
    return "/".join(part.strip() for part in " ".join(text.upper().split()).split("/"))


def estate_of(text):
    name = " ".join(str(text or "").upper().split())

    if name in searchData()["areas"]:
        return name

    return ""


def hintEstate(text):
    name = " ".join(str(text or "").upper().split())

    if len(name) < 3:
        return ""

    for estate in searchData()["areas"]:
        if estate.startswith(name):
            return estate

    return ""


def knownTown(text):
    name = town_words(text)

    if name in TOWNS:
        return name


    return searchData()["towns"].get(name, "")


def pullType(text):
    found = TYPE_WORDS.search(text)

    if not found:
        return "", text

    if found.group(1):
        flatType = f"{found.group(1)} ROOM"
    else:
        flatType = "EXECUTIVE"

    rest = " ".join((text[: found.start()] + " " + text[found.end() :]).split())

    return flatType, rest


def money(digits, unit):
    value = (
        float(digits.replace(",", "")) * {"": 1, "K": 1000, "M": 1000000}[unit.upper()]
    )

    if value > 99000000 or (not unit and value < 100000):
        return ""

    return str(int(value))


def pullPrice(text):
    found = PRICE_RANGE.search(text)

    if found:
        lowest, highest = money(found.group(1), found.group(2)), money(
            found.group(3), found.group(4)
        )

        if not lowest or not highest:
            found = None



    if not found:
        found = PRICE_LIMIT.search(text)

        if not found:
            return "", "", text

        amount = money(found.group(2), found.group(3))

        if not amount:
            return "", "", text

        ceiling = found.group(1).lower() in ("under", "below", "max")
        lowest, highest = ("", amount) if ceiling else (amount, "")

    rest = " ".join((text[: found.start()] + " " + text[found.end() :]).split())

    return lowest, highest, rest


def sectorName(text):
    words = " ".join(str(text or "").upper().replace("-", " ").split())

    for start in ("IN THE ", "IN ", "THE "):
        if words.startswith(start):
            words = words[len(start) :]

    for region in REGIONS:
        label = region.upper().replace("-", " ")

        if words in (label, label.replace(" ", "")):
            return region

    return ""


def hintSector(text):
    name = " ".join(str(text or "").upper().replace("-", " ").split())

    if len(name) < 3:
        return ""

    for region in REGIONS:
        label = region.upper().replace("-", " ")

        if label.startswith(name) or label.replace(" ", "").startswith(name):
            return region

    return ""


def pullTown(text):
    words = text.split()
    town = knownTown(text)

    if town:
        return town, "", town_words(text) != town

    if not words or estate_of(text) or roadWords(text) in roadStarts():
        return "", text, False

    for size in range(len(words) - 1, 0, -1):
        for part, rest in (
            (words[:size], words[size:]),
            (words[-size:], words[:-size]),
        ):
            town = knownTown(" ".join(part))

            if town:
                return town, " ".join(rest), town_words(" ".join(part)) != town

    close = get_close_matches(
        town_words(text), TOWNS + list(searchData()["towns"]), n=1, cutoff=TOWN_CUTOFF
    )


    if close:
        return knownTown(close[0]), "", True

    return "", text, False


def readSearch(text, withPlace=True):
    link_words = {"in", "at", "near", "around"}
    found = {"region": "", "town": "", "corrected": False}
    text = str(text or "").strip()[:60].strip()
    found["flatType"], text = pullType(text)
    found["minPrice"], found["maxPrice"], text = pullPrice(text)



    kept = " ".join(word for word in text.split() if word.lower() not in link_words)

    if kept or found["flatType"] or found["minPrice"] or found["maxPrice"]:
        text = kept

    if withPlace:
        found["region"] = sectorName(text)

        if found["region"]:
            text = ""
        else:
            found["town"], text, found["corrected"] = pullTown(text)

    found["q"] = text

    return found


def correctedTown(filters):
    if filters.get("town"):
        return ""

    found = readSearch(filters.get("q"))

    if found["corrected"]:
        return found["town"]

    return ""


def hintSearch(text):
    found = readSearch(text)

    place = (
        found["town"].title().replace("/", " / ")
        or found["region"]
        or estate_of(found["q"]).title()
    )

    if not place or not found["flatType"] or (found["town"] and found["q"]):
        return None


    kind = found["flatType"].replace(" ROOM", "-room").capitalize()

    return {
        "kind": "search",
        "label": f"{place} · {kind}",
        "value": f"{place.replace(' / ', '/')} {kind}",
    }


def searchedEstate(filters):
    text = readSearch(filters.get("q"), False)["q"]
    estate = estate_of(text)
    estates = searchData()["areas"]

    if not estate or filters.get("town") not in (None, "", estates[estate][0]):
        return None

    return {"name": estate.title(), "town": estates[estate][0]}


def hintTown(text):
    name = " ".join(str(text or "").upper().split())

    if len(name) < 3 or any(letter.isdigit() for letter in name):
        return ""

    aliases = searchData()["towns"]



    if name in aliases:
        return aliases[name]



    starts = {town[: len(name)]: town for town in reversed(TOWNS)}
    close = get_close_matches(name, list(starts), n=1, cutoff=TOWN_CUTOFF)

    if close:
        return starts[close[0]]

    return ""


def tidySearch(filters, priceCap=None, priceFloor=None):
    params = {}



    typed = readSearch(filters.get("q"), not filters.get("town"))
    search = typed["q"]

    if typed["town"]:
        params["town"] = typed["town"]


    if search:
        params["q"] = search

    town = str(filters.get("town") or "").strip().upper()



    if town in TOWNS:
        params["town"] = town

    region = regionName(filters.get("region")) or typed["region"]

    if region and "town" not in params:
        params["region"] = region

    flatType = str(filters.get("flatType") or "").strip().upper() or typed["flatType"]

    if flatType in FLAT_TYPES:
        params["flatType"] = flatType

    for field in ("minPrice", "maxPrice"):
        try:
            value = filters.get(field)

            if value in (None, ""):
                value = typed[field]

            value = float(value)

            if not isfinite(value):
                continue

            if priceCap is None:
                price_field = Listing._meta.get_field("askingPrice")
                lower, upper = connection.ops.integer_field_range(price_field.get_internal_type())

                if not max(0, lower) <= value <= upper:
                    continue

            if priceCap is not None:
                value = min(max(value, priceFloor or 0), priceCap)

            params[field] = str(int(value))
        except Exception as error:
            continue

    if "minPrice" in params and "maxPrice" in params:
        if int(params["minPrice"]) > int(params["maxPrice"]):
            params["minPrice"], params["maxPrice"] = (
                params["maxPrice"],
                params["minPrice"],
            )

    for field, maximum, decimal in (
        ("minSqm", 400, True),
        ("maxSqm", 400, True),
        ("bedrooms", 10, False),
        ("bathrooms", 10, False),
        ("minLease", 99, False),
    ):
        value = number(filters.get(field), maximum, decimal)

        if value:
            params[field] = str(value)

    if filters.get("balcony") == "1":
        params["balcony"] = "1"

    return params


def findHomes(q, queryset=None, priceCap=None, clampPrices=True):
    minAsk = None

    if priceCap is None:
        minAsk, priceCap = costRange()


    if minAsk is not None:
        minSpend = minAsk
    else:
        minSpend = 0

    if priceCap is not None:
        maxSpend = priceCap
    else:
        maxSpend = 3000000

    if clampPrices:
        q = tidySearch(q, maxSpend, minSpend)
    else:
        q = tidySearch(q)

    if clampPrices:
        if q.get("minPrice") == str(minSpend):
            q.pop("minPrice")

        if q.get("maxPrice") == str(maxSpend):
            q.pop("maxPrice")

    search = q.get("q", "")
    town = q.get("town", "")

    if town not in TOWNS:
        town = ""

    region = q.get("region", "")
    flatType = q.get("flatType", "")

    if flatType not in FLAT_TYPES:
        flatType = ""

    if "minPrice" in q:
        minCost = int(q["minPrice"])
    else:
        minCost = None

    if "maxPrice" in q:
        maxCost = int(q["maxPrice"])
    else:
        maxCost = None

    minSqm = float(q["minSqm"]) if q.get("minSqm") else None
    maxSqm = float(q["maxSqm"]) if q.get("maxSqm") else None
    beds = int(q["bedrooms"]) if q.get("bedrooms") else None
    baths = int(q["bathrooms"]) if q.get("bathrooms") else None


    minLease = int(q["minLease"]) if q.get("minLease") else None
    balcony = q.get("balcony") == "1"

    filters = {
        field: value
        for field, value in (
            ("town", town),
            ("flatType", flatType),
            ("askingPrice__gte", minCost),
            ("askingPrice__lte", maxCost),
            ("floorAreaSqm__gte", minSqm),
            ("floorAreaSqm__lte", maxSqm),
            ("bedrooms__gte", beds),
            ("bathrooms__gte", baths),
            ("hasBalcony", balcony),
        )
        if value
    }

    if maxCost == 0:
        filters["askingPrice__lte"] = 0

    if minLease:
        filters["leaseCommenceYear__gte"] = timezone.localdate().year - 99 + minLease


    if queryset is None:
        queryset = Listing.objects.all()

    homes = queryset.public().filter(status="live").filter(**filters)

    if region:
        homes = homes.filter(town__in=REGIONS[region])

    if "\x00" in search:
        homes = homes.none()
    elif search.isdigit() and len(search) == 6:
        homes = homes.filter(postalCode=search)
    elif search.upper() in TOWNS:
        homes = homes.filter(town=search.upper())
    elif estate_of(search):
        homes = homes.filter(street__in=searchData()["areas"][estate_of(search)][1])
    elif roadWords(search) in street_names():
        street = roadWords(search)



        matchingPostals = [
            postal for postal, entry in blocks().items() if entry["s"] == street
        ]

        homes = homes.filter(
            Q(street__iexact=street) | Q(postalCode__in=matchingPostals)
        )
    elif search.casefold().startswith(("blk ", "block ")) and len(search.split()) >= 3:
        _, block, street = roadWords(search).split(maxsplit=2)

        matchingPostals = [
            postal
            for postal, entry in blocks().items()
            if entry["b"].casefold() == block.casefold() and entry["s"] == street
        ]

        homes = homes.filter(
            Q(block__iexact=block, street__iexact=street)
            | Q(postalCode__in=matchingPostals)
        )
    elif search.casefold().startswith(("blk ", "block ")) and len(search.split()) == 2:
        homes = homes.filter(block__iexact=search.split()[1])
    elif search:
        typed = [
            word for word in search.casefold().split() if word not in ("blk", "block")
        ]

        stored = [
            word
            for word in roadWords(search).casefold().split()
            if word not in ("blk", "block")
        ]

        matchingPostals = []

        for postal, entry in blocks().items():
            place = f"{entry['b']} {entry['s']} {entry['t']}".casefold()



            if typed and (
                all(word in place for word in typed)
                or all(word in place for word in stored)
            ):
                matchingPostals.append(postal)

        homes = homes.filter(
            Q(street__icontains=search)
            | Q(street__icontains=roadWords(search))
            | Q(block__icontains=search)
            | Q(postalCode__icontains=search)
            | Q(town__icontains=search)
            | Q(postalCode__in=matchingPostals)
        )

    return rankFlats(homes), {
        "searchQuery": search,
        "towns": TOWNS,
        "flatTypes": FLAT_TYPES,
        "town": town,
        "region": region,
        "flatType": flatType,
        "minPrice": minCost if minCost is not None else "",
        "maxPrice": maxCost if maxCost is not None else "",
        "priceFloor": minAsk,
        "priceCap": priceCap,
        "budgetFloor": minSpend,
        "budgetCap": maxSpend,
        "budgetMin": minCost if minCost is not None else minSpend,
        "budgetMax": maxCost if maxCost is not None else maxSpend,
        "minSqm": minSqm or "",
        "maxSqm": maxSqm or "",
        "bedrooms": beds,
        "bathrooms": baths,
        "balcony": balcony,
        "minLease": minLease,
        "moreFilters": sum(
            bool(value) for value in (minSqm, maxSqm, beds, baths, balcony, minLease)
        ),
        "roomChoices": [1, 2, 3, 4],
        "bathChoices": [1, 2, 3],
        "leaseChoices": LEASE_CHOICES,
    }


def typeCounts(filters, priceCap=None):
    filters = filters.copy()
    filters.pop("flatType", None)
    homes, _ = findHomes(filters, priceCap=priceCap)
    rows = homes.order_by().values("flatType").annotate(total=Count("pk"))
    counts = {row["flatType"]: row["total"] for row in rows}
    choices = [{"value": kind, "total": counts.get(kind, 0)} for kind in FLAT_TYPES]

    return choices, sum(counts.values())


def zoneCounts(filters, priceCap=None):
    filters = filters.copy()
    filters.pop("region", None)
    homes, _ = findHomes(filters, priceCap=priceCap)
    rows = homes.order_by().values("town").annotate(total=Count("pk"))
    counts = {row["town"]: row["total"] for row in rows}

    choices = [
        {"value": name, "total": sum(counts.get(town, 0) for town in towns)}
        for name, towns in REGIONS.items()
    ]

    return choices, sum(counts.values())


def searchName(params):
    parts = [params.get("flatType", "Flats").title().replace(" Room", "-room")]

    if params.get("town"):
        parts.append("in " + params["town"].title())
    elif params.get("region"):
        parts.append("in the " + params["region"])

    if params.get("q"):
        parts.append(f"matching {params['q']}")



    for field, label in (("minPrice", "from "), ("maxPrice", "under ")):
        if params.get(field):
            price = int(params[field])

            if price % 1000 == 0:
                amount = f"${price // 1000:,}k"
            else:
                amount = f"${price:,}"

            parts.append(label + amount)

    for field, label in (
        ("minSqm", "sqm or larger"),
        ("maxSqm", "sqm or smaller"),
        ("bedrooms", "+ bedrooms"),
        ("bathrooms", "+ bathrooms"),
        ("minLease", "+ years left on lease"),
    ):
        if params.get(field):
            value = f"{float(params[field]):g}"
            parts.append(value + (" " if not label.startswith("+") else "") + label)

    if params.get("balcony"):
        parts.append("with a balcony")

    return " ".join(parts)[:200]


@login_required
@require_POST
@transaction.atomic
def saveSearch(request):
    if not hasRole(request.user, "buyer"):
        return notAllowed(request, "buyer")

    lock_user(request.user.pk)
    params = tidySearch(request.POST)


    if SavedSearch.objects.filter(buyer=request.user, queryParams=params).exists():
        messages.success(request, "You already saved this search.")
    else:
        try:
            SavedSearch.objects.create(buyer=request.user, queryParams=params)
        except ValidationError as error:
            messages.error(request, error.messages[0])
        else:
            messages.success(
                request, "Search saved. We will let you know about matching flats."
            )

    return redirect("saved")


@login_required
@require_POST
@transaction.atomic
def editSearch(request, pk):
    if not hasRole(request.user, "buyer"):
        return notAllowed(request, "buyer")

    lock_user(request.user.pk)
    hunt = get_object_or_404(SavedSearch, pk=pk, buyer=request.user)
    action = request.POST.get("action")

    if action == "delete":
        hunt.delete()
        messages.success(request, "Search deleted.")
    elif action in ("pause", "resume"):
        hunt.isActive = action == "resume"
        hunt.save(update_fields=["isActive"])



        messages.success(
            request, "Search resumed." if hunt.isActive else "Search paused."
        )

    return redirect("saved")


def huntAlerts(listingId, kinds):
    flat = Listing.objects.public().filter(pk=listingId, status="live").first()

    if flat is None:
        return

    hunts = (
        SavedSearch.objects.filter(
            isActive=True,
            buyer__is_active=True,
            buyer__is_staff=False,
            buyer__profile__canBuy=True,
        )
        .exclude(buyer_id=flat.seller_id)
        .select_related("buyer")
    )



    for savedSearch in hunts.iterator(chunk_size=100):
        matches, _ = findHomes(
            savedSearch.queryParams,
            Listing.objects.filter(pk=flat.pk),
            priceCap=flat.askingPrice,
            clampPrices=False,
        )

        if not matches.exists():
            continue

        for kind in kinds:
            if kind == "price_drop":
                price = flat.askingPrice
            else:
                price = 0

            with transaction.atomic():
                earlier = SearchAlert.objects.filter(
                    savedSearch=savedSearch, listing=flat, kind=kind
                )

                if (
                    kind == "price_drop"
                    and earlier.filter(askingPrice__lte=price).exists()
                ):
                    continue

                _, created = SearchAlert.objects.get_or_create(
                    savedSearch=savedSearch, listing=flat, kind=kind, askingPrice=price
                )


                if created:
                    searchNote(savedSearch, flat, kind)

                    SavedSearch.objects.filter(pk=savedSearch.pk).update(
                        lastNotifiedAt=timezone.now()
                    )
