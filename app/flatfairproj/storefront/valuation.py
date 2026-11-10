from django.utils import timezone
import json
import math
import logging
import joblib
import numpy as np
import pandas as pd
from pretrained import ML, TRAINING, ArtifactUnavailable, warnGone

log = logging.getLogger(__name__)
priceBacktestPath = TRAINING / "price_backtest_eval.json"
ARTIFACT = ML / "price_model_v5.joblib"
VERSION = "v5.3"
_CACHE = None
_CACHE_SIGNATURE = None

TOWNS = [
    "ANG MO KIO",
    "BEDOK",
    "BISHAN",
    "BUKIT BATOK",
    "BUKIT MERAH",
    "BUKIT PANJANG",
    "BUKIT TIMAH",
    "CENTRAL AREA",
    "CHOA CHU KANG",
    "CLEMENTI",
    "GEYLANG",
    "HOUGANG",
    "JURONG EAST",
    "JURONG WEST",
    "KALLANG/WHAMPOA",
    "MARINE PARADE",
    "PASIR RIS",
    "PUNGGOL",
    "QUEENSTOWN",
    "SEMBAWANG",
    "SENGKANG",
    "SERANGOON",
    "TAMPINES",
    "TOA PAYOH",
    "WOODLANDS",
    "YISHUN",
]
FLAT_MODELS = [
    "2-room",
    "3Gen",
    "Adjoined flat",
    "Apartment",
    "DBSS",
    "Improved",
    "Improved-Maisonette",
    "Maisonette",
    "Model A",
    "Model A-Maisonette",
    "Model A2",
    "New Generation",
    "Premium Apartment",
    "Premium Apartment Loft",
    "Premium Maisonette",
    "Simplified",
    "Standard",
    "Terrace",
    "Type S1",
    "Type S2",
]


def _bundleOk(candidate):
    if candidate["version"] != VERSION:
        raise ValueError("Invalid model schema")

    for name in ("median", "q10", "q90"):
        if name not in candidate["models"]:
            raise ValueError("Invalid model schema")



    if candidate.get("calibration_scale") != "dollars":
        raise ValueError("Serving requires dollar calibration")

    needed = (
        "features",
        "categories",
        "max_training_month",
        "calibration_offsets",
        "calibration_scale",
    )

    for key in needed:
        if key not in candidate:
            raise ValueError("Incomplete model schema")


def _load():
    global _CACHE, _CACHE_SIGNATURE



    try:
        mark = (ARTIFACT.stat().st_mtime_ns, ARTIFACT.stat().st_size)

        if _CACHE is None or mark != _CACHE_SIGNATURE:
            option = joblib.load(ARTIFACT)

            try:
                _bundleOk(option)
            except (KeyError, TypeError, ValueError) as error:
                raise ArtifactUnavailable("Invalid price model schema") from error

            _CACHE, _CACHE_SIGNATURE = option, mark

        return _CACHE
    except (OSError, ValueError, EOFError) as error:
        warnGone(log, f"Price model unavailable: {error}")
        _CACHE = None
        _CACHE_SIGNATURE = None

        raise ValueError(
            "Sorry, the price check isn’t available right now. Please try again later."
        ) from error
    except Exception as error:
        log.exception("Price model loading failed")
        _CACHE = None



        _CACHE_SIGNATURE = None

        raise ValueError(
            "Sorry, the price check isn’t available right now. Please try again later."
        ) from error


def modelInfo():
    try:
        bundle = _load()


        return {
            "available": True,
            "version": VERSION,
            "month": bundle["max_training_month"],
            "mape": bundle["metrics"][bundle.get("selected_variant", "v5")]["test"]["MAPE"],
            "coverage": bundle["metrics"]["band"]["coverage"] * 100,
            "target": 80,
        }
    except ValueError:
        return {"available": False, "version": VERSION}
    except Exception:
        log.exception("Price model information failed")

        return {"available": False, "version": VERSION}


def _distance(lat, lon, lat2, lon2):
    lat_one, lon_one, lat_two, lon_two = map(np.radians, (lat, lon, lat2, lon2))

    arc = (
        np.sin((lat_two - lat_one) / 2) ** 2
        + np.cos(lat_one) * np.cos(lat_two) * np.sin((lon_two - lon_one) / 2) ** 2
    )

    return 6371 * 2 * np.arcsin(np.sqrt(np.clip(arc, 0, 1)))


def _mapPlaces(geo):
    postals = {}
    blocks = {}

    for record in geo["addresses"]:
        code = str(record.get("postal", "")).strip()

        if len(code) == 6 and code.isdigit():
            postals.setdefault(code, record)



        key = (str(record.get("block", "")).strip().upper(), record.get("town"))
        blocks.setdefault(key, []).append(record)

    geo["_postal_lookup"] = postals
    geo["_block_lookup"] = {}

    for key, records in blocks.items():
        if len(records) == 1:
            geo["_block_lookup"][key] = records[0]


def geoPoint(geo, town, postal=None, block=None):
    if "_postal_lookup" not in geo:
        _mapPlaces(geo)

    point = geo["_postal_lookup"].get(str(postal or "").strip())

    if point is None:
        point = geo["_block_lookup"].get((str(block or "").strip().upper(), town))

    if point is None:
        point = geo["centroids"][town]

    return point


