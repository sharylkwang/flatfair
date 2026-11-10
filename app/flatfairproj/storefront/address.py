import json
from pretrained import ML as ML_DIR, file_cache
from django.contrib.humanize.templatetags.humanize import ordinal
from .templatetags.estate_tags import flat_label
import csv
import math
import re

BLOCKS_JSON = ML_DIR / "hdb_blocks.json"
SEARCH_LIMIT = 8
STATIONS_CSV = ML_DIR / "mrt_stations.csv"
STATIONS_JSON = ML_DIR / "mrt" / "stations.json"
WALK_FILE = ML_DIR / "mrt" / "block_mrt.json"
DARK_TEXT_LINES = {"CC", "CE", "CR"}


@file_cache(lambda: BLOCKS_JSON)
def blocks():
    return json.loads(BLOCKS_JSON.read_text(encoding="utf-8"))["blocks"]


@file_cache(lambda: BLOCKS_JSON)
def search_index():
    index = []

    for postal, entry in blocks().items():
        words = f"{entry['b']} {entry['s']} {entry['t']}".lower().split()
        index.append((postal, words))

    return index


def search(query):
    words = str(query or "").lower().split()[:6]

    if not words:
        return []

    digits = "".join(words)
    matches = []

    for postal, blockBits in search_index():
        if digits.isdigit() and postal.startswith(digits):
            found = True
        else:
            found = all(
                any(word.startswith(part) for word in blockBits) for part in words
            )

        if found:
            entry = blocks()[postal]



            matches.append(
                {
                    "postal": postal,
                    "block": entry["b"],
                    "label": f"Blk {entry['b']} {entry['s'].title()}",
                    "town": entry["t"],
                    "townLabel": town_label(entry["t"]),
                    "leaseYear": entry.get("y"),
                }
            )

            if len(matches) == SEARCH_LIMIT:
                break

    return matches


def street_names():
    names = set()


    for entry in blocks().values():
        names.add(entry["s"])

    return frozenset(names)


@file_cache(lambda: BLOCKS_JSON)
def hint_index():
    from .valuation import TOWNS

    items, starts = [], {}

    def add(kind, label, value, words):
        index = len(items)
        items.append({"kind": kind, "label": label, "value": value})

        for word in set(words.casefold().replace("/", " ").split()):
            for length in range(1, len(word) + 1):
                starts.setdefault(word[:length], set()).add(index)

    for town in TOWNS:
        add("town", town_label(town), town, town)

    for street in sorted(street_names()):
        add("street", street.title(), street, street)

    for postal, entry in sorted(blocks().items()):
        label = f"Blk {entry['b']} {entry['s'].title()}"
        add("block", label, label, f"{entry['b']} {entry['s']} {entry['t']}")


        add("postal", f"{label} · {postal}", postal, postal)

    return items, starts


def search_hints(query):
    words = str(query or "").strip().casefold().replace("/", " ").split()[:6]
    words = [word for word in words if word not in ("blk", "block")]

    if len(" ".join(words)) < 2:
        return []


    items, starts = hint_index()
    matches = set.intersection(*(starts.get(word, set()) for word in words))



    ordered = sorted(matches)

    if "".join(words).isdigit():
        postals = [index for index in ordered if items[index]["kind"] == "postal"]
        addresses = [index for index in ordered if items[index]["kind"] != "postal"]
        ordered = postals[:4] + addresses[:4] + postals[4:] + addresses[4:]

    return [items[index] for index in ordered[:SEARCH_LIMIT]]


def town_label(town):
    return town.title().replace("/", " / ")


def get_postal(postal):
    postal = str(postal or "").strip()
    entry = blocks().get(postal)



    if entry is None:
        return None

    kinds = []


    for kind, (low_area, top_area, models) in (entry.get("x") or {}).items():
        kinds.append(
            {
                "type": kind,
                "label": flat_label(kind),
                "sqm": [low_area, top_area],
                "models": models,
            }
        )

    return {
        "label": f"Blk {entry['b']} {entry['s'].title()}",
        "postal": postal,
        "block": entry["b"],
        "town": entry["t"],
        "townLabel": town_label(entry["t"]),
        "lat": entry["la"],
        "lon": entry["lo"],
        "leaseYear": entry.get("y"),
        "storeys": entry.get("f"),
        "hasSales": bool(entry.get("x")),
        "flatTypes": kinds,
    }


def not_found(postal):
    return f"We couldn't find an HDB block with postal code {postal}. Check the number on your HDB papers."


def area_range(kind):
    low_area, top_area = kind["sqm"]
    margin = max(8, top_area * 0.1)

    return low_area - margin, top_area + margin


