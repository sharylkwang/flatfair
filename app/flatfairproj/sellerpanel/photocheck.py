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

resourceCache = caches["resources"]
PROJECT = Path(__file__).resolve().parent.parent
ML = ASSETS / "ml" / "clip"
CLIP_REVISION = REVISIONS[CLIP_NAME]
EMBEDDING_SIZE = 512
HEAD_VERSION = "photo-v2"
BLOCK_AT = 0.80
REVIEW_AT = 0.50
SLOT_CONFIDENCE = 0.60
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
AI_REVIEW_AT = 1.01
AI_CLEAR_AT = 0.70
TIMEOUT = 120
PHOTO_BUSY = "Photo checks are busy right now. Please try again in a moment."
TRUST_CLIP_FOR = ["balcony", "service yard", "window view"]
FREQUENCY_RINGS = [16, 32, 48]

TAGS = [
    "sofa",
    "dining table",
    "bed",
    "wardrobe",
    "study desk",
    "TV console",
    "kitchen cabinets",
    "ceiling fan",
    "air conditioner",
    "wood floor",
    "tiled floor",
    "marble floor",
    "window view",
    "plants",
    "toilet bowl",
    "washing machine",
]
log = logging.getLogger(__name__)
_heads = None


def modelVersion():
    return picVersion(ML, CLIP_NAME, CLIP_REVISION)


MODEL_VERSION = modelVersion()


def get_cutoffs():
    try:
        metrics = json.loads((TRAINING / "photo_metrics.json").read_text())
        block = metrics["thresholds"]["block"]
        review = metrics["thresholds"]["review"]
        ai_cutoff = metrics["ai"]["threshold"]


        if (
            not all(
                isinstance(value, Real) and math.isfinite(value) and 0 <= value <= 1
                for value in (block, review, ai_cutoff)
            )
            or review > block
        ):
            raise ValueError("Invalid photo thresholds")

        return block, review, ai_cutoff
    except (OSError, ValueError, KeyError, TypeError):
        return BLOCK_AT, REVIEW_AT, AI_REVIEW_AT


BLOCK_AT, REVIEW_AT, AI_REVIEW_AT = get_cutoffs()
HOLD_AT = REVIEW_AT + (BLOCK_AT - REVIEW_AT) / 2
AI_HOLD_AT = max(AI_REVIEW_AT, AI_CLEAR_AT)


def heads():
    global _heads

    if _heads is None:
        loaded = []

        for name in ["photo_head.joblib", "aigen_head.joblib"]:
            try:
                import joblib

                head = joblib.load(ML.parent / name)

                try:
                    check_head(head, name == "aigen_head.joblib")
                except (KeyError, TypeError, AttributeError, ValueError) as error:
                    raise ArtifactUnavailable(f"Invalid classifier schema: {error}") from error

                loaded.append(head)
            except (OSError, EOFError, ValueError) as error:
                warnGone(log, f"Photo check head unavailable: {name}: {error}")
                loaded.append(None)
            except Exception:
                log.exception("Photo check head loading failed: %s", name)
                loaded.append(None)


        _heads = tuple(loaded)

    return _heads


def check_head(head, ai):
    import numpy as np

    if not isinstance(head, dict):
        raise ValueError("Expected a classifier artifact")

    if head.get("version") != HEAD_VERSION:
        raise ValueError("Incompatible classifier version")

    model = head["model"]
    classes = np.asarray(model.classes_)


    if not callable(getattr(model, "predict_proba", None)):
        raise ValueError("Expected a probability classifier")

    if classes.ndim != 1 or len(classes) < 2 or len(set(classes.tolist())) != len(classes):
        raise ValueError("Invalid classifier classes")

    if ai:
        if classes.tolist() != [False, True]:
            raise ValueError("Expected real and AI classes in that order")

        if not isinstance(head.get("frequency", False), bool):
            raise ValueError("Invalid frequency feature setting")
    else:
        if not all(isinstance(name, str) for name in classes):
            raise ValueError("Expected room labels")

        if not isinstance(head["relevant"], (list, tuple, set)) or not all(
            isinstance(name, str) for name in head["relevant"]
        ):
            raise ValueError("Expected relevant room labels")

    traitNum = 512 + (len(FREQUENCY_RINGS) if ai and head.get("frequency") else 0)



    if model.n_features_in_ != traitNum:
        raise ValueError("Incompatible classifier feature count")

    values = [head["thresholds"]["review"]]

    if not ai:
        values.extend([head["thresholds"]["block"], head["hybrid_confidence"]])

    if not all(
        isinstance(value, Real) and math.isfinite(value) and 0 <= value <= 1 for value in values
    ):
        raise ValueError("Invalid classifier thresholds")

    if not ai and head["thresholds"]["review"] > head["thresholds"]["block"]:
        raise ValueError("Invalid classifier threshold order")


