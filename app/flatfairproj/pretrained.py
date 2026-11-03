import hashlib
import json
import math
import os
from functools import lru_cache
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
ASSETS = PROJECT / "assets"
ML = ASSETS / "ml"
TRAINING = ASSETS / "training"
MANIFEST = TRAINING / "model_manifest.json"
CLIP_NAME = "openai/clip-vit-base-patch32"

PHOTO_LABELS = [
    ("a photo of the living room of an apartment", "living room", True),
    ("a photo of a kitchen", "kitchen", True),
    ("a photo of a bedroom", "bedroom", True),
    ("a photo of a bathroom or toilet", "toilet", True),
    ("a photo of a balcony", "balcony", True),
    ("a photo of a laundry area or service yard", "service yard", True),
    ("a view of buildings from an apartment window", "window view", True),
    ("a floor plan drawing of an apartment", "floor plan", True),
    ("a photo of an empty room in an apartment", "empty room", True),
    ("a photo of a corridor or entrance inside an apartment", "corridor", True),
    ("a selfie", "selfie", False),
    ("a photo of a person", "person", False),
    ("a group photo of people", "group photo", False),
    ("a photo of food or a drink", "food", False),
    ("a photo of ice cream or dessert", "dessert", False),
    ("a photo of a pet animal", "pet", False),
    ("a screenshot of a phone screen or text", "screenshot", False),
    ("a cartoon or meme", "cartoon", False),
    ("a photo of a car", "car", False),
    ("a photo of a beach, park or nature", "outdoor scene", False),
]

PIPELINE_VERSION = "7"
MODEL_VERSION = "package-v1"
FEATURE_NAMES = [
    "town",
    "flatType",
    "floorAreaSqm",
    "storey",
    "estimatedPrice",
    "askingRatio",
    "bandPosition",
    "townTransactions",
    "leaseRemaining",
    "daysListed",
]
PACKAGE_TIERS = {"none": 0, "boost30": 1, "boost90": 2, "topSpot": 3}

PINNED_REVISIONS = {
    CLIP_NAME: "3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268",
    "IDEA-Research/grounding-dino-tiny": "a2bb814dd30d776dcf7e30523b00659f4f141c71",
    "Zigeng/SlimSAM-uniform-77": "79c09c1ce6b4ae51f00634ed171d9b8e888f6911",
    "florence-community/Florence-2-base": "00921df66db728a9ceb750f5eca43e5c203a2051",
    "depth-anything/Depth-Anything-V2-Small-hf": "5426e4f0f36572d16453bbda7a8389317b1bef99",
}

REVISION_ENV = dict(
    zip(
        PINNED_REVISIONS,
        [
            "PHOTO_MODEL_REVISION",
            "GROUNDING_DINO_REVISION",
            "SLIMSAM_REVISION",
            "FLORENCE_REVISION",
            "DEPTH_REVISION",
        ],
    )
)
REVISIONS = {
    name: os.environ.get(REVISION_ENV[name], revision)
    for name, revision in PINNED_REVISIONS.items()
}
MODEL_PATHS = dict(
    zip(
        PINNED_REVISIONS,
        ["clip", "vision/grounding_dino", "vision/slimsam", "vision/florence2", "depth"],
    )
)
MODELS = [(name, folder) for name, folder in MODEL_PATHS.items() if folder != "clip"]


def file_info(path):
    stat = path.stat()
    content = hashlib.sha256(path.read_bytes()).hexdigest() if stat.st_size <= 65536 else None

    return (
        str(path.resolve()),
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_ino,
        content,
    )


def file_cache(path, size=1):
    from functools import wraps

    def remember(function):
        @lru_cache(maxsize=size)
        def saved(stamps, *args, **kwargs):
            return function(*args, **kwargs)

        @wraps(function)
        def read(*args, **kwargs):
            files = path()

            if isinstance(files, (str, Path)):
                files = (files,)

            stamps = []

            for file in files:
                try:
                    stamps.append(file_info(Path(file)))
                except Exception:
                    stamps.append((str(file), None))

            return saved(tuple(stamps), *args, **kwargs)

        read.cache_clear = saved.cache_clear
        read.cache_info = saved.cache_info
        read.cache_parameters = saved.cache_parameters
        return read

    return remember


