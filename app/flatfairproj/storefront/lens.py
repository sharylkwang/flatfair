import hashlib
import time
import math
import logging
from numbers import Real
import numpy
from django import forms
from django.core.cache import caches
from django.core.exceptions import ValidationError
from django.db.models import Count, Max
from django.shortcuts import render
from sellerpanel.photocheck import (
    BLOCK_AT,
    EMBEDDING_SIZE,
    MODEL_VERSION,
    PHOTO_BUSY,
    checkPics,
)
from sellerpanel.photos import shrink
from storefront.models import object_id, Listing, ListingPhoto
from pretrained import warnGone

log = logging.getLogger(__name__)
resourceCache = caches["resources"]
MAX_MB = 8
SEARCHES_PER_MINUTE = 5
SEARCHES_PER_HOUR = 10
RESULTS = 12
MIN_USEFUL_SIMILARITY = 0.75

CLOSENESS = [
    (0.92, "Very similar"),
    (0.85, "Similar"),
    (MIN_USEFUL_SIMILARITY, "A little similar"),
    (-1, "Not very similar"),
]

BUSY = "You have searched a lot recently. Wait a while, then try again."
FAILED = "Photo search is unavailable for this photo. Try another room photo."


class LensForm(forms.Form):
    photo = forms.ImageField(
        label="Room photo",
        help_text=f"A JPG or PNG up to {MAX_MB} MB.",
        error_messages={
            "required": "Choose a photo of a room.",
            "invalid_image": "This file is not a photo. Choose a JPG or PNG.",
        },
    )

    def clean_photo(self):
        photo = self.cleaned_data["photo"]

        if photo.size > MAX_MB * 1024 * 1024:
            raise ValidationError(f"This photo is over {MAX_MB} MB. Choose a smaller one.")

        if photo.image.format not in ("JPEG", "PNG"):
            raise ValidationError("This file is not a photo. Choose a JPG or PNG.")

        return photo


