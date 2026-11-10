import logging
import os
import sys
import threading
import time

log = logging.getLogger(__name__)
_started = False
_guard = threading.Lock()
_wake = threading.Event()
MAX_ATTEMPTS = 2


def requeue(listingId=None):
    from pretrained import pipelineIdentity
    from .models import PhotoAnalysis

    records = PhotoAnalysis.objects.filter(
        photo__listing__status="live", status__in=("done", "failed")
    ).exclude(pipelineIdentity=pipelineIdentity())

    if listingId is not None:
        records = records.filter(photo__listing_id=listingId)



    return records.update(status="pending", error="")


def runtime_enabled():
    import multiprocessing

    return (
        multiprocessing.current_process().name == "MainProcess"
        and "runserver" in sys.argv
        and (os.environ.get("RUN_MAIN") == "true" or "--noreload" in sys.argv)
    )


def running():
    return _started


def wake():
    _wake.set()


def queue_flat(listing_id):
    from storefront.models import ListingPhoto
    from .models import PhotoAnalysis

    requeue(listing_id)
    waiting = ListingPhoto.objects.filter(
        listing_id=listing_id, listing__status="live", vision_analysis__isnull=True
    )


    PhotoAnalysis.objects.bulk_create(
        [PhotoAnalysis(photo=photo) for photo in waiting], ignore_conflicts=True
    )


def _waiting():
    from django.db import close_old_connections



    from .models import PhotoAnalysis

    close_old_connections()

    try:
        requeue()

        return list(
            PhotoAnalysis.objects.filter(status="pending", photo__listing__status="live")
            .order_by("pk")
            .values_list("pk", flat=True)[:50]
        )
    finally:
        close_old_connections()


def _duePicks():
    from datetime import timedelta
    from django.db import close_old_connections
    from django.utils import timezone
    from .models import ItemPick


    close_old_connections()



    try:
        fresh = timezone.now() - timedelta(minutes=5)
        ItemPick.objects.filter(status="pending", createdAt__lt=fresh).update(status="failed")

        return list(
            ItemPick.objects.filter(status="pending")
            .order_by("pk")
            .values_list("pk", flat=True)[:5]
        )
    finally:
        close_old_connections()


def _give_up(pk):
    from pretrained import pipelineIdentity
    from django.db import close_old_connections


    from .models import PhotoAnalysis

    close_old_connections()

    try:
        PhotoAnalysis.objects.filter(pk=pk, status__in=("pending", "running")).update(
            status="failed",
            error="Analysis stopped twice; check the server log.",
            pipelineIdentity=pipelineIdentity(),
        )
    finally:
        close_old_connections()


def _retry(pk):
    from django.db import close_old_connections
    from .models import PhotoAnalysis

    close_old_connections()

    try:
        PhotoAnalysis.objects.filter(pk=pk, status="running").update(status="pending")
    finally:
        close_old_connections()


def recover():
    import fcntl


    from pathlib import Path
    from django.conf import settings
    from django.db import close_old_connections
    from .models import PhotoAnalysis

    lock_at = Path(settings.MEDIA_ROOT) / ".locks" / "vision.lock"
    lock_at.parent.mkdir(parents=True, exist_ok=True)

    with open(lock_at, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0

        close_old_connections()

        try:
            from .models import ItemPick



            ItemPick.objects.filter(status="running").update(status="failed")

            return PhotoAnalysis.objects.filter(status="running").update(status="pending")
        finally:
            close_old_connections()


def _supervise(stopped=None):
    from .worker import run_pick, run_vision, end_vision, vision_up, VISION_IDLE_SECONDS

    try:
        recover()
    except Exception:
        log.exception("Could not reset interrupted analyses")


    tries, idle_at = {}, time.monotonic()

    while stopped is None or not stopped.is_set():
        try:
            picks = _duePicks()
        except Exception:
            log.exception("Could not read the pick queue")
            picks = []

        if picks:
            try:
                run_pick(picks[0])
            except Exception:
                log.exception("Could not run item pick %s", picks[0])

            idle_at = time.monotonic()

            continue



        try:
            waiting = [
                analysis_id
                for analysis_id in _waiting()
                if tries.get(analysis_id, 0) < MAX_ATTEMPTS
            ]
        except Exception:
            log.exception("Could not read the photo queue")
            waiting = []

        if waiting:
            scan_id = waiting[0]
            tries[scan_id] = tries.get(scan_id, 0) + 1

            try:
                status = run_vision(scan_id)
            except Exception:
                log.exception("Could not run photo analysis %s", scan_id)
                status = None

            try:
                if status is None:
                    if tries[scan_id] >= MAX_ATTEMPTS:
                        _give_up(scan_id)
                    else:
                        _retry(scan_id)
                elif status == "done":
                    tries.pop(scan_id, None)
            except Exception:
                log.exception("Could not finish photo analysis %s", scan_id)

            idle_at = time.monotonic()

            continue

        if vision_up() and time.monotonic() - idle_at > VISION_IDLE_SECONDS:
            end_vision()



        _wake.wait(5)
        _wake.clear()

    end_vision()


def start():
    global _started

    from .pipeline import weights_ok

    if not runtime_enabled() or not weights_ok():
        return

    with _guard:
        if _started:
            return

        _started = True

    threading.Thread(target=_supervise, daemon=True, name="photo-queue").start()