@lru_cache(maxsize=256)
def saved_hash(details):
    if details[-1] is not None:
        return details[-1]

    with Path(details[0]).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def file_hash(path):
    return saved_hash(file_info(path))


def modelEntry(name):
    try:
        sheet = read_model(file_info(MANIFEST))

        return next(entry for entry in sheet["pretrained"] if entry["modelId"] == name)
    except Exception as error:
        return None


def verifyModel(name, folder=None, revision=None, entry=None):
    folder = Path(folder) if folder is not None else ML / MODEL_PATHS[name]
    revision = revision if revision is not None else REVISIONS[name]
    entry = entry if entry is not None else modelEntry(name)

    if not folder.is_dir():
        return "not installed"

    if not entry or entry.get("status") != "installed" or entry.get("revision") != revision:
        return "identity mismatch"

    files = entry.get("files", [])
    names = {file["path"] for file in files}

    if "config.json" not in names or not any(
        name.endswith((".safetensors", ".bin")) for name in names
    ):
        return "identity mismatch"

    try:
        actual = {
            file.name
            for file in folder.iterdir()
            if file.is_file() and file.suffix in {".json", ".txt", ".safetensors", ".bin"}
        }

        if actual != names:
            return "identity mismatch"

        for file in files:
            path = folder / file["path"]

            if path.stat().st_size != file["sizeBytes"] or file_hash(path) != file["sha256"]:
                return "identity mismatch"
    except Exception as error:
        return "identity mismatch"

    return "verified"


def modelTag(name, folder=None, revision=None, entry=None):
    entry = entry if entry is not None else modelEntry(name)

    return {
        "status": verifyModel(name, folder, revision, entry),
        "fingerprint": hashlib.sha256(
            json.dumps(entry.get("files", []) if entry else [], sort_keys=True).encode()
        ).hexdigest(),
    }


def picVersion(folder, name, revision, entry=None):
    folder = Path(folder)
    parts = [name, revision, json.dumps(modelTag(name, folder, revision, entry), sort_keys=True)]

    for head in ["photo_head.joblib", "aigen_head.joblib"]:
        try:
            head_id = file_hash(folder.parent / head)
        except Exception as error:
            head_id = "unavailable"

        parts.append(f"{head}:{head_id}")

    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def pipelineIdentity():
    return {
        "version": PIPELINE_VERSION,
        "models": dict(REVISIONS),
        "installed": {name: modelTag(name) for name in REVISIONS},
    }


def tagErrors():
    issues = []

    for name in REVISIONS:
        status = verifyModel(name)

        if status != "verified":
            issues.append(f"{name}: {status}")

    return issues


class ArtifactUnavailable(ValueError):
    pass


@lru_cache(maxsize=128)
def warnGone(log, message):
    log.warning(message)


def packTraits(
    town,
    flatType,
    floorAreaSqm,
    storey,
    leaseCommenceYear,
    askingPrice,
    estimatedPrice,
    bandLow,
    bandHigh,
    daysListed,
    asOfYear,
    townTransactions,
):
    values = [
        float(value)
        for value in (
            floorAreaSqm,
            storey,
            leaseCommenceYear,
            askingPrice,
            estimatedPrice,
            bandLow,
            bandHigh,
            daysListed,
            asOfYear,
            townTransactions,
        )
    ]

    if not all(math.isfinite(value) for value in values):
        raise ValueError("Package features must be finite.")

    area, floor, lease, asking, worth, low, high, days, year, rivals = values

    if area <= 0 or asking <= 0 or not 0 < low < worth < high or rivals < 0:
        raise ValueError("Package features need a valid price band and listing.")

    return {
        "town": town.strip().upper(),
        "flatType": flatType.strip().upper(),
        "floorAreaSqm": area,
        "storey": floor,
        "estimatedPrice": worth,
        "askingRatio": asking / worth,
        "bandPosition": (asking - low) / (high - low),
        "townTransactions": rivals,
        "leaseRemaining": max(0.0, min(99.0, 99 - (year - lease))),
        "daysListed": max(0.0, days),
    }