def check(
    postal, town=None, block=None, flatType=None, area=None, lease=None, storey=None
):
    errors, warns, fixes = {}, {}, {}
    record = get_postal(postal)

    if record is None:
        errors["postal"] = not_found(postal)

        return None, errors, warns, fixes

    where = record["label"]

    if not town:
        fixes["town"] = record["town"]
    elif town != record["town"]:
        errors["town"] = (
            f"Postal code {postal} is {where} in {record['townLabel']}, not {town_label(town)}. "
            "Change the town or the postal code."
        )

    if block is not None:
        given = str(block).strip().upper()

        if not given:
            fixes["block"] = record["block"]
        elif given != record["block"]:
            errors["block"] = (
                f"Postal code {postal} is Blk {record['block']}, not Blk {given}. "
                "Check the block number or the postal code."
            )

    kinds = {kind["type"]: kind for kind in record["flatTypes"]}
    kind = kinds.get(flatType)

    if flatType and record["hasSales"] and kind is None:
        labels = [flat_type["label"] for flat_type in record["flatTypes"]]

        if len(labels) == 1:
            sold = labels[0]
        else:
            sold = ", ".join(labels[:-1]) + " and " + labels[-1]

        warns["flatType"] = (
            f"HDB records show only {sold} flats sold at {where}, not {flat_label(flatType)}. "
            "Please check the flat type."
        )


    if kind and area is not None:
        low, high = area_range(kind)

        if not low <= float(area) <= high:
            low_area, top_area = kind["sqm"]

            if low_area == top_area:
                span = f"about {low_area:g} sqm"
            else:
                span = f"{low_area:g} to {top_area:g} sqm"

            errors["area"] = (
                f"{kind['label'].capitalize()} flats at {where} are {span}, "
                f"so {float(area):g} sqm looks wrong. Check your HDB papers or change the flat type."
            )

    if (
        lease is not None
        and record["leaseYear"]
        and abs(int(lease) - record["leaseYear"]) > 1
    ):
        errors["lease"] = (
            f"The lease at {where} started in {record['leaseYear']}, not {lease}. "
            f"Use {record['leaseYear']}, or check the postal code."
        )

    if (
        storey is not None
        and record["storeys"]
        and int(storey) > record["storeys"][1] + 3
    ):
        warns["storey"] = (
            f"The highest flat sold at {where} was on the {ordinal(record['storeys'][1])} floor, "
            f"so the {ordinal(storey)} floor looks too high. Please check it."
        )



    return record, errors, warns, fixes


def stations():
    rows = []

    with open(STATIONS_CSV, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append((row["name"], float(row["lat"]), float(row["lon"])))

    return rows


@file_cache(lambda: (STATIONS_JSON, WALK_FILE))
def walk_index():
    try:
        stopData = json.loads(STATIONS_JSON.read_text(encoding="utf-8"))


        index = json.loads(WALK_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"stations": [], "lines": {}, "blocks": {}, "byBlock": {}}

    byId = {}


    for station in stopData["stations"]:
        byId[station["id"]] = station

    ordered = []


    for stopId in index["stationIds"]:
        ordered.append(byId[stopId])

    return {
        "stations": ordered,
        "lines": stopData["lines"],
        "blocks": index["blocks"],
        "byBlock": index["byBlock"],
    }


def distance(lat, lon, lat2, lon2):
    lat_one, lon_one, lat_two, lon_two = (
        math.radians(lat),
        math.radians(lon),
        math.radians(lat2),
        math.radians(lon2),
    )


    arc = (
        math.sin((lat_two - lat_one) / 2) ** 2
        + math.cos(lat_one) * math.cos(lat_two) * math.sin((lon_two - lon_one) / 2) ** 2
    )

    return 6371000 * 2 * math.asin(math.sqrt(arc))


def nearest_station(postal):
    record = get_postal(postal)

    if record is None:
        return None

    best = None

    for name, lat, lon in stations():
        metres = distance(record["lat"], record["lon"], lat, lon)

        if best is None or metres < best["metres"]:
            best = {"name": name, "metres": metres}

    best["metres"] = int(round(best["metres"], -1))

    return best


def line_tags(codes, lines):
    lrt_codes = {"STC": "SK", "SE": "SK", "SW": "SK", "PTC": "PG", "PE": "PG", "PW": "PG"}
    colours = {"#009645": "#007A38", "#748477": "#5F6D62"}
    badges = []

    for code in codes:
        line = re.match(r"[A-Z]+", code).group()
        line = lrt_codes.get(line, line)
        meta = lines.get(line, {})
        colour = meta.get("colour", "#748477").upper()
        colour = colours.get(colour, colour)
        text = "#FFFFFF"

        if line in DARK_TEXT_LINES:
            text = "#1B2A3A"


        badges.append(
            {"code": code, "lineName": meta.get("name", line), "colour": colour, "text": text}
        )

    return badges


def walk_stop(postal, block="", town=""):
    index = walk_index()
    row = index["blocks"].get(str(postal or "").strip())

    if row is None and block and town:
        row = index["byBlock"].get(f"{town.strip().upper()}|{block.strip().upper()}")



    if row is None:
        return None

    station = index["stations"][row[0]]
    wayOut = ""

    if row[2] >= 0:
        wayOut = station["exits"][row[2]]["code"]

    return {
        "name": f"{station['name']} {station['kind']}",
        "metres": int(round(row[1], -1)),
        "minutes": max(1, round(row[1] / 80)),
        "exit": wayOut,
        "badges": line_tags(station["codes"], index["lines"]),
        "walking": True,
    }


def nearest_mrt(postal, block="", town=""):
    walk = walk_stop(postal, block, town)

    if walk is not None:
        return walk

    return nearest_station(postal)