def headOdds(head, features):
    import numpy as np

    odds = np.asarray(head["model"].predict_proba(features), dtype=float)

    if odds.shape != (1, len(head["model"].classes_)):
        raise ValueError("Invalid classifier probability shape")

    if not np.isfinite(odds).all() or (odds < 0).any() or (odds > 1).any():
        raise ValueError("Invalid classifier probabilities")

    if not np.isclose(odds.sum(), 1):
        raise ValueError("Classifier probabilities must sum to one")

    return odds[0]


def slotWarn(result, slot):
    room_words = {
        "bedroom": "bedroom",
        "bathroom": "toilet",
        "toilet": "toilet",
        "kitchen": "kitchen",
        "living room": "living room",
        "dining room": "dining room",
    }
    slot_words = {
        "bedroom": "bedroom",
        "toilet": "toilet",
        "kitchen": "kitchen",
        "living_room": "living room",
    }

    if not result or slot not in ROOM_LABELS:
        return ""



    if result.get("labelSource") == "clip-override":
        label = result.get("label")

        fitSlots = {"balcony": "balcony", "service yard": "service_yard", "window view": "view"}

        guessed = fitSlots.get(label)

        if guessed and guessed != slot:
            return (
                f"This looks like a {label}, not the {ROOM_LABELS[slot]}. Our team will check it."
            )

        return ""

    if slot == "other":
        return ""

    trust = result.get("roomConfidence")


    if not isinstance(trust, Real) or not math.isfinite(trust) or not SLOT_CONFIDENCE <= trust <= 1:
        return ""

    label = str(result.get("roomLabel", "")).replace("_", " ").lower()

    wanted = "bedroom" if "bedroom" in slot else "toilet" if "toilet" in slot else slot

    if label not in room_words or wanted not in SLOT_ROOMS:
        return ""

    if label in SLOT_ROOMS[wanted] or SLOT_PROOF[wanted].intersection(result.get("tags") or []):
        return ""

    return (
        f"This looks like a {room_words[label]}, not a {slot_words[wanted]}. "
        f"Please check it’s the right photo for {ROOM_LABELS[slot]}."
    )


def holdAt(result):
    review = result.get("reviewAt", REVIEW_AT)

    return review + (max(result.get("blockAt", BLOCK_AT), review) - review) / 2


def borderline(result):
    score = (result or {}).get("irrelevant")

    return isinstance(score, Real) and result.get("reviewAt", REVIEW_AT) <= score < holdAt(result)


def reviewReasons(result):
    if not result or result.get("unchecked"):
        return ["photo check unavailable"]

    reasons = list(result.get("reviewReasons", []))

    if result.get("identityStatus") == "identity mismatch":
        reasons.append("Photo model identity mismatch; staff must review it")



    if result.get("modelVersion") != MODEL_VERSION:
        reasons.append("photo check is out of date; staff must review it")



    if result.get("irrelevant") is None:
        reasons.append("The room-photo check could not run")
    elif result["irrelevant"] >= holdAt(result):
        reasons.append("may not show the flat")

    if result.get("aiGenerated") is None:
        reasons.append("The AI-image check could not run")
    elif result["aiGenerated"] >= max(result.get("aiReviewAt", AI_REVIEW_AT), AI_CLEAR_AT):
        reasons.append(AI_REASON)

    return list(dict.fromkeys(reasons))


def isOn():
    return (ML / "model.safetensors").exists()