def searchFlood(request):
    now = time.time()
    recent = []

    for moment in request.session.get("lensSearches", []):
        if moment > now - 60:
            recent.append(moment)

    if len(recent) >= SEARCHES_PER_MINUTE:
        return True

    address = request.META.get("REMOTE_ADDR", "unknown")
    stamps = ["ip:" + hashlib.sha256(address.encode()).hexdigest()]

    if request.user.is_authenticated:
        stamps.append(f"user:{request.user.pk}")

    window = int(now // 3600)

    for stamp in stamps:
        allowed = any(
            resourceCache.add(f"lens:{stamp}:{window}:{slot}", True, 3600)
            for slot in range(SEARCHES_PER_HOUR)
        )

        if not allowed:
            return True

    recent.append(now)
    request.session["lensSearches"] = recent

    return False


def closeness(score):
    for lowest, phrase in CLOSENESS:
        if score >= lowest:
            return phrase

    return CLOSENESS[-1][1]


def validEmbedding(embedding):
    try:
        vector = numpy.asarray(embedding)

        if vector.shape != (EMBEDDING_SIZE,) or vector.dtype.kind not in "iuf":
            return None

        vector = vector.astype(float)

        with numpy.errstate(over="ignore", invalid="ignore"):
            length = numpy.linalg.norm(vector)

        if not numpy.isfinite(vector).all() or not math.isfinite(length) or length == 0:
            return None

        return vector / length
    except Exception as error:
        return None


def scoreOk(score):
    return isinstance(score, Real) and math.isfinite(score) and 0 <= score <= 1


def embeddingMatrix():
    photos = ListingPhoto.objects.filter(embedding__isnull=False, modelVersion=MODEL_VERSION)

    mark = photos.aggregate(count=Count("pk"), latest=Max("updatedAt"), lastId=Max("pk"))

    key = "lens:matrix:" + hashlib.sha256(f"{MODEL_VERSION}:{mark}".encode()).hexdigest()

    cached = resourceCache.get(key)

    if cached is not None:
        return cached

    ids, vectors = [], []

    for photo in photos.only("pk", "embedding").order_by("pk").iterator():
        vector = validEmbedding(photo.embedding)

        if vector is not None:
            ids.append(photo.pk)
            vectors.append(vector)

    matrix = numpy.asarray(vectors, dtype=float).reshape((-1, EMBEDDING_SIZE))
    cached = (ids, matrix)
    resourceCache.set(key, cached, 300)

    return cached


def similarFlats(embedding, skipListing=None):
    query = validEmbedding(embedding)

    if query is None:
        warnGone(log, "Photo search unavailable: incompatible query embedding")

        return []

    try:
        ids, matrix = embeddingMatrix()
        scores = matrix @ query

        photos = ListingPhoto.objects.filter(
            listing__in=Listing.objects.public().filter(status="live"),
            embedding__isnull=False,
            modelVersion=MODEL_VERSION,
        )

        if skipListing:
            photos = photos.exclude(listing=skipListing)

        allowed = dict(photos.values_list("pk", "listing_id"))
        cover = {}

        for photoId in ids:
            flatId = allowed.get(photoId)

            if flatId is not None:
                cover.setdefault(flatId, photoId)

        picked, homes = [], set()

        tieRank = numpy.asarray([cover.get(allowed.get(photoId), photoId) for photoId in ids])

        for index in numpy.lexsort((numpy.arange(len(ids)), tieRank, -scores)):
            photoId = ids[index]
            flatId = allowed.get(photoId)

            if flatId is None or flatId in homes:
                continue

            homes.add(flatId)
            picked.append((photoId, float(scores[index])))

            if len(picked) == RESULTS:
                break

        if not picked or picked[0][1] < MIN_USEFUL_SIMILARITY:
            return []

        found = ListingPhoto.objects.select_related("listing").in_bulk(
            [photo_id for photo_id, score in picked]
        )

        return [
            {
                "flat": found[photo_id].listing,
                "photo": found[photo_id],
                "score": score,
                "closeness": closeness(score),
            }
            for photo_id, score in picked
            if photo_id in found
        ]
    except Exception as error:
        log.exception("Photo similarity search failed")

        return []


def fromUpload(request, context):
    form = LensForm(request.POST, request.FILES)
    context["form"] = form

    if not form.is_valid():
        return

    if searchFlood(request):
        context["error"] = BUSY

        return

    try:
        data = shrink(form.cleaned_data["photo"]).read()
    except Exception as error:
        form.add_error("photo", "This file is not a photo. Choose a JPG or PNG.")

        return

    try:
        result = checkPics([data])[0]
    except Exception as error:
        log.exception("Photo search inference failed")
        context["error"] = FAILED

        return

    if result and result.get("busy"):
        context["error"] = PHOTO_BUSY

        return

    if (
        not result
        or result.get("modelVersion") != MODEL_VERSION
        or validEmbedding(result.get("embedding")) is None
        or not scoreOk(result.get("irrelevant"))
    ):
        warnGone(log, "Photo search unavailable: missing or incompatible photo result")

        context["error"] = FAILED

        return

    if result["irrelevant"] >= BLOCK_AT:
        form.add_error(
            "photo",
            "This does not look like a room in a flat. Choose a photo of a room.",
        )

        return

    context["tags"] = result.get("tags", [])
    context["matches"] = similarFlats(result["embedding"])
    context["searched"] = True


def fromPic(request, context):
    try:
        photoId = object_id(request.GET.get("photo"))
    except Exception as error:
        photoId = None

    public = Listing.objects.public()
    source = ListingPhoto.objects.filter(pk=photoId, listing__in=public)
    source = source.select_related("listing").first()

    if not source:
        context["error"] = "We cannot search with that photo yet. Upload a room photo instead."

        return

    if (
        source.modelVersion != MODEL_VERSION
        or validEmbedding(source.embedding) is None
        or not scoreOk(source.aiIrrelevant)
    ):
        warnGone(log, "Photo search unavailable: missing or incompatible stored embedding")

        context["error"] = FAILED

        return

    if searchFlood(request):
        context["error"] = BUSY

        return

    context["source"] = source
    context["tags"] = source.tags
    context["matches"] = similarFlats(source.embedding, source.listing)
    context["searched"] = True


def lens(request):
    context = {"form": LensForm()}

    if request.method == "POST":
        fromUpload(request, context)
    elif "photo" in request.GET:
        fromPic(request, context)

    return render(request, "storefront/lens.html", context)
