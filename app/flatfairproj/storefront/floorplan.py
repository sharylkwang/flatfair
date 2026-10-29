import base64
import json
import math
from hashlib import sha256
from html import escape
from pathlib import Path
import io
from PIL import Image, ImageDraw, ImageFont
from django.http import FileResponse, Http404, HttpResponse, HttpResponseNotModified
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_GET
from adminpanel.access import canOpen
from pretrained import ML
from .models import Listing, OrderItem, FLAT_RULES

PLAN_WIDTH = 900
PLAN_HEIGHT = 760
PLANS_ON = True
RENDER_VERSION = "flatfair-1"
BLOCK_SHAPES_PATH = ML / "block_shapes.json"


# Plan where rooms go
def planRooms(flatType, bedrooms, bathrooms, area):
    ratio = 1.45
    hall = 1.2

    if area < 60:
        ratio = 1.0
        hall = 1.0
    elif area < 90:
        ratio = 1.2

    depth = (area / ratio) ** 0.5
    width = area / depth
    top = max(0.42 * depth, 3.0)
    weights = [1.5, 1.15, 1.0, 0.95, 0.9]
    columns = []

    if bedrooms <= 1:
        columns.append(("Bedroom", 1.0))
        columns.append(("Dining", 0.9))

    for number in range(1, bedrooms + 1):
        if bedrooms > 1:
            name = "Master bedroom" if number == 1 else "Bedroom " + str(number)
            columns.append((name, weights[min(number - 1, 4)]))

    if flatType == "EXECUTIVE":
        columns.append(("Study", 0.75))

    for number in range(2, bathrooms):
        columns.append(("Toilet " + str(number), 0.55))

    total = 0

    for name, weight in columns:
        total += weight

    rooms = []
    left = 0

    for name, weight in columns:
        size = width * weight / total
        rooms.append(
            {
                "name": name,
                "box": (left, 0, left + size, top),
                "door": ("h", left + size - 1.1, top, -1),
                "window": "top",
            }
        )
        left += size

    if columns[1][0] == "Dining":
        rooms[1]["door"] = None
        rooms[1]["open"] = "bottom"

    master = rooms[0]["box"]

    if bathrooms >= 2:
        bathWide = min(1.8, 0.4 * (master[2] - master[0]))
        rooms[0]["labelBox"] = (master[0], master[1], master[2] - bathWide, master[3])

        rooms.append(
            {
                "name": "Master toilet",
                "box": (master[2] - bathWide, 0, master[2], 2.0),
                "door": ("v", master[2] - bathWide, 0.9, -1),
                "window": "top",
            }
        )

    living = 0.48 * width
    cookLeft = 0.68 * width
    below = top + hall
    middle = below + (depth - below) * 0.5
    door = ("h", living - 1.4, depth, -1)

    if flatType in ("5 ROOM", "EXECUTIVE"):
        split = top + (depth - top) * 0.4
        rooms.append(
            {
                "name": "Dining",
                "box": (0, top, living, split),
                "door": None,
                "window": "left",
                "open": "right",
            }
        )
        rooms.append(
            {
                "name": "Living",
                "box": (0, split, living, depth),
                "door": door,
                "window": "left",
                "open": "top",
            }
        )
    elif bedrooms <= 1:
        rooms.append(
            {
                "name": "Living",
                "box": (0, top, living, depth),
                "door": door,
                "window": "left",
                "open": "right",
            }
        )
    else:
        rooms.append(
            {
                "name": "Living and dining",
                "box": (0, top, living, depth),
                "door": door,
                "window": "left",
                "open": "right",
            }
        )

    rooms.append(
        {"name": "Corridor", "box": (living, top, width, below), "door": None, "window": None}
    )
    rooms.append(
        {
            "name": "Common toilet",
            "box": (living, below, cookLeft, middle),
            "door": ("h", cookLeft - 0.9, below, 1),
            "window": None,
        }
    )
    rooms.append(
        {
            "name": "Household shelter",
            "box": (living, middle, cookLeft, depth),
            "door": ("v", living, depth - 1.1, 1),
            "window": None,
        }
    )
    yardWide = min(1.6, 0.4 * (width - cookLeft))
    yardTop = depth - min(2.6, 0.5 * (depth - below))

    rooms.append(
        {
            "name": "Kitchen",
            "box": (cookLeft, below, width, depth),
            "door": ("h", width - 1.0, below, 1),
            "window": "right",
            "labelBox": (cookLeft, below, width, yardTop),
        }
    )

    rooms.append(
        {
            "name": "Service yard",
            "box": (width - yardWide, yardTop, width, depth),
            "door": ("v", width - yardWide, yardTop + 0.3, -1),
            "window": "bottom",
        }
    )

    return width, depth, rooms


