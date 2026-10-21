import io
import os
import uuid
import time
import warnings
from contextlib import contextmanager
from datetime import timedelta
from numbers import Real
from django.utils import timezone
from django import forms
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.urls import reverse
from PIL import Image, ImageOps
from storefront.models import (
    ListingPhoto, PhotoCheckJob, Listing, OPTIONAL_ROOMS,
    ROOM_CHOICES, ROOM_LABELS, REQUIRED_ROOMS, applies,
    counts,
)
from .models import ListingDraft
from .photocheck import (
    missingQ, AI_REASON, borderline, checkPics,
    BLOCK_AT, MODEL_VERSION, reviewReasons, slotWarn,
    SLOT_PROOF,
)
from .photoqueue import queuePics, ensureWorker

MAX_MB = 10
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def shrink(upload):
    max_side = 1600
    upload.seek(0)
    img = Image.open(upload)

    if img.width * img.height > MAX_PIXELS:
        raise ValueError("Photo exceeds pixel limit")

    img = ImageOps.exif_transpose(img)
    img.thumbnail((max_side, max_side))

    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        flat = Image.new("RGB", img.size, "white")
        flat.paste(img, mask=img.getchannel("A"))
        img = flat
    elif img.mode != "RGB":
        img = img.convert("RGB")

    out = io.BytesIO()
    img.save(out, "JPEG", quality=82, optimize=True, progressive=True)

    return ContentFile(out.getvalue())


def pending(request):
    token = draft(request)

    return request.session[DRAFTS_KEY][token]["photos"]


def clearHeld(request, token=None):
    token = token or draft(request)
    drafts = request.session.get(DRAFTS_KEY, {})
    data = drafts.pop(token, {})
    paths = [
        *data.get("photos", {}).values(),
        *[photo["path"] for photo in data.get("refused", {}).values()],
    ]

    PhotoCheckJob.objects.filter(
        owner=request.user, pendingPath__in=paths, status__in=("queued", "running")
    ).update(status="failed", finishedAt=timezone.now(), error="Draft removed.")

    dropPaths(paths)
    request.session[DRAFTS_KEY] = drafts


def reviewNote(result, room):
    warning = slotWarn(result, room)

    if warning:
        return warning

    reasons = reviewReasons(result)

    if AI_REASON in reasons:
        return "This photo looks computer-generated. Our team will check it before your flat goes live. Pro photography in Packages can retake it for you."

    if "may not show the flat" in reasons:
        return "We’re not sure this photo shows the flat. Our team will take a quick look at it. Pro photography in Packages can retake it for you."

    return "Our team will take a quick look at this photo."


def roomState(request, room):
    result = checks(request).get(room, {})

    if result.get("checking"):
        return {
            "verdict": "checking",
            "state": "checking",
            "message": "Checking…",
            "jobId": result.get("jobId"),
        }

    if room in refused(request):
        found = refused(request)[room]["result"]

        return {
            "verdict": "block",
            "message": blockNote(found["label"], ROOM_LABELS[room].lower())
            + " Please choose a photo of that room instead.",
            "appealable": True,
        }

    if reviewReasons(result):
        return {"verdict": "review", "message": reviewNote(result, room)}

    if borderline(result):
        return {"verdict": "accept", "message": "Please check that this photo shows the flat."}

    return {
        "verdict": "accept",
        "message": sortNote(result) or "This looks like a photo of the flat.",
    }


def keepFiles(request, syncResults=True):
    dropFiles(request)
    errors = {}
    kept = pending(request)

    for room, path in list(kept.items()):
        if not default_storage.exists(path):
            kept.pop(room)
            checks(request).pop(room, None)
            request.session.modified = True

            if not request.FILES.get(f"photo_{room}"):
                errors[room] = "This photo is no longer available. Please choose the photo again."

    ready = {}

    for room, _ in ROOM_CHOICES:
        upload = request.FILES.get(f"photo_{room}")

        if not upload:
            continue

        data, error = readPhoto(upload)

        if error:
            errors[room] = error
        else:
            ready[room] = data

    queueFiles(request, ready)

    if syncResults:
        syncChecks(request)

    errors.update(appealAll(request))

    if syncResults:
        for room, error in syncChecks(request).items():
            errors.setdefault(room, error)

    return errors


def attachPics(request, listing, rooms, savedPhotos):
    kept, results = pending(request), checks(request)

    if any(results.get(room, {}).get("checking") for room in rooms):
        raise ValueError("Photo checks are still running")

    for room in rooms:
        if room in kept:
            result = results.get(room, {})

            photo = ListingPhoto(
                listing=listing,
                room=room,
                aiLabel=result.get("label", "unchecked"),
                aiIrrelevant=result.get("irrelevant"),
                aiGenerated=result.get("aiGenerated"),
                tags=result.get("tags", []),
                embedding=result.get("embedding"),
                modelVersion=result.get("modelVersion"),
                photoReview=result.get("reviewReasons", [])
                + (
                    ["Photo model identity mismatch; staff must review it"]
                    if result.get("identityStatus") == "identity mismatch"
                    else []
                ),
            )

            with default_storage.open(kept[room]) as stored:
                photo.image.save(os.path.basename(kept[room]), stored, save=False)

            savedPhotos.append(photo.image)
            photo.save()


