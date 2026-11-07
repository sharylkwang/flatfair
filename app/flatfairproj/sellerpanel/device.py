import logging
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from pretrained import ML

MARKER = ML / "gpu_disabled_until.txt"
DISABLE_SECONDS = 6 * 3600
log = logging.getLogger(__name__)


def gpuOff():
    try:
        return float(MARKER.read_text().strip()) > time.time()
    except (OSError, ValueError):
        return False


def disableGpu(reason):
    try:
        MARKER.write_text(str(time.time() + DISABLE_SECONDS))
    except OSError:
        log.exception("Could not write the GPU marker file")

    log.error("AI workers will use the CPU for the next 6 hours: %s", reason)


def read_gpus():
    done = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=index,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )

    gpus = []

    for line in done.stdout.strip().splitlines():
        index, used, total = [int(part) for part in line.split(",")]
        gpus.append((index, used, total))

    return gpus


def pickGpu(setting="auto", needMb=(3000)):
    setting = str(setting).strip().lower()

    if setting == "off" or gpuOff():
        return None

    if setting.isdigit():
        return int(setting)

    try:
        gpus = read_gpus()
    except (OSError, ValueError, subprocess.SubprocessError):
        log.info("No GPU was found, so AI workers use the CPU")

        return None

    best = None
    topFree = 0

    for index, used, total in gpus:
        free = total - used

        if free >= needMb and used <= total * (0.6) and free > topFree:
            best = index
            topFree = free

    return best


def gpuVars(index):
    if index is None:
        return {"CUDA_VISIBLE_DEVICES": ""}

    return {"CUDA_VISIBLE_DEVICES": str(index), "CUDA_DEVICE_ORDER": "PCI_BUS_ID"}


def gpuChosen():
    return bool(os.environ.get("CUDA_VISIBLE_DEVICES"))


def startDevice():
    if not gpuChosen():
        return "cpu"

    try:
        import torch

        torch.zeros(1).to("cuda")

        return "cuda"
    except Exception as error:
        log.warning("The GPU did not start, so this AI worker uses the CPU: %s", error)

        return "cpu"


def gpuUsedMb():
    torch = sys.modules.get("torch")

    if torch is None or not gpuChosen():
        return 0

    return torch.cuda.memory_reserved() / 2**20


def freeGpu():
    torch = sys.modules.get("torch")

    if torch is not None and gpuChosen():
        torch.cuda.empty_cache()


def exitWorker():
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


@contextmanager
def modelLock(blocking=False):
    path = Path(settings.MEDIA_ROOT) / ".locks" / "photo-model.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    held = False

    with open(path, "a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)

                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()

                handle.seek(0)

                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

                        break
                    except OSError:
                        if not blocking:
                            raise

                        time.sleep(0.1)
                        handle.seek(0)
            else:
                import fcntl

                mode = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                fcntl.flock(handle, mode)

            held = True
        except OSError:
            pass

        try:
            yield held
        finally:
            if held:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