def photoGpu():
    setting = str(settings.FLATFAIR_GPU).strip().lower()

    if setting.isdigit():
        return pickGpu(setting)

    return None


def checkPics(photos):
    try:
        return runChecks(photos)
    except Exception:
        log.exception("Photo checks failed, so staff will check these photos.")

        return [None] * len(photos)


def runChecks(photos):
    if not photos:
        return []

    unseen = [None] * len(photos)



    if not isOn():
        warnGone(
            log, "Photo check is off: run `python manage.py download_photo_model` to turn it on."
        )

        return unseen

    if verifyModel(CLIP_NAME, ML, CLIP_REVISION) != "verified":
        warnGone(log, "Photo model unavailable: identity mismatch")
        misfits = []

        for photo in photos:
            misfits.append({"identityStatus": "identity mismatch", "modelVersion": MODEL_VERSION})



        return misfits

    if settings.PHOTO_WORKER_ON and not connection.in_atomic_block:
        from .photoqueue import checkNow

        results = checkNow(photos)

        if results is not None:
            return results

    gpu = photoGpu()


    token = uuid.uuid4().hex
    slotKey = None

    for slot in range(2):
        key = f"photo-check-slot:{slot}"

        if resourceCache.add(key, token, TIMEOUT + 30):
            slotKey = key

            break

    if slotKey is None:
        busy = []

        for photo in photos:
            busy.append({"busy": True, "unchecked": True})

        return busy

    try:
        with tempfile.TemporaryDirectory() as folder:
            paths = []

            for number, data in enumerate(photos):
                path = Path(folder) / f"{number}.jpg"
                path.write_bytes(data)
                paths.append(str(path))

            command = [sys.executable, "-m", "sellerpanel.photocheck"] + paths

            done = subprocess.run(
                command,
                cwd=PROJECT,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                env={**os.environ, **gpuVars(gpu)},
            )

        if done.returncode != 0:
            log.error("Photo check stopped with code %s: %s", done.returncode, done.stderr[-500:])

            return unseen

        results = json.loads(done.stdout.strip().splitlines()[-1])

        if len(results) != len(photos):
            log.error("Photo check gave %s results for %s photos", len(results), len(photos))

            return unseen

        return results
    except subprocess.TimeoutExpired:
        log.error("Photo check took over %s seconds, so staff will check these photos.", TIMEOUT)

        if gpu is not None:
            disableGpu("a photo check timed out on the GPU")

        return unseen
    except Exception:
        log.exception("Photo check failed, so staff will check these photos.")

        return unseen
    finally:
        if resourceCache.get(slotKey) == token:
            resourceCache.delete(slotKey)


def unit(features):
    return features / features.norm(dim=-1, keepdim=True)


def pickTags(chances):
    ranked = sorted(zip(chances, TAGS), reverse=True)
    tags = []

    for chance, tag in ranked[:4]:
        if chance >= 0.1:
            tags.append(tag)

    return tags


def textFeatures(model, processor, prompts):
    text = processor(text=prompts, return_tensors="pt", padding=True).to(model.device)

    return unit(model.text_projection(model.text_model(**text).pooler_output))


def tagFeatures(model, processor):
    templates = ["a photo of a {}.", "a photo of a room with a {}.", "a {} in a home."]

    import torch

    rows = []

    for tag in TAGS:
        prompts = []

        for pattern in templates:
            prompts.append(pattern.format(tag))

        rows.append(unit(textFeatures(model, processor, prompts).mean(dim=0)))

    return torch.stack(rows)