def room_Area(room, rooms):
    inner_rooms = {
        "Master bedroom": "Master toilet",
        "Bedroom": "Master toilet",
        "Kitchen": "Service yard",
    }

    left, top, right, bottom = room["box"]
    size = (right - left) * (bottom - top)
    inner = inner_rooms.get(room["name"])

    for other in rooms:
        if other["name"] == inner:
            size -= (other["box"][2] - other["box"][0]) * (other["box"][3] - other["box"][1])

    return size


def add_window(draw, room, left, top, right, bottom):
    across = (right - left) * 0.3
    down = (bottom - top) * 0.3

    if room["window"] == "top":
        draw.rectangle(
            (left + across, top - 5, right - across, top + 5),
            fill="white",
            outline="black",
            width=2,
        )

    if room["window"] == "bottom":
        draw.rectangle(
            (left + across, bottom - 5, right - across, bottom + 5),
            fill="white",
            outline="black",
            width=2,
        )

    if room["window"] == "left":
        draw.rectangle(
            (left - 5, top + down, left + 5, bottom - down), fill="white", outline="black", width=2
        )

    if room["window"] == "right":
        draw.rectangle(
            (right - 5, top + down, right + 5, bottom - down),
            fill="white",
            outline="black",
            width=2,
        )


def drawDoor(draw, door, hingeX, hingeY, leaf):
    kind, x_spot, y_spot, side = door

    if kind == "h":
        draw.rectangle((hingeX, hingeY - 5, hingeX + leaf, hingeY + 5), fill="white")
        draw.line((hingeX, hingeY, hingeX, hingeY + leaf * side), fill="black", width=2)
        start, end = (0, 90) if side > 0 else (270, 360)
    else:
        draw.rectangle((hingeX - 5, hingeY, hingeX + 5, hingeY + leaf), fill="white")
        draw.line((hingeX, hingeY, hingeX + leaf * side, hingeY), fill="black", width=2)
        start, end = (0, 90) if side > 0 else (90, 180)

    draw.arc(
        (hingeX - leaf, hingeY - leaf, hingeX + leaf, hingeY + leaf),
        start,
        end,
        fill="#777777",
        width=2,
    )


