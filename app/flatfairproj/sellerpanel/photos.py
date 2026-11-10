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
DRAFTS_KEY = "photoDrafts"
MAX_PIXELS = 40000000
MAX_APPEALS = 3
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


def pic_paths():
    paths = set()

    for photos in ListingDraft.objects.values_list("photos", flat=True):
        for path in photos.values():
            paths.add(path)

    return paths


def dropPaths(paths):
    paths = set(paths)

    if not paths:
        return

    inUse = set(
        PhotoCheckJob.objects.filter(status__in=("queued", "running")).values_list(
            "pendingPath", flat=True
        )
    )

    for path in paths - pic_paths() - inUse:
        default_storage.delete(path)


def sweepPics():
    try:
        _, files = default_storage.listdir("pending")
    except FileNotFoundError:
        files = []

    removed = 0
    cutoff = timezone.now() - timedelta(hours=24)
    oldJobs = PhotoCheckJob.objects.filter(status__in=("done", "failed"), finishedAt__lt=cutoff)
    oldOnes = list(oldJobs.values_list("pendingPath", flat=True))
    oldJobs.delete()
    dropPaths(oldOnes)
    saves = pic_paths() | set(
        PhotoCheckJob.objects.filter(status__in=("queued", "running")).values_list(
            "pendingPath", flat=True
        )
    )

    for name in files:
        path = f"pending/{name}"

        if path in saves:
            continue

        try:
            if default_storage.get_modified_time(path) < cutoff:
                default_storage.delete(path)
                removed += 1
        except FileNotFoundError:
            pass

    return removed


def draft(request):
    if hasattr(request, "_photoDraft"):
        return request._photoDraft

    drafts = request.session.get(DRAFTS_KEY, {})
    now = time.time()

    for token, data in list(drafts.items()):
        if data["created"] < now - 86400:
            dropPaths(
                [
                    *data["photos"].values(),
                    *[photo["path"] for photo in data.get("refused", {}).values()],
                ]
            )

            del drafts[token]

    token = (
        request.POST.get("draftToken")
        if request.method == "POST"
        else request.GET.get("draftToken")
    )

    if token not in drafts:
        token = uuid.uuid4().hex
        drafts[token] = {"created": now, "photos": {}, "checks": {}}

    request._photoDraft = token
    request.session[DRAFTS_KEY] = drafts

    if now - request.session.get("pendingCleanupAt", 0) > 3600:
        sweepPics()
        request.session["pendingCleanupAt"] = now

    return token


def pending(request):
    token = draft(request)

    return request.session[DRAFTS_KEY][token]["photos"]


def checks(request):
    token = draft(request)

    return request.session[DRAFTS_KEY][token]["checks"]


def refused(request):
    token = draft(request)

    return request.session[DRAFTS_KEY][token].get("refused", {})


def forget_bad(request, room):
    token = draft(request)
    photo = request.session[DRAFTS_KEY][token].get("refused", {}).pop(room, None)

    if photo:
        dropPaths([photo["path"]])
        request.session.modified = True


def appealWhy(room):
    return (
        f"Seller says this is a photo of the {ROOM_LABELS[room].lower()}; the photo check disagreed"
    )


def appealNum(request, room=None):
    token = draft(request)
    data = request.session[DRAFTS_KEY][token]
    total = sum(bool(result.get("appeal")) for key, result in data["checks"].items() if key != room)
    optional = {key for key, label, needs in OPTIONAL_ROOMS}
    replaced = [
        *data["photos"],
        *[key for key in request.POST.getlist("removePhoto") if key in optional],
    ]

    if room:
        replaced.append(room)

    already = ListingPhoto.objects.filter(
        listing_id=data.get("listingId"), listing__seller=request.user
    ).exclude(room__in=replaced)

    return total + sum(appealWhy(photo.room) in photo.photoReview for photo in already)


