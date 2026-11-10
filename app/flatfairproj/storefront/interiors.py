import json
import re
import shutil
from pretrained import ASSETS
from math import sqrt
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.utils.text import slugify

from .models import Listing, ListingPhoto

PHOTO_MAP = ASSETS / "seed" / "photos.json"
PHOTO_DIR = ASSETS / "seed" / "photos"
FALLBACK_COVER = "storefront/estate/fallback.jpg"

CENTROIDS = ASSETS / "static" / "storefront" / "map" / "sg_centroids.json"

OUTLINES = CENTROIDS.parent / "sg_planning_areas.svg"

ALIASES = {"KALLANG/WHAMPOA": "KALLANG", "CENTRAL AREA": "DOWNTOWN CORE"}
TOWN_AREAS = {
    "CENTRAL AREA": [
        "DOWNTOWN CORE",
        "MARINA EAST",
        "MARINA SOUTH",
        "MUSEUM",
        "NEWTON",
        "ORCHARD",
        "OUTRAM",
        "RIVER VALLEY",
        "ROCHOR",
        "SINGAPORE RIVER",
        "STRAITS VIEW",
    ],
    "KALLANG/WHAMPOA": ["KALLANG", "NOVENA"],
}
NEARBY_AREAS = {
    "BUKIT BATOK": ["TENGAH"],
    "BUKIT MERAH": ["SOUTHERN ISLANDS"],
    "BUKIT PANJANG": ["CENTRAL WATER CATCHMENT"],
    "BUKIT TIMAH": ["TANGLIN"],
    "CHOA CHU KANG": ["LIM CHU KANG", "SUNGEI KADUT"],
    "HOUGANG": ["PAYA LEBAR"],
    "JURONG WEST": [
        "BOON LAY",
        "PIONEER",
        "TUAS",
        "WESTERN ISLANDS",
        "WESTERN WATER CATCHMENT",
    ],
    "PASIR RIS": ["CHANGI", "CHANGI BAY", "NORTH-EASTERN ISLANDS"],
    "SENGKANG": ["SELETAR"],
    "WOODLANDS": ["MANDAI"],
    "YISHUN": ["SIMPANG"],
}
LABEL_POINTS = {
    "ANG MO KIO": [483.1, 175.4],
    "BEDOK": [675.7, 313.2],
    "BISHAN": [480.9, 232.9],
    "BUKIT BATOK": [305.0, 242.8],
    "BUKIT MERAH": [446.8, 405.6],
    "BUKIT PANJANG": [359.5, 247.8],
    "BUKIT TIMAH": [386.2, 285.2],
    "CENTRAL AREA": [506.4, 371.8],
    "CHOA CHU KANG": [295.0, 185.3],
    "CLEMENTI": [320.0, 328.3],
    "GEYLANG": [586.8, 301.5],
    "HOUGANG": [593.9, 204.1],
    "JURONG EAST": [261.6, 332.8],
    "JURONG WEST": [201.4, 276.5],
    "KALLANG/WHAMPOA": [508.3, 315.4],
    "MARINE PARADE": [605.2, 350.2],
    "PASIR RIS": [729.3, 202.2],
    "PUNGGOL": [626.3, 136.1],
    "QUEENSTOWN": [379.4, 367.0],
    "SEMBAWANG": [439.5, 36.0],
    "SENGKANG": [593.0, 161.8],
    "SERANGOON": [546.2, 253.1],
    "TAMPINES": [705.3, 250.8],
    "TOA PAYOH": [513.0, 278.5],
    "WOODLANDS": [378.7, 60.1],
    "YISHUN": [472.4, 136.1],
}
REGIONS = {
    "North": ["SEMBAWANG", "WOODLANDS", "YISHUN"],
    "North-East": ["ANG MO KIO", "HOUGANG", "PUNGGOL", "SENGKANG", "SERANGOON"],
    "East": ["BEDOK", "PASIR RIS", "TAMPINES"],
    "West": [
        "BUKIT BATOK",
        "BUKIT PANJANG",
        "CHOA CHU KANG",
        "CLEMENTI",
        "JURONG EAST",
        "JURONG WEST",
        "TENGAH",
    ],
    "Central": [
        "BISHAN",
        "BUKIT MERAH",
        "BUKIT TIMAH",
        "CENTRAL AREA",
        "GEYLANG",
        "KALLANG/WHAMPOA",
        "MARINE PARADE",
        "QUEENSTOWN",
        "TOA PAYOH",
    ],
}