# Draw the room plan
def drawPlan(flatType, bedrooms, bathrooms, area):
    width, depth, rooms = planRooms(flatType, bedrooms, bathrooms, area)
    image = Image.new("RGB", (PLAN_WIDTH, PLAN_HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    title = ImageFont.load_default(size=26)
    label = ImageFont.load_default(size=20)
    small = ImageFont.load_default(size=16)
    scale = min((PLAN_WIDTH - 80) / width, 540 / depth)
    originX = (PLAN_WIDTH - width * scale) / 2
    originY = 80

    def point(x, y):
        return (originX + x * scale, originY + y * scale)

    kind = flatType.title().replace(" Room", "-room")
    draw.text((40, 28), f"{kind} flat · about {area} sqm", fill="black", font=title)

    for room in rooms:
        left, top = point(room["box"][0], room["box"][1])
        right, bottom = point(room["box"][2], room["box"][3])
        fill = (
            "#f4f6fb"
            if room["name"] in (("Corridor", "Service yard", "Household shelter"))
            else "white"
        )
        draw.rectangle((left, top, right, bottom), fill=fill, outline="black", width=4)

        if room["name"] == "Corridor":
            hallLow = room["box"][3]

    mapLeft, planTop = point(0, 0)
    mapRight, mapLow = point(width, depth)
    draw.rectangle((mapLeft, planTop, mapRight, mapLow), outline="black", width=8)

    for room in rooms:
        left, top = point(room["box"][0], room["box"][1])
        right, bottom = point(room["box"][2], room["box"][3])
        add_window(draw, room, left, top, right, bottom)

        if room["door"]:
            hingeX, hingeY = point(room["door"][1], room["door"][2])
            drawDoor(draw, room["door"], hingeX, hingeY, 0.8 * scale)

        if room["name"] in ("Living", "Living and dining"):
            doorX, doorY = point(room["door"][1] + 0.4, depth)
            draw.text((doorX, doorY + 22), "Main door", fill="black", font=small, anchor="mm")

        if room.get("open") == "right":
            openLow = point(0, hallLow)[1]
            draw.rectangle((right - 5, top + 5, right + 5, openLow - 5), fill="white")

        if room.get("open") == "bottom":
            draw.rectangle(
                (left + (right - left) * 0.2, bottom - 4, right - (right - left) * 0.2, bottom + 4),
                fill="white",
            )

        if room.get("open") == "top":
            draw.rectangle(
                (left + (right - left) * 0.2, top - 4, right - (right - left) * 0.2, top + 4),
                fill="white",
            )

    for room in rooms:
        box = room.get("labelBox", room["box"])
        left, top = point(box[0], box[1])
        right, bottom = point(box[2], box[3])
        font = label
        lines = [room["name"]]

        if (
            draw.textlength(room["name"], font=label) > right - left - 12
            or room_Area(room, rooms) < 7
        ):
            font = small
            lines = room["name"].split(" ", 1)

            if draw.textlength(lines[0], font=small) > right - left - 6:
                lines = room["name"].split(" ")

        if room["name"] != "Corridor":
            lines.append(f"{room_Area(room, rooms):.0f} sqm")

        text = "\n".join(lines)
        draw.multiline_text(
            ((left + right) / 2, (top + bottom) / 2),
            text,
            fill="black",
            font=font,
            anchor="mm",
            align="center",
        )

    barY = mapLow + 62
    draw.line((40, barY, 40 + 5 * scale, barY), fill="black", width=3)

    for metre in range(6):
        draw.line(
            (40 + metre * scale, barY - 8, 40 + metre * scale, barY + 8), fill="black", width=2
        )

    draw.text((40, barY + 14), "0", fill="black", font=small)
    draw.text((40 + 5 * scale, barY + 14), "5 m", fill="black", font=small, anchor="ma")
    draw.text(
        (40, PLAN_HEIGHT - 34),
        ("Illustration based on the flat type and size, not the actual plan"),
        fill="#555555",
        font=small,
    )

    return image


@lru_cache(maxsize=200)
def planPng(flatType, bedrooms, bathrooms, area):
    buffer = io.BytesIO()
    drawPlan(flatType, bedrooms, bathrooms, area).save(buffer, "PNG", optimize=True)

    return buffer.getvalue()


def sellerPlan(flat):
    items = OrderItem.objects.filter(
        listing=flat,
        name="Floor plan",
        order__seller_id=flat.seller_id,
        order__status="completed",
        deliveryFile__iendswith=".pdf",
    )

    return items.order_by("-pk").first()


def shownFlat(request, publicId):
    flat = get_object_or_404(Listing, publicId=publicId)

    if Listing.objects.public().filter(pk=flat.pk).exists():
        return flat, True

    isOwner = request.user.is_authenticated and flat.seller_id == request.user.pk

    if not isOwner and not canOpen(request.user, "admin_queue") and flat.status != "paused":
        raise Http404

    return flat, False


@require_GET
def floor_plan(request, publicId):
    flat, public = shownFlat(request, publicId)
    data = planPng(flat.flatType, flat.bedrooms, flat.bathrooms, round(flat.floorAreaSqm))
    reply = HttpResponse(data, content_type="image/png")
    reply["Cache-Control"] = "public, max-age=3600" if public else "private, no-store"

    return reply


@require_GET
def seller_floor_plan(request, publicId):
    flat, public = shownFlat(request, publicId)
    item = sellerPlan(flat)

    if not item:
        raise Http404

    try:
        reply = FileResponse(
            item.deliveryFile.open("rb"),
            content_type="application/pdf",
            filename=item.deliveryFileName,
        )
    except (OSError, ValueError):
        raise Http404

    reply["Cache-Control"] = "private, no-store"
    reply["X-Content-Type-Options"] = "nosniff"

    return reply


def plan_data(flat):
    data = {}
    for field, default in [
        ("flatType", "4 ROOM"),
        ("flatModel", ""),
        ("floorAreaSqm", 92),
        ("bedrooms", 3),
        ("bathrooms", 2),
        ("leaseCommenceYear", 0),
        ("hasBalcony", False),
        ("block", ""),
        ("postalCode", ""),
        ("storey", 1),
    ]:
        data[field] = (
            flat.get(field, default) if isinstance(flat, dict) else getattr(flat, field, default)
        )
    if data["flatType"] not in FLAT_RULES:
        data["flatType"] = "4 ROOM"
    data["bedrooms"] = numberFrom(data, "bedrooms", 3, 1, 5)
    data["bathrooms"] = numberFrom(data, "bathrooms", 2, 1, 4)
    data["leaseCommenceYear"] = numberFrom(data, "leaseCommenceYear", 0, 0, 2200)
    data["storey"] = numberFrom(data, "storey", 1, 1, 100)
    try:
        area = float(data["floorAreaSqm"])
        if not math.isfinite(area):
            area = 92
    except (TypeError, ValueError, OverflowError):
        area = 92
    data["floorAreaSqm"] = min(220, max(30, area))
    data["hasBalcony"] = str(data["hasBalcony"]).lower() not in ("", "0", "no", "false", "off")
    for field in ("flatModel", "block", "postalCode"):
        data[field] = str(data[field] or "")
    return data


def fits_plan(spec):
    low, high = FLAT_RULES[spec["flatType"]]["sqm"]
    if not low <= spec["floorAreaSqm"] <= high:
        return False
    if spec["flatType"] == "2 ROOM" and spec["bedrooms"] > 1:
        return False
    return True


def plan_caption(spec):
    kind = spec["flatType"].replace(" ROOM", "-room")
    size = f'{spec["floorAreaSqm"]:g} sqm'
    if not fits_plan(spec):
        return f"{kind} · {size}"
    label = spec["flatModel"] or kind
    if spec["leaseCommenceYear"]:
        decade = spec["leaseCommenceYear"] // 10 * 10
        label = f"{decade}s {label}"
    return f"{label} · {size} · typical layout"


def plan_hash(spec):
    text = json.dumps(spec, sort_keys=True)
    key = sha256(text.encode()).hexdigest()[:16]
    return key + "-r" + RENDER_VERSION + "-sqm"


def planSvg(spec):
    png = planPng(
        spec["flatType"], spec["bedrooms"], spec["bathrooms"], round(spec["floorAreaSqm"])
    )
    image = base64.b64encode(png).decode()
    title = escape(plan_caption(spec))
    width, depth, rooms = planRooms(
        spec["flatType"], spec["bedrooms"], spec["bathrooms"], spec["floorAreaSqm"]
    )
    areas = []
    for room in rooms:
        size = room_Area(room, rooms)
        areas.append(f'{room["name"]}: {size:.1f} sqm')
    balcony = "yes" if spec["hasBalcony"] else "no"
    detail = escape(
        f'Total {spec["floorAreaSqm"]:g} sqm. Balcony listed: {balcony}. ' + "; ".join(areas)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{PLAN_WIDTH}" height="{PLAN_HEIGHT}" '
        f'viewBox="0 0 {PLAN_WIDTH} {PLAN_HEIGHT}" role="img" aria-labelledby="title detail">'
        f'<title id="title">{title}</title><desc id="detail">{detail}</desc>'
        f'<image width="{PLAN_WIDTH}" height="{PLAN_HEIGHT}" href="data:image/png;base64,{image}"/>'
        "</svg>"
    )


def planTitle(flat):
    if not PLANS_ON:
        return ""
    return plan_caption(plan_data(flat)).removesuffix(" · typical layout")


def planUrl(flat):
    if not PLANS_ON:
        return reverse("floor_plan", args=[flat.publicId])
    url = reverse("listing_floor_plan", args=[flat.publicId])
    return url + "?v=" + plan_hash(plan_data(flat))


def svgReply(spec, public, versioned, notModified=False):
    reply = (
        HttpResponseNotModified()
        if notModified
        else HttpResponse(planSvg(spec), content_type="image/svg+xml; charset=utf-8")
    )
    reply["ETag"] = '"fp-' + plan_hash(spec) + '"'

    if not public:
        reply["Cache-Control"] = "private, no-store"
    elif versioned:
        reply["Cache-Control"] = "public, max-age=604800, immutable"
    else:
        reply["Cache-Control"] = "public, max-age=3600"

    reply["Content-Security-Policy"] = (
        "default-src 'none'; img-src data:; style-src 'unsafe-inline'"
    )
    reply["X-Content-Type-Options"] = "nosniff"

    return reply


def numberFrom(query, name, default, low, high):
    try:
        value = round(float(query.get(name, default)))
    except (TypeError, ValueError, OverflowError):
        value = default

    return min(high, max(low, value))


def leanPeek(query):
    flatType = query.get("flatType", "4 ROOM")

    if flatType not in FLAT_RULES:
        flatType = "4 ROOM"

    beds = numberFrom(query, "bedrooms", 3, 1, 5)
    baths = numberFrom(query, "bathrooms", 2, 1, 4)
    area = numberFrom(query, "floorAreaSqm", 92, 30, 200)
    reply = HttpResponse(planPng(flatType, beds, baths, area), content_type="image/png")
    reply["Cache-Control"] = "public, max-age=3600"

    return reply


@require_GET
def listing_floor_plan(request, publicId):
    flat, public = shownFlat(request, publicId)
    if not PLANS_ON:
        return redirect("floor_plan", publicId=flat.publicId)
    spec = plan_data(flat)
    if not fits_plan(spec):
        return redirect("floor_plan", publicId=flat.publicId)
    version = plan_hash(spec)
    same = request.headers.get("If-None-Match") == '"fp-' + version + '"'
    return svgReply(spec, public, request.GET.get("v") == version, notModified=same)


@require_GET
def floor_plan_preview(request):
    if not PLANS_ON:
        return leanPeek(request.GET)
    spec = plan_data(request.GET.dict())
    if not fits_plan(spec):
        return leanPeek(request.GET)
    return svgReply(spec, True, False)
