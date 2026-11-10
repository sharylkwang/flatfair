import json
import math
import time
import urllib.parse
import urllib.request
from django.core.management.base import BaseCommand, CommandError
from storefront.address import blocks
from storefront import floorplan
from storefront.models import Listing


def to_metres(points, lat0, lon0):
    metres = []

    for lat, lon in points:
        x_spot = (lon - lon0) * 111320 * math.cos(math.radians(lat0))
        y_spot = (lat - lat0) * 111320
        metres.append((x_spot, y_spot))

    return metres


def contains(poly, x, y):
    inside = False

    for index in range(len(poly)):
        start_x, start_y = poly[index]
        end_x, end_y = poly[(index + 1) % len(poly)]

        if (start_y > y) != (end_y > y) and x < (end_x - start_x) * (y - start_y) / (
            end_y - start_y
        ) + start_x:
            inside = not inside

    return inside


def blockShape(outline, lat, lon):
    points = to_metres(outline, lat, lon)


    best = None

    for degrees in range(180):
        angle = math.radians(degrees)
        alongs = [
            horizontal_position * math.sin(angle) + vertical_position * math.cos(angle)
            for horizontal_position, vertical_position in points
        ]
        acrosses = [
            horizontal_position * math.cos(angle) - vertical_position * math.sin(angle)
            for horizontal_position, vertical_position in points
        ]



        length = max(alongs) - min(alongs)
        depth = max(acrosses) - min(acrosses)

        if best is None or length * depth < best[0]:
            best = (length * depth, degrees, length, depth)



    area, degrees, length, depth = best

    if depth > length:
        degrees = (degrees + 90) % 180



        length, depth = depth, length

    ratio = length / max(depth, 1)



    form = "other"

    if ratio >= 2.2 and length >= 40:
        form = "slab"
    elif ratio < 1.7 and length < 45:
        form = "point"

    return {"form": form, "axis": degrees, "length": round(length, 1), "depth": round(depth, 1)}


def get_blocks(lat, lon):
    query = f'[out:json][timeout:25];way(around:30,{lat},{lon})["building"];out geom;'
    data = urllib.parse.urlencode({"data": query}).encode()
    request = urllib.request.Request(
        "https://overpass-api.de/api/interpreter",
        data=data,
        headers={"User-Agent": "FlatFair-coursework/1.0"},
    )

    with urllib.request.urlopen(request, timeout=40) as reply:
        return json.loads(reply.read())["elements"]


def pickBlock(ways, lat, lon):
    best = None

    for way in ways:
        outline = [(point["lat"], point["lon"]) for point in way.get("geometry", [])]

        if len(outline) < 4:
            continue

        poly = to_metres(outline, lat, lon)
        middleX = sum(horizontal_position for horizontal_position, vertical_position in poly) / len(
            poly
        )
        middleY = sum(vertical_position for horizontal_position, vertical_position in poly) / len(
            poly
        )
        score = math.hypot(middleX, middleY)



        if contains(poly, 0, 0):
            score = 0

        if best is None or score < best[0]:
            best = (score, way["id"], outline)

    return best


class Command(BaseCommand):
    help = "Saves the OpenStreetMap block shape of listed flats for the floor plan engine."

    def add_arguments(self, parser):
        parser.add_argument("--postal", action="append", default=[])
        parser.add_argument("--refresh", action="store_true")

    def handle(self, *args, **options):
        if not floorplan.PLANS_ON:
            raise CommandError("Floor plans are turned off.")

        path = floorplan.BLOCK_SHAPES_PATH


        try:
            cache = json.loads(path.read_text())
        except (OSError, ValueError):
            cache = {}

        cache["v"] = 1
        cache["source"] = "OpenStreetMap contributors via Overpass API"
        cache["licence"] = "ODbL 1.0"
        cache["about"] = (
            "Per HDB postal code: footprint form (slab/point/other), long-axis bearing, size in m."
        )
        shapes = cache.setdefault("blocks", {})
        postals = options["postal"]

        if not postals:
            listed = Listing.objects.exclude(postalCode="").order_by("postalCode")
            postals = list(listed.values_list("postalCode", flat=True).distinct())

        known = blocks()

        for postal in postals:
            if postal in shapes and not options["refresh"]:
                continue

            entry = known.get(postal)


            if not entry:
                self.stdout.write(f"{postal}: not an HDB block, skipped")

                continue

            lat = float(entry["la"])
            lon = float(entry["lo"])

            try:
                picked = pickBlock(get_blocks(lat, lon), lat, lon)
            except (OSError, ValueError, KeyError) as error:
                self.stdout.write(f"{postal}: Overpass failed ({error}), skipped")
                time.sleep(5)

                continue

            if picked is None or picked[0] > 40:
                self.stdout.write(f"{postal}: no building outline found, skipped")
            else:
                shape = blockShape(picked[2], lat, lon)


                shape["osm"] = picked[1]


                shapes[postal] = shape
                self.stdout.write(
                    f"{postal} blk {entry['b']}: {shape['form']} {shape['length']}x{shape['depth']} m"
                )



            time.sleep(2)


        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n")
        self.stdout.write(self.style.SUCCESS(f"{len(shapes)} block shapes saved"))
