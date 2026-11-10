import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

from django.core.cache import caches
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import OperationalError, transaction
from django.db.models import F
from django.utils import timezone

from storefront.models import lock_user, PhotoCheckJob
from .device import disableGpu, gpuVars, modelLock

log = logging.getLogger(__name__)
resourceCache = caches["resources"]
HEARTBEAT_KEY = "photo-worker:heartbeat"
STARTING_KEY = "photo-worker:starting"
STARTUP_FAILURE_KEY = "photo-worker:startup-failures"
RETRY_AT_KEY = "photo-worker:retry-at"
MAX_STARTUP_FAILURES = 3
STARTUP_COOLDOWN = 300
STALE_SECONDS = 90
MAX_ATTEMPTS = 2
STUCK_SECONDS = 1200
PROJECT = Path(__file__).resolve().parent.parent


def fail_queue():
    PhotoCheckJob.objects.filter(status="queued").update(
        status="failed",
        finishedAt=timezone.now(),
        result={"unchecked": True},
        error="Photo checker could not start; staff will check this photo.",
    )


def startFail():
    fails = resourceCache.get(STARTUP_FAILURE_KEY, 0) + 1
    delay = STARTUP_COOLDOWN if fails >= MAX_STARTUP_FAILURES else 2**fails
    resourceCache.set(STARTUP_FAILURE_KEY, fails, STARTUP_COOLDOWN)
    resourceCache.set(RETRY_AT_KEY, time.time() + delay, STARTUP_COOLDOWN)

    if fails >= MAX_STARTUP_FAILURES:
        fail_queue()

    return delay


def markClaim(token=None):
    resourceCache.delete_many([STARTUP_FAILURE_KEY, RETRY_AT_KEY, STARTING_KEY])

    if token:
        resourceCache.set(f"photo-worker:claimed:{token}", True, 1800)


def fixStale(locked=False, heartbeat=None):
    if not locked:
        with modelLock() as held:
            return fixStale(locked=True, heartbeat=heartbeat) if held else 0

    heartbeat = resourceCache.get(HEARTBEAT_KEY, 0) if heartbeat is None else heartbeat

    if time.time() - heartbeat < STALE_SECONDS:
        return 0

    stale = PhotoCheckJob.objects.filter(
        status="running", startedAt__lt=timezone.now() - timedelta(seconds=STALE_SECONDS)
    )

    failed = stale.filter(attempts__gte=MAX_ATTEMPTS).update(
        status="failed",
        finishedAt=timezone.now(),
        error="Photo check stopped twice; staff will check this photo.",
        result={"unchecked": True},
    )

    return failed + stale.filter(attempts__lt=MAX_ATTEMPTS).update(
        status="queued", startedAt=None, error=""
    )


def watchWork(process, token, gpu=None):
    from django.db import close_old_connections

    try:
        if gpu is None:
            process.wait()
        else:
            try:
                process.wait(timeout=STUCK_SECONDS)
            except subprocess.TimeoutExpired:
                disableGpu("the photo worker on the GPU did not stop")
                process.kill()
                process.wait()

        close_old_connections()

        if resourceCache.get(STARTING_KEY) == token:
            resourceCache.delete(STARTING_KEY)

        claimKey = f"photo-worker:claimed:{token}"
        claimed = resourceCache.get(claimKey, False)
        resourceCache.delete(claimKey)

        if not claimed:
            with modelLock() as held:
                if not held or resourceCache.get(STARTING_KEY):
                    return

                if PhotoCheckJob.objects.filter(status="queued").exists():
                    delay = startFail()

                    if delay == STARTUP_COOLDOWN:
                        return
                else:
                    delay = 0

            if delay:
                time.sleep(delay)

        if PhotoCheckJob.objects.filter(status="running").exists():
            time.sleep(STALE_SECONDS + 1)

        ensureWorker()
    except Exception:
        log.exception("Could not restart the photo worker")
    finally:
        close_old_connections()