def makeRow(town, flatType, flatModel, floorAreaSqm, storey, leaseYear, postal=None, block=None):
    bundle = _load()

    try:
        area, floor, year = float(floorAreaSqm), float(storey), float(leaseYear)
        today = timezone.localdate()

        if not all(math.isfinite(number) for number in (area, floor, year)):
            raise ValueError("Invalid area, storey or lease year")


        if not (25 <= area <= 300 and 1 <= floor <= 50 and 1960 <= year <= today.year):
            raise ValueError("Invalid area, storey or lease year")

        if not floor.is_integer() or not year.is_integer():
            raise ValueError("Invalid area, storey or lease year")

        cats = bundle["categories"]
        values = {"town": town, "flat_type": flatType, "flat_model": flatModel}

        for key, value in values.items():
            if value not in cats[key]:
                raise ValueError("Unknown town, flat type or flat model")

        trend = min(
            pd.Timestamp(today.replace(day=1)), pd.Timestamp(bundle["max_training_month"] + "-01")
        )


        months = (today.year - int(year)) * 12 + today.month - 1

        values.update(
            floor_area_sqm=area,
            storey_mid=(int(floor) - 1) // 3 * 3 + 2,
            remaining_lease_months=1188 - months,
            flat_age=months / 12,
            month_index=(trend.year - 2017) * 12 + trend.month - 1,
            sale_year=trend.year,
            sale_month=trend.month,
        )

        geo = bundle.get("geo")

        if geo:
            point = geoPoint(geo, town, postal, block)
            lat, lon = point["lat"], point["lon"]
            stops = np.asarray(geo["stations"])
            stop_gaps = _distance(lat, lon, stops[:, 0], stops[:, 1])

            values.update(
                lat=lat,
                lon=lon,
                dist_cbd_km=_distance(lat, lon, 1.2840, 103.8514),
                dist_mrt_km=stop_gaps.min(),
                n_mrt_1km=int((stop_gaps <= 1).sum()),
            )

        row = pd.DataFrame([values])


        for name, groups in cats.items():
            row[name] = pd.Categorical(row[name], categories=groups)

        return row[bundle["features"]]
    except (TypeError, KeyError, OverflowError) as error:
        raise ValueError("Invalid valuation input") from error


def rawBand(predictions, calibrationOffsets):
    fair_cost = round(np.exp(predictions["median"]))
    low = np.exp(min(predictions["q10"], predictions["q90"])) - calibrationOffsets["lower"]
    high = np.exp(max(predictions["q10"], predictions["q90"])) + calibrationOffsets["upper"]

    if not all(math.isfinite(price) for price in (low, fair_cost, high)):
        raise ValueError("Invalid model prediction")

    return low, fair_cost, high


def guessBand(predictions, calibrationOffsets):
    low, fair_cost, high = rawBand(predictions, calibrationOffsets)



    spread = max(10000, round(fair_cost * 0.05))


    return (
        max(1, min(round(low), fair_cost - spread)),
        fair_cost,
        max(round(high), fair_cost + spread),
    )


def band(town, flatType, flatModel, floorAreaSqm, storey, leaseYear, postal=None, block=None):
    try:
        row = makeRow(town, flatType, flatModel, floorAreaSqm, storey, leaseYear, postal, block)
        bundle = _load()
    except ValueError:
        raise
    except Exception as error:
        log.exception("Price serving failed")

        raise ValueError("Sorry, we couldn’t work out a price for this flat.") from error

    try:
        guesses = {}

        for name, model in bundle["models"].items():
            guesses[name] = float(model.predict(row)[0])


        return guessBand(guesses, bundle["calibration_offsets"])
    except Exception as error:
        log.exception("Price prediction failed")

        raise ValueError("Sorry, we couldn’t work out a price for this flat.") from error


def bandSpots(low, est, high, ask):
    point = ask or est
    span = high - low
    left = min(low - 0.45 * span, point - 0.15 * span)
    right = max(high + 0.45 * span, point + 0.15 * span)

    def pct(value):
        return round((value - left) / (right - left) * 100, 1)

    return {"lo": pct(low), "hi": pct(high), "est": pct(est), "pos": pct(point), "point": point}


def flagReason(ask, low, est, high):
    price_verdict = verdict(ask, low, est, high)

    return price_verdict["key"] if price_verdict["out"] else None


def verdict(ask, low, est, high):
    if not ask:
        return {"key": "none", "tone": "brand", "text": "Add your price to check it", "out": False}

    if ask > high:
        return {"key": "above", "tone": "amber", "text": "Priced high", "out": True}

    if ask < low * 0.7:
        return {"key": "typo", "tone": "bad", "text": "Priced low", "out": True}

    if ask < low:
        return {"key": "fair", "tone": "good", "text": "Priced low", "out": False}

    return {"key": "fair", "tone": "good", "text": "Fair price", "out": False}


def summary_ok(summary):
    if not isinstance(summary, dict):
        return False

    count, share = summary.get("count"), summary.get("servedCoverage")

    return (
        type(count) is int
        and count >= 0
        and type(share) in (int, float)
        and math.isfinite(share)
        and 0 <= share <= 1
    )


def readTrial(path):
    try:
        pooled = json.loads(path.read_text(encoding="utf-8"))["pooled"]

        for key in ("byTown", "byFlatType"):
            groups = pooled[key]

            if not isinstance(groups, dict) or any(
                not isinstance(name, str) or not summary_ok(summary)
                for name, summary in groups.items()
            ):
                return None



        weakest = pooled.get("weakestSubgroups", [])

        if not isinstance(weakest, list) or any(
            not summary_ok(summary)
            or summary.get("dimension") not in ("town", "flat_type")
            or not isinstance(summary.get("group"), str)
            for summary in weakest
        ):
            return None

        return {
            "byTown": pooled["byTown"],
            "byFlatType": pooled["byFlatType"],
            "weakestSubgroups": [
                {**summary, "coverage": summary["servedCoverage"] * 100} for summary in weakest
            ],
        }
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None


def pastCheck():
    return readTrial(priceBacktestPath)
