import json
import logging
import math
import os
import subprocess
import sys
import tempfile
import uuid
from numbers import Real
from pathlib import Path
from django.conf import settings
from django.core.cache import caches
from django.db import connection
from django.db.models import F, Q
from pretrained import (
    ASSETS, CLIP_NAME, REVISIONS, TRAINING, picVersion,
    verifyModel, ArtifactUnavailable, warnGone, PHOTO_LABELS as LABELS,
)
from storefront.models import ROOM_LABELS
from .device import disableGpu, exitWorker, gpuChosen, gpuVars, pickGpu, startDevice

ML = ASSETS / "ml" / "clip"
BLOCK_AT = 0.80
REVIEW_AT = 0.50
SLOT_ROOMS = {
    "bedroom": {"bedroom"},
    "toilet": {"bathroom", "toilet"},
    "kitchen": {"kitchen", "dining room"},
    "living_room": {"living room", "dining room", "kitchen"},
}
SLOT_PROOF = {
    "bedroom": {"bed"},
    "toilet": {"toilet bowl"},
    "kitchen": {"kitchen cabinets"},
    "living_room": {"sofa", "TV console", "dining table"},
}
AI_REASON = "may be AI-generated"
AI_CLEAR_AT = 0.70
TIMEOUT = 120
log = logging.getLogger(__name__)
BLOCK_AT, REVIEW_AT, AI_REVIEW_AT = get_cutoffs()
HOLD_AT = REVIEW_AT + (BLOCK_AT - REVIEW_AT) / 2
AI_HOLD_AT = max(AI_REVIEW_AT, AI_CLEAR_AT)


def holdAt(result):
    review = result.get("reviewAt", REVIEW_AT)

    return review + (max(result.get("blockAt", BLOCK_AT), review) - review) / 2


def borderline(result):
    score = (result or {}).get("irrelevant")

    return isinstance(score, Real) and result.get("reviewAt", REVIEW_AT) <= score < holdAt(result)


def isOn():
    return (ML / "model.safetensors").exists()


def photoGpu():
    setting = str(settings.FLATFAIR_GPU).strip().lower()

    if setting.isdigit():
        return pickGpu(setting)

    return None


def unit(features):
    return features / features.norm(dim=-1, keepdim=True)


def manualQ(revision):
    return Q(
        staffReviewedBy__isnull=False,
        staffReviewedAt__isnull=False,
        staffReviewedRevision=revision,
    )


def missingQ():
    return (
        Q(modelVersion__isnull=True)
        | ~Q(modelVersion=MODEL_VERSION)
        | Q(aiIrrelevant__isnull=True)
        | Q(aiGenerated__isnull=True)
    )


def doubtQ(prefix=""):
    flagged = (
        ~Q(**{prefix + "photoReview": []})
        | Q(**{prefix + "aiIrrelevant__gte": HOLD_AT})
        | Q(**{prefix + "aiGenerated__gte": AI_HOLD_AT})
        | Q(**{prefix + "aiIrrelevant__isnull": True})
        | Q(**{prefix + "aiGenerated__isnull": True})
    )

    stale = Q(**{prefix + "modelVersion__isnull": True}) | ~Q(
        **{prefix + "modelVersion": MODEL_VERSION}
    )

    manual = Q(
        **{
            prefix + "staffReviewedBy__isnull": False,
            prefix + "staffReviewedAt__isnull": False,
            prefix + "staffReviewedRevision": F(prefix + "listing__revision"),
        }
    )

    return (stale | (flagged & Q(**{prefix + "staffChecked": False}))) & ~manual


if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "flatfair.settings")

    import django

    django.setup()

    from sellerpanel.device import modelLock
    from sellerpanel.photoworker import capMemory

    capMemory()

    with modelLock(blocking=True) as held:
        if not held:
            raise SystemExit(1)

        print(json.dumps(scoreFiles(sys.argv[1:])))

    if gpuChosen():
        exitWorker()