def badAppeal(request):
    if appealNum(request) <= MAX_APPEALS:
        return {}

    errors = {}

    for room, result in checks(request).items():
        if result.get("appeal"):
            errors[room] = (
                "You can ask our team to check up to 3 refused photos per listing. Please choose another photo for this room."
            )

    return errors


def appealPhoto(request, room):
    token = draft(request)
    data = request.session[DRAFTS_KEY][token]
    photo = data.get("refused", {}).get(room)

    if not photo:
        return "Please choose a refused room photo before asking our team to check it.", None

    if appealNum(request, room) >= MAX_APPEALS:
        return (
            "You can ask our team to check up to 3 refused photos per listing. Please choose another photo for this room.",
            None,
        )

    try:
        with default_storage.open(photo["path"]) as image:
            picData = image.read()
    except (OSError, ValueError):
        forget_bad(request, room)

        return "This photo is no longer available. Please choose the photo again.", None

    error = keepPhoto(request, room, picData, photo["result"], appeal=True)

    return error, checks(request).get(room)


def appealAll(request):
    errors = {}

    for room in dict.fromkeys(request.POST.getlist("appealPhoto")):
        if room not in ROOM_LABELS or request.FILES.get(f"photo_{room}"):
            continue

        error, result = appealPhoto(request, room)

        if error:
            errors[room] = error

    errors.update(badAppeal(request))

    return errors


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


def readPhoto(upload):
    if upload.content_type not in ("image/jpeg", "image/png", "image/webp"):
        return None, "This file is not a photo. Choose a JPG, PNG or WebP."

    if upload.size > MAX_MB * 1024 * 1024:
        return None, f"This photo is too big (over {MAX_MB} MB). Please choose a smaller one."

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            photo = forms.ImageField().clean(upload)

            if photo.image.format not in ("JPEG", "PNG", "WEBP"):
                raise forms.ValidationError("Unsupported photo type")

            return shrink(upload).read(), ""
    except (
        forms.ValidationError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        ValueError,
    ):
        return None, "This file is not a photo. Choose a JPG or PNG."


def keepPhoto(request, room, data, result, appeal=False, pendingPath=None):
    result = dict(result) if result else result

    if (
        result
        and result.get("irrelevant") is not None
        and result["irrelevant"] >= result.get("blockAt", BLOCK_AT)
        and not appeal
    ):
        forget_bad(request, room)
        token = draft(request)

        request.session[DRAFTS_KEY][token].setdefault("refused", {})[room] = {
            "path": pendingPath
            or default_storage.save(f"pending/{uuid.uuid4().hex}.jpg", ContentFile(data)),
            "result": result,
        }

        oldPath = request.session[DRAFTS_KEY][token]["photos"].pop(room, None)
        request.session[DRAFTS_KEY][token]["checks"].pop(room, None)

        if oldPath and oldPath != pendingPath:
            dropPaths([oldPath])

        request.session.modified = True

        return (
            blockNote(result["label"], ROOM_LABELS[room].lower())
            + " Please choose a photo of that room instead."
        )

    if result:
        if appeal:
            result["appeal"] = True
            result["reviewReasons"] = list(
                dict.fromkeys([*result.get("reviewReasons", []), appealWhy(room)])
            )

        warning = slotWarn(result, room)

        if warning:
            result["reviewReasons"] = list(
                dict.fromkeys([*result.get("reviewReasons", []), warning])
            )

    token = draft(request)
    drafts = request.session[DRAFTS_KEY]
    kept = drafts[token]["photos"]
    path = pendingPath or default_storage.save(f"pending/{uuid.uuid4().hex}.jpg", ContentFile(data))

    if room in kept and kept[room] != path:
        dropPaths([kept[room]])

    kept[room] = path

    if result:
        drafts[token]["checks"][room] = {**result, "unchecked": result.get("unchecked", False)}
    else:
        drafts[token]["checks"][room] = {"unchecked": True}

    request.session[DRAFTS_KEY] = drafts
    forget_bad(request, room)

    return ""