def ensureWorker(warm=False):
    try:
        with modelLock() as held:
            if not held:
                return False

            fixStale(locked=True)

            if resourceCache.get(RETRY_AT_KEY, 0) > time.time():
                if resourceCache.get(STARTUP_FAILURE_KEY, 0) >= MAX_STARTUP_FAILURES:
                    fail_queue()

                return False

            if not warm and not PhotoCheckJob.objects.filter(status="queued").exists():
                return False

            token = uuid.uuid4().hex

            if not resourceCache.add(STARTING_KEY, token, 30):
                return False

            from .photocheck import photoGpu

            gpu = photoGpu()

            try:
                options = (
                    {
                        "creationflags": subprocess.DETACHED_PROCESS
                        | subprocess.CREATE_NEW_PROCESS_GROUP
                    }
                    if os.name == "nt"
                    else {"start_new_session": True}
                )

                process = subprocess.Popen(
                    [sys.executable, "-m", "sellerpanel.photoworker"],
                    cwd=PROJECT,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    env={
                        **os.environ,
                        **gpuVars(gpu),
                        "MALLOC_ARENA_MAX": "2",
                        "FLATFAIR_PHOTO_WORKER_TOKEN": token,
                        "FLATFAIR_PHOTO_WARM": "1" if warm else "",
                    },
                    **options,
                )
            except Exception:
                resourceCache.delete(STARTING_KEY)
                startFail()

                raise

        threading.Thread(
            target=watchWork, args=(process, token, gpu), daemon=True, name="photo-worker-watch"
        ).start()

        return True
    except Exception:
        log.exception("Could not start the photo worker")

        return False


def queuePics(owner, paths, previousPaths=None):
    jobs = {}

    with transaction.atomic():
        lock_user(owner.pk)

        for room, path in paths.items():
            old = (previousPaths or {}).get(room)

            if old:
                PhotoCheckJob.objects.filter(
                    owner=owner, room=room, pendingPath=old, status__in=("queued", "running")
                ).update(
                    status="failed", finishedAt=timezone.now(), error="Replaced by a newer photo."
                )

            jobs[room] = PhotoCheckJob.objects.create(owner=owner, room=room, pendingPath=path)

        if jobs:
            transaction.on_commit(ensureWorker)

    return jobs


def workerAlive():
    if resourceCache.get(STARTING_KEY):
        return True

    with modelLock() as held:
        return not held


def checkNow(photos, seconds=(45)):
    paths = []
    ids = []

    try:
        for data in photos:
            path = default_storage.save(f"pending/{uuid.uuid4().hex}.jpg", ContentFile(data))
            paths.append(path)
            ids.append(PhotoCheckJob.objects.create(room="other", pendingPath=path).pk)

        waiting = PhotoCheckJob.objects.filter(pk__in=ids, status__in=("queued", "running"))
        started = time.monotonic()
        until = started + seconds
        limit = started + max(seconds, (120))
        checkAt = 0

        while waiting.exists():
            now = time.monotonic()

            if now >= limit or (now >= until and not workerAlive()):
                break

            if time.monotonic() >= checkAt:
                if not ensureWorker() and not workerAlive():
                    break

                checkAt = time.monotonic() + 1

            time.sleep(0.05)

        found = dict(
            PhotoCheckJob.objects.filter(pk__in=ids, status="done").values_list("pk", "result")
        )

        if len(found) != len(ids):
            log.warning("The photo worker did not answer, so a one-off check runs instead")

            return None

        answers = []

        for job_id in ids:
            answers.append(found[job_id])

        return answers
    except Exception:
        log.exception("The photo worker could not be asked, so a one-off check runs instead")

        return None
    finally:
        try:
            PhotoCheckJob.objects.filter(pk__in=ids).delete()

            for path in paths:
                default_storage.delete(path)
        except Exception:
            log.exception("Could not remove finished photo check requests")


def try_again(action):
    for attempt in range(10):
        try:
            return action()
        except OperationalError as error:
            if "locked" not in str(error).lower() or attempt == 9:
                raise

            time.sleep(0.02)


def claimJob(pk):
    with transaction.atomic():
        changed = PhotoCheckJob.objects.filter(pk=pk, status="queued").update(
            status="running", startedAt=timezone.now(), attempts=F("attempts") + 1
        )

        return PhotoCheckJob.objects.get(pk=pk) if changed else None


def claimJobs(limit=6):
    pool = try_again(
        lambda: list(
            PhotoCheckJob.objects.filter(status="queued")
            .order_by("createdAt", "pk")
            .values_list("pk", flat=True)[:limit]
        )
    )
    claimed = []

    for job_id in pool:
        job = try_again(lambda: claimJob(job_id))

        if job is not None:
            claimed.append(job)

    return claimed
