import argparse
import logging
import os
import signal
import tempfile
import threading
import time
from pathlib import Path

from .device import disableGpu, exitWorker, gpuChosen

IDLE_SECONDS = 300
MAX_JOBS = 200
MAX_LIFETIME_SECONDS = 900
HARD_MB = 3000
log = logging.getLogger(__name__)


def capMemory():
    if os.name != "nt":
        from vision.worker import memory_cap



        memory_cap(HARD_MB, 1500)

        return

    import ctypes
    from ctypes import wintypes



    class MemoryCounters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in (
                "PeakWorkingSetSize",
                "WorkingSetSize",
                "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage",
                "PagefileUsage",
                "PeakPagefileUsage",
            )
        ]



    getSelf = ctypes.windll.kernel32.GetCurrentProcess
    getSelf.restype = wintypes.HANDLE
    getRam = ctypes.windll.psapi.GetProcessMemoryInfo
    getRam.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    getRam.restype = wintypes.BOOL

    def watch():
        tally = MemoryCounters()
        tally.cb = ctypes.sizeof(tally)
        process = getSelf()

        while True:
            if getRam(process, ctypes.byref(tally), tally.cb):
                if tally.WorkingSetSize > HARD_MB * 2**20:
                    os._exit(3)

            time.sleep(2)

    threading.Thread(target=watch, daemon=True, name="photo-memory-guard").start()


def runWorker(
    once=False,
    idleSeconds=IDLE_SECONDS,
    maxJobs=MAX_JOBS,
    maxLifetime=MAX_LIFETIME_SECONDS,
    loader=None,
    sleep=time.sleep,
    onGpu=False,
):
    from django.core.files.storage import default_storage
    from django.db import close_old_connections
    from django.utils import timezone
    from storefront.models import PhotoCheckJob
    from .photocheck import TIMEOUT, loadScorer
    from .device import modelLock



    from .photoqueue import (
        HEARTBEAT_KEY,
        STARTING_KEY,
        MAX_ATTEMPTS,
        claimJobs,
        fixStale,
        resourceCache,
        markClaim,
    )

    done = 0
    started = idleAt = time.monotonic()

    with modelLock() as held:
        if not held:
            return 0

        oldBeat = resourceCache.get(HEARTBEAT_KEY, 0)
        fixStale(locked=True, heartbeat=oldBeat)
        fixedAt = time.monotonic()
        resourceCache.delete(STARTING_KEY)



        resourceCache.set(HEARTBEAT_KEY, time.time(), 180)
        stopped = threading.Event()
        until = [None]



        def heartbeat():
            try:
                while not stopped.wait(5):
                    if until[0] is not None and time.monotonic() > until[0]:
                        if onGpu:
                            disableGpu("a photo batch took too long on the GPU")

                        os._exit(4)


                    if time.monotonic() - started > maxLifetime:
                        os._exit(4)

                    resourceCache.set(HEARTBEAT_KEY, time.time(), 180)
            finally:
                close_old_connections()

        thread = threading.Thread(target=heartbeat, daemon=True, name="photo-heartbeat")
        thread.start()
        scorer = None

        if os.environ.get("FLATFAIR_PHOTO_WARM") == "1":
            scorer = (loader or loadScorer)()

        try:
            while done < maxJobs and time.monotonic() - started < maxLifetime:
                if time.monotonic() - fixedAt >= 5:
                    fixStale(locked=True, heartbeat=oldBeat)
                    fixedAt = time.monotonic()

                jobs = claimJobs(min(6, maxJobs - done))

                if not jobs:
                    if once or time.monotonic() - idleAt >= idleSeconds:
                        break

                    sleep(min(0.1, max(0, idleSeconds)))

                    continue

                if not done:
                    markClaim(os.environ.get("FLATFAIR_PHOTO_WORKER_TOKEN"))

                until[0] = time.monotonic() + TIMEOUT

                try:
                    with tempfile.TemporaryDirectory() as folder:
                        paths, ready = [], []

                        for index, job in enumerate(jobs):
                            if not PhotoCheckJob.objects.filter(
                                pk=job.pk, status="running"
                            ).exists():
                                continue



                            path = Path(folder) / f"{index}.jpg"

                            try:
                                with default_storage.open(job.pendingPath, "rb") as source:
                                    path.write_bytes(source.read())
                            except (OSError, ValueError) as error:
                                failed = job.attempts >= MAX_ATTEMPTS

                                PhotoCheckJob.objects.filter(pk=job.pk, status="running").update(
                                    status="failed" if failed else "queued",
                                    error=str(error)[:2000],
                                    result={"unchecked": True} if failed else {},
                                    finishedAt=timezone.now() if failed else None,
                                )

                                log.warning(
                                    "Could not read photo check job %s", job.pk, exc_info=True
                                )


                                continue

                            paths.append(str(path))
                            ready.append(job)

                        if paths:
                            if scorer is None:
                                scorer = (loader or loadScorer)()


                            results = scorer(paths)
                        else:
                            results = []

                    if len(results) != len(ready):
                        raise ValueError("The photo checker returned an incomplete batch")

                    for job, result in zip(ready, results):
                        PhotoCheckJob.objects.filter(pk=job.pk, status="running").update(
                            status="done",
                            result=result or {"unchecked": True},
                            error="",
                            finishedAt=timezone.now(),
                        )
                except Exception as error:
                    log.exception("Photo batch failed")

                    for job in jobs:
                        failed = job.attempts >= MAX_ATTEMPTS

                        PhotoCheckJob.objects.filter(pk=job.pk, status="running").update(
                            status="failed" if failed else "queued",
                            error=str(error)[:2000],
                            result={"unchecked": True} if failed else {},
                            finishedAt=timezone.now() if failed else None,
                        )

                    if onGpu:
                        disableGpu("a photo batch failed on the GPU")

                        break
                finally:
                    until[0] = None

                done += len(jobs)


                idleAt = time.monotonic()

                if once:
                    break
        finally:
            stopped.set()
            thread.join(timeout=6)
            resourceCache.delete(HEARTBEAT_KEY)

    return done


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "flatfair.settings")

    import django

    django.setup()
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--idle-seconds", type=float, default=IDLE_SECONDS)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    capMemory()



    runWorker(once=args.once, idleSeconds=max(0, args.idle_seconds), onGpu=gpuChosen())

    if gpuChosen():
        exitWorker()


if __name__ == "__main__":
    main()
