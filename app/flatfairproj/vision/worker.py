import logging
import multiprocessing
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor

from sellerpanel.device import (
    disableGpu,
    exitWorker,
    freeGpu,
    gpuVars,
    gpuUsedMb,
    pickGpu,
    startDevice,
)

log = logging.getLogger(__name__)
VISION_RSS_CAP_MB = 4200
VISION_HARD_MB = 6000
VISION_IDLE_SECONDS = 90
VISION_MAX_JOBS = 40
VISION_GPU_NEED_MB = 5000
_vision_pool = None
_vision_count = 0
_vision_gpu = None
_vision_run = threading.Lock()
_starting = threading.Lock()


def _workers(executor):
    jobs = getattr(executor, "_processes", None)

    if isinstance(jobs, dict):
        return dict(jobs)

    return {}


def _rss_mb(executor):
    total = 0

    for pid in _workers(executor):
        try:
            with open(f"/proc/{pid}/statm") as ram_file:
                total += int(ram_file.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            pass

    return total / 2**20


def trim():
    import gc

    gc.collect()
    freeGpu()

    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def _retire(executor, kill=False):
    global _vision_pool

    with _starting:
        if _vision_pool is executor:
            _vision_pool = None

    jobs = list(_workers(executor).values())
    executor.shutdown(wait=False, cancel_futures=True)

    if kill:
        for process in jobs:
            process.terminate()


def _quit_too():
    parent = os.getppid()

    def watch():
        while os.getppid() == parent:
            time.sleep(2)

        os._exit(0)

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


def memory_cap(hard_mb, gpu_hard_mb=0):
    def watch():
        page = os.sysconf("SC_PAGE_SIZE")

        while True:
            time.sleep(2)

            try:
                with open("/proc/self/statm") as ram_file:
                    used = int(ram_file.read().split()[1]) * page / 2**20
            except (OSError, ValueError, IndexError):
                continue

            if used > hard_mb:
                log.error("Vision worker used %d MB (limit %d MB); stopping it", used, hard_mb)
                os._exit(3)

            if gpu_hard_mb and gpuUsedMb() > gpu_hard_mb:
                disableGpu("an AI worker used too much GPU memory")
                os._exit(3)

    threading.Thread(target=watch, name="memory-guard", daemon=True).start()


def _start_up(hard_mb, gpu=None):
    os.environ.update(gpuVars(gpu))
    _quit_too()
    memory_cap(hard_mb, (6000))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "flatfair.settings")

    import django

    django.setup()
    use_device()


def use_device():
    import atexit
    from vision import pipeline

    pipeline.DEVICE = startDevice()
    log.info("Shop the look runs on the %s", "GPU" if pipeline.DEVICE == "cuda" else "CPU")

    if pipeline.DEVICE == "cuda":
        atexit.register(exitWorker)


def _new_pool():
    global _vision_gpu

    from django.conf import settings

    os.environ.setdefault("MALLOC_ARENA_MAX", "2")
    _vision_gpu = pickGpu(settings.FLATFAIR_GPU, VISION_GPU_NEED_MB)

    return ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_start_up,
        initargs=(VISION_HARD_MB, _vision_gpu),
    )


def gpu_failed(error):
    from vision import pipeline

    return pipeline.DEVICE == "cuda" and ("CUDA" in error or "cuDNN" in error)


def _scan_one(record_id):
    from django.db import close_old_connections
    from vision.models import PhotoAnalysis
    from vision.services import process

    close_old_connections()

    try:
        record = PhotoAnalysis.objects.filter(pk=record_id).select_related("photo").first()

        if record is not None:
            done = process(record.photo)

            if done.status == "failed" and gpu_failed(done.error):
                PhotoAnalysis.objects.filter(pk=record_id, status="failed").update(
                    status="pending", error=""
                )
                disableGpu("Shop the look failed on the GPU: " + done.error[:200])
                os._exit(5)

            return done.status
    except Exception:
        log.exception("Photo analysis failed")
    finally:
        close_old_connections()
        trim()


def _pick_one(pick_id):
    from django.db import close_old_connections
    from vision.models import ItemPick
    from vision.services import runPick

    close_old_connections()

    try:
        pick = ItemPick.objects.filter(pk=pick_id).select_related("analysis__photo").first()

        if pick is not None:
            done = runPick(pick)

            if done.status == "failed" and gpu_failed(done.error):
                disableGpu("Tap search failed on the GPU: " + done.error[:200])
                os._exit(5)

            return done.status
    except Exception:
        log.exception("Item pick failed")
    finally:
        close_old_connections()
        trim()


def run_pick(pick_id):
    global _vision_pool, _vision_count

    from vision.models import ItemPick
    from vision.pipeline import weights_ok

    if not weights_ok():
        ItemPick.objects.filter(pk=pick_id, status="pending").update(status="failed")

        return None

    with _vision_run:
        with _starting:
            if _vision_pool is None:
                _vision_pool = _new_pool()
                _vision_count = 0

            runner = _vision_pool

        try:
            status = runner.submit(_pick_one, pick_id).result(timeout=120)
        except Exception:
            log.exception("Item pick worker failed")
            ItemPick.objects.filter(pk=pick_id, status__in=("pending", "running")).update(
                status="failed"
            )

            if _vision_gpu is not None:
                disableGpu("the tap search worker failed or timed out on the GPU")

            _retire(runner, kill=True)

            return None

        _vision_count += 1

        if _vision_count >= VISION_MAX_JOBS or _rss_mb(runner) > VISION_RSS_CAP_MB:
            _retire(runner)

        return status


def run_vision(record_id):
    global _vision_pool, _vision_count

    from vision.pipeline import weights_ok

    if not weights_ok():
        return None

    with _vision_run:
        with _starting:
            if _vision_pool is None:
                _vision_pool = _new_pool()
                _vision_count = 0

            runner = _vision_pool

        try:
            status = runner.submit(_scan_one, record_id).result(timeout=(900))
        except Exception:
            log.exception("Photo analysis worker failed")

            if _vision_gpu is not None:
                disableGpu("the Shop the look worker failed or timed out on the GPU")

            _retire(runner, kill=True)

            return None

        _vision_count += 1

        if _vision_count >= VISION_MAX_JOBS or _rss_mb(runner) > VISION_RSS_CAP_MB:
            _retire(runner)

        return status


def end_vision():
    with _vision_run:
        if _vision_pool is not None:
            _retire(_vision_pool)


def vision_up():
    return _vision_pool is not None