def queueFiles(request, ready):
    if not ready:
        return

    token = draft(request)
    data = request.session[DRAFTS_KEY][token]
    keepRooms(request, data)
    paths = {
        room: default_storage.save(f"pending/{uuid.uuid4().hex}.jpg", ContentFile(image))
        for room, image in ready.items()
    }
    previousPaths = {room: data["photos"].get(room) for room in paths}

    try:
        jobs = queuePics(request.user, paths, previousPaths=previousPaths)
    except Exception:
        dropPaths(paths.values())

        raise

    for room, path in paths.items():
        old = data["photos"].get(room)
        data["photos"][room] = path
        data["checks"][room] = {"jobId": str(jobs[room].pk), "checking": True}

        if old:
            dropPaths([old])

        forget_bad(request, room)

    request.session.modified = True


def syncChecks(request):
    token = draft(request)
    data = request.session[DRAFTS_KEY][token]
    ids = []

    for result in data["checks"].values():
        if result.get("checking") and result.get("jobId"):
            ids.append(result["jobId"])

    jobs = {
        str(job.pk): job for job in PhotoCheckJob.objects.filter(owner=request.user, pk__in=ids)
    }
    errors = {}

    for room, result in list(data["checks"].items()):
        if not result.get("checking"):
            continue

        job = jobs.get(result.get("jobId"))

        if job is not None and job.room == room and data["photos"].get(room) == job.pendingPath:
            if job.status in ("queued", "running"):
                continue

            found = job.result if job.status == "done" else {"unchecked": True}
            error = keepPhoto(request, room, None, found, pendingPath=job.pendingPath)

            if error:
                errors[room] = error
            else:
                sortPhoto(request, data, room)
        else:
            data["checks"][room] = {"unchecked": True}
            request.session.modified = True

    for room, photo in data.get("refused", {}).items():
        errors.setdefault(
            room,
            blockNote(photo["result"]["label"], ROOM_LABELS[room].lower())
            + " Please choose a photo of that room instead.",
        )

    return errors


def busyRooms(request):
    rooms = []

    for room, result in checks(request).items():
        if result.get("checking") and room in pending(request):
            rooms.append(room)

    return rooms


def waitChecks(request, seconds=20):
    until = time.monotonic() + seconds
    errors = syncChecks(request)

    if busyRooms(request):
        ensureWorker()

    while busyRooms(request) and time.monotonic() < until:
        time.sleep(min(0.25, max(0, until - time.monotonic())))
        errors.update(syncChecks(request))

    errors.update(
        {
            room: "Still checking… refresh to see the result, then submit again. Your listing hasn’t been published yet."
            for room in busyRooms(request)
        }
    )

    return errors


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


def photoState(request, room):
    data = request.session[DRAFTS_KEY][draft(request)]
    target = movedTo(data["photos"], data["checks"], room)

    if target:
        return {
            "verdict": "moved",
            "message": movedNote(data["photos"], data["checks"], room),
            "movedTo": target,
            "target": photoState(request, target),
            "url": picUrl(request, data, room),
        }

    state = roomState(request, room)
    source = data["checks"].get(room, {}).get("movedFrom")

    if source and room in data["photos"]:
        state["url"] = reverse("draft_photo", kwargs={"path": data["photos"][room]})

    if source and room in data["photos"] and source not in data["photos"]:
        state["movedFrom"] = source
        state["movedNote"] = movedNote(data["photos"], data["checks"], source)
        state["movedFromUrl"] = picUrl(request, data, source)

    return state


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


def scanUpload(request, room, upload):
    data, error = readPhoto(upload)

    if error:
        return error, None

    queueFiles(request, {room: data})
    errors = syncChecks(request)
    result = refused(request)[room]["result"] if room in errors else checks(request).get(room)

    return errors.get(room, ""), result


