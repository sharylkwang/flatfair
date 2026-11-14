import csv
import hashlib
import importlib.metadata
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = next(
    path
    for path in (HERE.parent / "flatfairproj", HERE.parents[1])
    if (path / "manage.py").is_file()
)
ROOT = PROJECT.parents[1]
sys.path.insert(0, str(PROJECT))

import pretrained


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pretrainedEntries():
    entries = []
    cache = Path.home() / ".cache" / "huggingface" / "hub"

    for name, folder in pretrained.MODEL_PATHS.items():
        path = pretrained.ML / folder
        reference = cache / ("models--" + name.replace("/", "--")) / "refs" / "main"
        meta = path / ".cache" / "huggingface" / "download" / "config.json.metadata"
        source = meta if meta.is_file() else reference
        seen = source.read_text().splitlines()[0].strip() if source.is_file() else None

        installed = (path / "config.json").is_file() and any(
            (path / file).is_file() for file in ["model.safetensors", "pytorch_model.bin"]
        )

        files = []

        if path.is_dir():
            for file in sorted(path.iterdir()):
                if file.is_file() and file.suffix in {".json", ".txt", ".safetensors", ".bin"}:
                    files.append(
                        {
                            "path": file.name,
                            "sizeBytes": file.stat().st_size,
                            "sha256": file_hash(file),
                        }
                    )

        entries.append(
            {
                "modelId": name,
                "localPath": folder,
                "resolvedPath": str(path.relative_to(PROJECT)),
                "status": "installed" if installed else "not installed",
                "revision": seen or pretrained.PINNED_REVISIONS[name],
                "revisionSource": (
                    (
                        "~/" + str(source.relative_to(Path.home()))
                        if source.is_relative_to(Path.home())
                        else str(source)
                    )
                    if seen
                    else "Pinned default; no local revision evidence"
                ),
                "files": files,
            }
        )

    return entries


def photoIdentity(entry=None):
    revision = pretrained.REVISIONS[pretrained.CLIP_NAME]

    return {
        "modelId": pretrained.CLIP_NAME,
        "revision": revision,
        "embeddingSize": 512,
        "photoModelVersion": pretrained.picVersion(
            pretrained.ML / "clip", pretrained.CLIP_NAME, revision, entry
        ),
    }


def libraryVersions():
    versions = {}

    for line in (ROOT / "requirements.txt").read_text().splitlines():
        name, separator, needed = line.strip().partition("==")

        if not separator:
            continue

        try:
            installed = importlib.metadata.version(name)
        except Exception as error:
            installed = None

        versions[name] = {"required": needed, "installed": installed}

    return versions


# List the saved model files
def main():
    source = "ResaleflatpricesbasedonregistrationdatefromJan2017onwards.csv"
    price = json.loads((HERE / "price_model_v5_metrics.json").read_text())
    photo = json.loads((HERE / "photo_metrics.json").read_text())
    package = json.loads((HERE / "package_model_metrics.json").read_text())

    with (HERE / source).open(newline="") as stream:
        months = [row["month"] for row in csv.DictReader(stream)]

    start, end = min(months), max(months)

    photoData = {
        "sources": [
            "MIT Indoor-67 and wrong-photo classes",
            "ELSA D3 generators and paired real LAION interiors",
        ],
        "dateRange": None,
        "dateRangeNote": "Image capture/generation dates are not recorded in supplied training evidence.",
        "splitDates": None,
        "split": "Stratified random 70/15/15, seed 2108; not a temporal split.",
    }

    meta = {
        "price_model_v5.joblib": {
            "modelType": "Three lightgbm.LGBMRegressor models: median, q10, q90 on log price",
            "version": price["selected_variant"],
            "trainingScript": "price_training.ipynb",
            "data": {
                "sources": [
                    source,
                    "geo/hdb_blocks_geo.csv",
                    "geo/mrt_stations.csv",
                    "legacy_mappings.json",
                ],
                "dateRange": {"start": start, "end": end},
                "splitDates": {
                    "trainEnd": "2025-04",
                    "validationStart": "2025-05",
                    "validationEnd": "2025-10",
                    "testStart": "2025-11",
                    "testEnd": "2026-10",
                },
                "finalRefit": "All supported source sales, including former test months.",
                "limitation": "Test period previously informed geographic feature selection; metrics precede final refit.",
            },
            "metricsFiles": [
                "price_model_v5_metrics.json",
                "price_band_eval.json",
                "price_backtest_eval.json",
            ],
        },
        "photo_head.joblib": {
            "modelType": "sklearn.neural_network.MLPClassifier, hidden layer 64, frozen CLIP features",
            "version": "photo-v2",
            "trainingScript": "photo_training.ipynb",
            "data": {**photoData, "splitRows": photo["split"]},
            "metricsFiles": ["photo_metrics.json", "lens_eval.json"],
        },
        "aigen_head.joblib": {
            "modelType": "sklearn.linear_model.LogisticRegression, frozen CLIP features",
            "version": "photo-v2",
            "trainingScript": "photo_training.ipynb",
            "data": {
                **photoData,
                "splitEvidence": "photo_metrics.json: ai.split_paths and ai.calibration_method",
            },
            "metricsFiles": ["photo_metrics.json"],
        },
        "package_model.joblib": {
            "modelType": "sklearn.pipeline.Pipeline: DictVectorizer and RandomForestClassifier",
            "version": package["version"],
            "trainingScript": "package_training.ipynb",
            "data": {
                "sources": [
                    source,
                    "Synthetic valuation, asking-price and listing-age fields; assumed noisy policy labels",
                ],
                "dateRange": {"start": "2023-11", "end": package["sourceMaxMonth"]},
                "activityDateRange": {"start": "2025-11", "end": package["sourceMaxMonth"]},
                "splitDates": None,
                "split": package["split"],
                "seed": package["seed"],
                "trainRows": package["trainRows"],
                "testRows": package["testRows"],
            },
            "metricsFiles": ["package_model_metrics.json", "package_model_eval.json"],
        },
    }

    artifacts = []



    for path in sorted((pretrained.ML).rglob("*.joblib")):
        artifacts.append(
            {
                "path": str(path.relative_to(PROJECT)),
                "sha256": file_hash(path),
                "sizeBytes": path.stat().st_size,
                **meta[path.name],
            }
        )

    from pretrained import MODELS, PIPELINE_VERSION

    entries = pretrainedEntries()

    sheet = {
        "schemaVersion": 2,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "metadataProvenance": "Training source and recorded metrics; artifacts are hashed, never deserialized. Training-time dependency versions were not recorded.",
        "pythonVersion": platform.python_version(),
        "libraryVersions": libraryVersions(),
        "pretrained": entries,
        "clip": photoIdentity(entries[0]),
        "vision": {
            "pipelineVersion": PIPELINE_VERSION,
            "models": [
                {"modelId": name, "revision": pretrained.REVISIONS[name], "localPath": folder}
                for name, folder in MODELS
            ],
        },
        "revisionLimitation": "Pinned commits come from local Hugging Face refs/main or per-file download metadata. Cache refs associate a revision with a repository but do not prove the origin of copied or converted files; installed SHA-256 hashes are the runtime baseline. Missing folders are explicitly not installed. No models are loaded and no .env is read.",
        "artifacts": artifacts,
    }

    (HERE / "model_manifest.json").write_text(json.dumps(sheet, indent=2) + "\n")
    print(json.dumps({"artifacts": len(artifacts), "output": "model_manifest.json"}))


if __name__ == "__main__":
    main()