def demoPhotos():
    return json.loads(PHOTO_MAP.read_text(encoding="utf-8"))


def picsReady():
    for rooms in demoPhotos().values():
        for photo in rooms.values():
            if not (PHOTO_DIR / photo["file"]).exists():
                return False

    return True


def seedPics(flat):
    rooms = demoPhotos().get(flat.postalCode)

    if not rooms:
        return

    folder = Path(settings.MEDIA_ROOT) / "demo" / str(flat.publicId)
    folder.mkdir(parents=True, exist_ok=True)
    flat.photos.exclude(room__in=list(rooms)).delete()

    for room, photo in rooms.items():
        target = folder / photo["file"]

        if not target.exists():
            shutil.copyfile(PHOTO_DIR / photo["file"], target)

        ListingPhoto.objects.update_or_create(
            listing=flat,
            room=room,
            defaults={
                "image": f'demo/{flat.publicId}/{photo["file"]}',
                "aiLabel": photo["label"],
                "aiIrrelevant": photo["irrelevant"],
                "aiGenerated": photo["aiGenerated"],
                "createdAt": flat.createdAt,
            },
        )


def picCredits():
    path = finders.find(("storefront/photos/credits.json"))

    if not path:
        return {"note": "", "photos": []}

    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def townCover(town):
    slug = slugify(str(town).replace("/", " "))
    path = f"storefront/towns/{slug}.jpg"

    return path if finders.find(path) else FALLBACK_COVER


@lru_cache(maxsize=64)
def townPhotos(slug):
    found = [f"storefront/towns/{slug}.jpg"] if finders.find(f"storefront/towns/{slug}.jpg") else []
    number = 2

    while finders.find(f"storefront/towns/{slug}-{number}.jpg"):
        found.append(f"storefront/towns/{slug}-{number}.jpg")
        number += 1

    return tuple(found)


def estatePic(flat):
    photos = townPhotos(slugify(str(flat.town).replace("/", " ")))

    if not photos:
        return FALLBACK_COVER

    return photos[(flat.pk or 0) % len(photos)]


def regionName(value):
    wanted = str(value or "").strip().casefold()

    for name in REGIONS:
        if name.casefold() == wanted:
            return name

    return ""


def townAreas(town):
    return TOWN_AREAS.get(town, [town])


def nearAreas(town):
    return NEARBY_AREAS.get(town, [])


def townPoint(town):
    return LABEL_POINTS.get(town) or centroids().get(ALIASES.get(town, town))


@file_cache(lambda: OUTLINES)
def areaPaths():
    return dict(re.findall(r'<path data-area="([^"]+)" d="([^"]+)"', OUTLINES.read_text()))


@file_cache(lambda: OUTLINES)
def mapBounds():
    across = []
    down = []

    for path in areaPaths().values():
        numbers = [float(number) for number in re.findall(r"-?\d+\.?\d*", path)]
        across += numbers[0::2]
        down += numbers[1::2]

    width = max(across) - min(across)
    height = max(down) - min(down)
    edge = 0.02 * max(width, height)

    return [
        round(min(across) - edge, 1),
        round(min(down) - edge, 1),
        round(width + 2 * edge, 1),
        round(height + 2 * edge, 1),
    ]


@file_cache(lambda: CENTROIDS)
def centroids():
    return json.loads(CENTROIDS.read_text())["centroids"]


def mapDots(liveTowns):
    biggest = 1

    for town in liveTowns:
        biggest = max(biggest, town["count"])

    dots = []

    for town in liveTowns:
        point = centroids().get(ALIASES.get(town["town"], town["town"]))

        if point:
            share = sqrt(town["count"] / biggest)

            dots.append(
                {
                    "town": town["town"],
                    "count": town["count"],
                    "x": point[0],
                    "y": point[1],
                    "r": round(10 + 8 * share, 1),
                }
            )

    return dots


def roomPhotos():
    picked = []

    for room in ("kitchen", "master_bedroom", "bedroom_2"):
        photo = (
            ListingPhoto.objects.filter(
                listing__in=Listing.objects.public().filter(status="live"), room=room
            )
            .exclude(image="")
            .exclude(listing__in=[chosen.listing_id for chosen in picked])
            .select_related("listing")
            .order_by("pk")
            .first()
        )

        if photo:
            picked.append(photo)

    return picked