def loadScorer():
    if not isOn():
        return lambda paths: [None for path in paths]

    if verifyModel(CLIP_NAME, ML, CLIP_REVISION) != "verified":
        warnGone(log, "Photo model unavailable: identity mismatch")


        return lambda paths: [
            {"identityStatus": "identity mismatch", "modelVersion": MODEL_VERSION} for path in paths
        ]

    import torch
    from PIL import Image
    from transformers import CLIPModel, CLIPProcessor

    device = startDevice()
    log.info("The photo model runs on the %s", "GPU" if device == "cuda" else "CPU")
    torch.set_num_threads(4)
    model = CLIPModel.from_pretrained(ML, local_files_only=True).eval()

    if device == "cuda":
        model.to("cuda")

    prep = CLIPProcessor.from_pretrained(ML, local_files_only=True)

    with torch.inference_mode():
        labelFeatures = textFeatures(model, prep, [label[0] for label in LABELS])
        tagWords = tagFeatures(model, prep)



    room, ai = heads()

    def score(paths):
        images = []

        try:
            for path in paths:
                with Image.open(path) as source:
                    images.append(source.convert("RGB"))

            results = []

            with torch.inference_mode():
                pixels = prep(images=images, return_tensors="pt")["pixel_values"]

                if device == "cuda":
                    pixels = pixels.to("cuda")

                traits = unit(
                    model.visual_projection(model.vision_model(pixel_values=pixels).pooler_output)
                )

                chances = (
                    (model.logit_scale.exp() * traits @ labelFeatures.T).softmax(dim=-1).tolist()
                )


                tagOdds = (model.logit_scale.exp() * traits @ tagWords.T).softmax(dim=-1).tolist()

                if device == "cuda":
                    traits = traits.cpu()


                for index, image in enumerate(images):
                    vector = traits[index : index + 1]
                    result = decide(vector.numpy(), chances[index], image)
                    result["tags"] = pickTags(tagOdds[index])

                    result["embedding"] = [round(number, 4) for number in vector[0].tolist()]

                    result["modelVersion"] = MODEL_VERSION

                    result["blockAt"] = room["thresholds"]["block"] if room else BLOCK_AT

                    result["reviewAt"] = room["thresholds"]["review"] if room else REVIEW_AT

                    result["aiReviewAt"] = ai["thresholds"]["review"] if ai else AI_REVIEW_AT


                    result["reviewReasons"] = reviewReasons(result)
                    results.append(result)

            return results
        finally:
            for image in images:
                image.close()

    return score


def scoreFiles(paths):
    return loadScorer()(paths)


def frequencyFeatures(image):
    import numpy as np

    grey = np.asarray(image.resize((128, 128)).convert("L"), dtype=float) / 255


    power = np.abs(np.fft.fftshift(np.fft.fft2(grey - grey.mean()))) ** 2
    y_shifts, x_shifts = np.ogrid[-64:64, -64:64]

    radius = np.sqrt(x_shifts * x_shifts + y_shifts * y_shifts)

    shares = []

    for ring in FREQUENCY_RINGS:
        shares.append(power[radius > ring].sum() / (power.sum() + 1e-9))

    return [shares]


def decide(embedding, chances, image=None):
    labels = ["floor plan", "floor_plan"]

    import numpy as np

    room, ai_head = heads()
    best = int(np.argmax(chances))
    topName = LABELS[best][1]
    label = topName



    tagFrom = "clip"
    unfit = None
    roomTag = None
    roomSure = None

    if room is not None:
        try:
            odds = headOdds(room, embedding)
            names = room["model"].classes_
            winner = int(np.argmax(odds))
            roomUnfit = 0.0

            for chance, name in zip(odds, names):
                if name not in room["relevant"] or name in labels:
                    roomUnfit += float(chance)

            label = str(names[winner])
            tagFrom = "room-head"
            roomTag = label
            roomSure = float(odds[winner])

            if topName in TRUST_CLIP_FOR and chances[best] >= room["hybrid_confidence"]:
                roomUnfit = 0.0
                label = topName
                tagFrom = "clip-override"

            unfit = float(roomUnfit)
        except Exception:
            log.exception("Room-photo check could not run")

    fake = None

    if ai_head is not None:
        try:
            traits = embedding

            if ai_head.get("frequency"):
                traits = np.concatenate([embedding, frequencyFeatures(image)], axis=1)

            fake = float(headOdds(ai_head, traits)[1])
        except Exception:
            log.exception("AI-image check could not run")


    return {
        "irrelevant": unfit,
        "label": label,
        "labelSource": tagFrom,
        "aiGenerated": fake,
        "roomLabel": roomTag,
        "roomConfidence": roomSure,
    }


def manualQ(revision):
    return Q(
        staffReviewedBy__isnull=False, staffReviewedAt__isnull=False, staffReviewedRevision=revision
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