def slot_type(room):
    single_slots = {"living_room", "kitchen"}

    if "toilet" in room:
        return "toilet"

    if "bedroom" in room:
        return "bedroom"

    if room in single_slots:
        return room

    return None


def photoType(result):
    photo_types = {
        "living_room": "living_room",
        "kitchen": "kitchen",
        "bedroom": "bedroom",
        "bathroom": "toilet",
    }

    if not result or result.get("labelSource") != "room-head" or result.get("appeal"):
        return None

    kind = photo_types.get(result.get("roomLabel"))
    trust = result.get("roomConfidence")

    if kind is None or not isinstance(trust, Real) or not (0.98) <= trust <= 1:
        return None

    tags = set(result.get("tags") or [])

    for other, proof in SLOT_PROOF.items():
        if other != kind and tags & proof:
            return None

    return kind


def read_count(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def keepRooms(request, data):
    if "bedrooms" in request.POST or "bathrooms" in request.POST:
        data["roomCounts"] = counts(
            read_count(request.POST.get("bedrooms")),
            read_count(request.POST.get("bathrooms")),
            False,
        )


def listingFor(request, data):
    if not data.get("listingId"):
        return None

    return Listing.objects.filter(pk=data["listingId"], seller=request.user).first()


def tallyShown(request, data):
    if "roomCounts" in data:
        return data["roomCounts"]

    listing = listingFor(request, data)

    if listing:
        return counts(listing.bedrooms, listing.bathrooms, listing.hasBalcony)

    return counts(1, 1, False)


def picUrl(request, data, room):
    if not data.get("listingId"):
        return ""

    photo = ListingPhoto.objects.filter(
        listing_id=data["listingId"], listing__seller=request.user, room=room
    ).first()

    return photo.publicUrl if photo else ""


def takenRooms(request, data):
    taken = set(data["photos"]) | set(data["checks"]) | set(data.get("refused", {}))

    if data.get("listingId"):
        taken |= set(
            ListingPhoto.objects.filter(
                listing_id=data["listingId"], listing__seller=request.user
            ).values_list("room", flat=True)
        )

    return taken


def sortTarget(request, data, room, result):
    current = slot_type(room)
    kind = photoType(result)

    if current is None or kind is None or kind == current:
        return None

    have = tallyShown(request, data)
    taken = takenRooms(request, data)

    for key, label, needs in REQUIRED_ROOMS:
        if slot_type(key) == kind and applies(needs, have) and key not in taken:
            return key

    return None


def withSlot(result, old, new):
    moved = dict(result)
    warning = slotWarn(result, old)
    reasons = [reason for reason in result.get("reviewReasons", []) if reason != warning]
    warning = slotWarn(result, new)

    if warning:
        reasons.append(warning)

    moved["reviewReasons"] = list(dict.fromkeys(reasons))

    return moved


def sortPhoto(request, data, room):
    result = data["checks"].get(room)

    if room not in data["photos"] or not result or result.get("checking") or result.get("keptHere"):
        return None

    target = sortTarget(request, data, room, result)

    if not target:
        return None

    moved = withSlot(result, room, target)
    moved["movedFrom"] = room
    data["photos"][target] = data["photos"].pop(room)
    data["checks"][target] = moved
    data["checks"].pop(room)
    request.session.modified = True

    return target


def movedTo(photos, results, room):
    if room in photos:
        return None

    for target, result in results.items():
        if result.get("movedFrom") == room and target in photos:
            return target

    return None


def moveBack(request, data, room):
    target = movedTo(data["photos"], data["checks"], room)

    if not target:
        return False

    result = withSlot(data["checks"].pop(target), target, room)
    result.pop("movedFrom", None)
    result["keptHere"] = True
    data["photos"][room] = data["photos"].pop(target)
    data["checks"][room] = result
    request.session.modified = True

    return True


def placeName(room):
    name = ROOM_LABELS[room].lower()

    if name[-1:].isdigit():
        return name

    return "the " + name


def movedNote(photos, results, room):
    type_names = {
        "living_room": "the living room",
        "kitchen": "the kitchen",
        "bedroom": "a bedroom",
        "toilet": "a toilet",
    }

    target = movedTo(photos, results, room)

    if not target:
        return ""

    kind = slot_type(target)

    return f"Your photo looks like {type_names[kind]}, so we moved it to {ROOM_LABELS[target]}. {ROOM_LABELS[room]} still needs its own photo."


def sortNote(result):
    result = result or {}

    if result.get("keptHere"):
        return "No problem, we’ll keep this photo here."

    if result.get("movedFrom") not in ROOM_LABELS:
        return ""

    return f"We moved this photo here from {placeName(result['movedFrom'])}."
