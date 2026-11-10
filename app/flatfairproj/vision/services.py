from urllib.parse import urlencode, urlparse
from pathlib import Path
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.urls import reverse
from contextlib import contextmanager
from uuid import uuid4
import logging
from storefront.models import Listing
from .models import PhotoAnalysis, DetectedItem, ItemPick
from .pipeline import analyse, pick_item
from pretrained import pipelineIdentity


@contextmanager
def ai_lock():
    import fcntl

    lock_at = Path(settings.MEDIA_ROOT) / ".locks" / "vision.lock"
    lock_at.parent.mkdir(parents=True, exist_ok=True)

    with open(lock_at, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def file_hash(path):
    import hashlib

    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except (OSError, TypeError, ValueError):
        return ""


def is_stale(record):
    if record.status == "done" and record.pipelineIdentity != pipelineIdentity():
        return True

    stored = record.timings.get("source_sha256")

    if record.status != "done" or not record.photo_id or not stored:
        return False

    try:
        current = file_hash(record.photo.image.path)
    except (ValueError, NotImplementedError):
        return False

    return bool(current) and stored != current


def process(photo, force=False):
    with ai_lock():
        return _process(photo, force)


# Save the photo check results
def _process(photo, force=False):
    record, _ = PhotoAnalysis.objects.get_or_create(photo=photo)
    path = photo.image.path

    if record.status == "done" and not force and not is_stale(record):
        return record

    batch = uuid4().hex
    stamp = pipelineIdentity()
    record.pipelineIdentity = stamp
    record.status = "running"
    record.error = ""
    record.timings = dict(record.timings, publication_generation=batch)
    record.save(update_fields=["status", "error", "timings", "pipelineIdentity"])
    new_crops = []

    def owned():
        return PhotoAnalysis.objects.filter(
            pk=record.pk, status="running", timings__publication_generation=batch
        )

    def remove_crops(crops):
        for storage, name in crops:
            try:
                storage.delete(name)
            except OSError:
                logging.getLogger(__name__).exception("Crop cleanup failed: %s", name)

    def fail(exc):
        if owned().update(status="failed", error=str(exc)[:2000]):
            record.status, record.error = "failed", str(exc)[:2000]

    try:
        digest = file_hash(path)
        result = analyse(path, room=photo.room)

        with transaction.atomic():
            current = PhotoAnalysis.objects.select_for_update().get(pk=record.pk)

            if current.timings.get("publication_generation") != batch:
                return current

            record.items.all().delete()

            for index, values in enumerate(result["items"]):
                values = dict(values)
                png = values.pop("png")
                item = DetectedItem(analysis=record, **values)
                item.crop.name = item.crop.field.generate_filename(
                    item, f"{record.pk}-{index}-{uuid4().hex}.png"
                )
                item.save()
                new_crops.append((item, png))

            record.flooring, record.layout = result["flooring"], result["layout"]
            record.timings = dict(
                result["timings"],
                size=result.get("size", [1000, 1000]),
                publication_generation=batch,
                source_sha256=digest,
            )
            record.version = stamp["version"]
            record.save(update_fields=["flooring", "layout", "timings", "version"])

            def publish_crops():
                written = []
                reserved = [(item.crop.storage, item.crop.name) for item, _ in new_crops]

                try:
                    if not owned().exists():
                        return

                    posted = []

                    for item, png in new_crops:
                        name = item.crop.storage.save(item.crop.name, ContentFile(png))
                        written.append((item.crop.storage, name))
                        posted.append((item, name))

                    with transaction.atomic():
                        current = (
                            PhotoAnalysis.objects.select_for_update().filter(pk=record.pk).first()
                        )

                        if (
                            not current
                            or current.status != "running"
                            or current.timings.get("publication_generation") != batch
                        ):
                            remove_crops(written + reserved)

                            return

                        for item, name in posted:
                            item.crop.name = name

                        published_items = [item for item, _ in posted]
                        updated = DetectedItem.objects.filter(analysis=record).bulk_update(
                            published_items, ["crop"]
                        )

                        if updated != len(posted):
                            raise RuntimeError("Crop row removed during publication")

                        current.status = "done"
                        current.save(update_fields=["status"])

                    record.status = "done"
                except Exception as exc:
                    remove_crops(written + reserved)
                    fail(exc)

            transaction.on_commit(publish_crops, robust=True)
    except Exception as exc:
        fail(exc)

    return record


def runPick(pick):
    if not ItemPick.objects.filter(pk=pick.pk, status="pending").update(status="running"):
        pick.refresh_from_db()

        return pick

    pick.error = ""

    try:
        with ai_lock():
            result = pick_item(
                pick.analysis.photo.image.path, pick.x, pick.y, room=pick.analysis.photo.room
            )
    except Exception as exc:
        logging.getLogger(__name__).exception("Item pick %s failed", pick.pk)
        ItemPick.objects.filter(pk=pick.pk).update(status="failed")
        pick.status, pick.error = "failed", str(exc)[:500]

        return pick

    if result.get("empty"):
        ItemPick.objects.filter(pk=pick.pk).update(
            status="empty", label=result.get("label", "")[:60]
        )
        pick.status = "empty"

        return pick

    pick.crop.save(
        f"{pick.analysis_id}-{pick.pk}-{uuid4().hex}.png", ContentFile(result["png"]), save=False
    )
    pick.label = result["label"][:60]
    pick.confident = result["confident"]
    pick.box = result["box"]
    pick.polygon = result["polygon"]
    pick.embedding = result["embedding"]
    pick.status = "done"
    pick.save(update_fields=["crop", "label", "confident", "box", "polygon", "embedding", "status"])

    return pick


def family(label):
    return {
        "armchair": "seating",
        "sofa": "seating",
        "chair": "seating",
        "coffee table": "table",
        "dining table": "table",
        "lamp": "light",
        "side table": "table",
        "bedside table": "storage",
        "floor lamp": "light",
        "table lamp": "light",
        "pendant light": "light",
        "kitchen cabinet": "storage",
        "cabinet": "storage",
        "wardrobe": "storage",
        "shelf": "storage",
        "stool": "seating",
        "desk": "table",
        "vanity cabinet": "storage",
        "ceiling light": "light",
    }.get(label, label)


def like_pool(listing_id):
    import numpy as np

    groups = {}

    pool = (
        DetectedItem.objects.filter(
            analysis__status="done",
            analysis__pipelineIdentity=pipelineIdentity(),
            analysis__photo__listing__in=Listing.objects.public(),
        )
        .exclude(analysis__photo__listing_id=listing_id)
        .select_related("analysis__photo__listing")
        .order_by("pk")
    )

    for other in pool:
        vector = np.asarray(other.embedding, dtype=float)

        if vector.shape != (512,) or not np.isfinite(vector).all():
            continue

        items, vectors = groups.setdefault(family(other.label), ([], []))
        items.append(other)
        vectors.append(vector / max(float(np.linalg.norm(vector)), 1e-8))

    grouped = {}

    for key, (items, vectors) in groups.items():
        grouped[key] = (items, np.asarray(vectors))

    return grouped


def like_items(item, candidates=None):
    stamp = pipelineIdentity()

    if item.analysis.status != "done" or item.analysis.pipelineIdentity != stamp:
        return []

    if candidates is None:
        candidates = like_pool(item.analysis.photo.listing_id)

    return nearest(item.label, item.embedding, candidates)


def nearest(label, embedding, candidates):
    import numpy as np

    stamp = pipelineIdentity()
    vector = np.asarray(embedding, dtype=float)

    if vector.shape != (512,) or not np.isfinite(vector).all():
        return []

    items, matrix = candidates.get(family(label), ([], None))

    if not items:
        return []

    scores = matrix @ (vector / max(float(np.linalg.norm(vector)), 1e-8))

    return [
        items[index]
        for index in np.argsort(-scores, kind="stable")
        if items[index].analysis.status == "done"
        and items[index].analysis.pipelineIdentity == stamp
    ][:3]


def item_words(item):
    details = item.attributes or {}
    words = []

    if details.get("brand"):
        words.append(str(details["brand"]))

    for key, value in details.items():
        if key != "brand":
            words.append(str(value))

    return words


def find_words(item):
    return " ".join(item_words(item) + [item.label])


def links(item):
    query = find_words(item)
    search = "https://www.google.com/search?" + urlencode({"tbm": "isch", "q": query})
    base = settings.PUBLIC_URL
    host = urlparse(base).hostname or ""
    public = (
        urlparse(base).scheme == "https"
        and "." in host
        and host not in ("localhost", "127.0.0.1")
        and not host.endswith((".localhost", ".local"))
    )

    import ipaddress

    try:
        public = public and ipaddress.ip_address(host).is_global
    except ValueError:
        pass

    if public:
        crop_url = base.rstrip("/") + reverse("vision_crop", args=[item.pk])
        lens = "https://lens.google.com/uploadbyurl?" + urlencode({"url": crop_url})
    else:
        lens = ""

    return search, lens