def dropFiles(request):
    token = draft(request)
    drafts = request.session[DRAFTS_KEY]

    for room in request.POST.getlist("removePhoto"):
        if not request.FILES.get(f"photo_{room}"):
            forget_bad(request, room)

        if room in drafts[token]["photos"] and not request.FILES.get(f"photo_{room}"):
            path = drafts[token]["photos"].pop(room)

            PhotoCheckJob.objects.filter(
                owner=request.user, pendingPath=path, status__in=("queued", "running")
            ).update(status="failed", finishedAt=timezone.now(), error="Photo removed.")

            dropPaths([path])
            drafts[token]["checks"].pop(room, None)

    request.session[DRAFTS_KEY] = drafts


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


def doubtfulPhotos(request, rooms):
    unsure = []

    for room in rooms:
        if room in pending(request) and reviewReasons(checks(request).get(room)):
            unsure.append(room)

    return unsure


@contextmanager
def fileGuard():
    newPics = []

    try:
        yield newPics
    except Exception:
        for photo in newPics:
            photo.storage.delete(photo.name)

        raise


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


def niceLabel(label):
    names = {
        "other_place": "an office or shop, not a home",
        "phone": "a phone",
        "food": "food or a drink",
        "floor_plan": "a floor plan",
        "outdoor": "an outdoor scene",
    }

    if label in names:
        return names[label]

    words = label.replace("_", " ")

    if words[:1] in ("a", "e", "i", "o", "u"):
        return "an " + words

    return "a " + words


def blockNote(label, room):
    if label == "other_place":
        return "This looks like an office or shop, not a home."

    article = "" if room[-1:].isdigit() else "the "

    return f"This looks like {niceLabel(label)}, not {article}{room}."


def dropPhoto(listing, photo):
    name = photo.image.name
    photo.delete()

    if name.startswith(f"listings/{listing.publicId}/"):
        transaction.on_commit(lambda: default_storage.delete(name))


def photoStamp(listing):
    return list(
        listing.photos.order_by("pk").values_list(
            "pk",
            "image",
            "modelVersion",
            "staffChecked",
            "updatedAt",
            "staffReviewedBy_id",
            "staffReviewedAt",
            "staffReviewedRevision",
        )
    )


def planRedo(listing):
    stamp = photoStamp(listing)
    stale = list(listing.photos.filter(missingQ()).order_by("pk"))

    if not stale:
        return stamp, []

    ready = []

    for photo in stale:
        try:
            with photo.image.open("rb") as image:
                ready.append((photo, image.read()))
        except (OSError, ValueError):
            pass

    results = checkPics([data for photo, data in ready])
    checked = {photo.pk: result for (photo, data), result in zip(ready, results)}

    return stamp, [(photo, checked.get(photo.pk) or {}) for photo in stale]


def redoStale(listing, prepared):
    stamp, checked = prepared

    if photoStamp(listing) != stamp:
        return False

    allDone = True

    for photo, result in checked:
        if result.get("modelVersion") != MODEL_VERSION:
            result = {}
            allDone = False

        photo.aiLabel = result.get("label", "unchecked")
        photo.aiIrrelevant = result.get("irrelevant")
        photo.aiGenerated = result.get("aiGenerated")
        photo.tags = result.get("tags", [])
        photo.embedding = result.get("embedding")
        photo.modelVersion = result.get("modelVersion")
        warning = slotWarn(result, photo.room)
        appealReasons = [reason for reason in photo.photoReview if reason == appealWhy(photo.room)]
        photo.photoReview = list(
            dict.fromkeys(
                appealReasons + result.get("reviewReasons", []) + ([warning] if warning else [])
            )
        )

        if result.get("identityStatus") == "identity mismatch":
            photo.photoReview.append("Photo model identity mismatch; staff must review it")

        photo.staffChecked = False
        photo.save(
            update_fields=[
                "aiLabel",
                "aiIrrelevant",
                "aiGenerated",
                "tags",
                "embedding",
                "modelVersion",
                "staffChecked",
                "photoReview",
            ]
        )

    return allDone


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
