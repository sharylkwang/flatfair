import json
import logging
import time
import unicodedata
from functools import lru_cache

from PIL import Image
from pretrained import ASSETS

from . import pipeline
from .pipeline import ML

VERSION = 1
DATA = ASSETS / "data" / "brands.json"
OCR_DIR = ML / "vision" / "florence2"
log = logging.getLogger(__name__)


def ocr_available():
    return (OCR_DIR / "model.safetensors").exists()


def catalogue():
    data = json.loads(DATA.read_text())
    group_of = {}

    for family_name, info in data["families"].items():
        for label in info["labels"]:
            group_of[label] = family_name

    read = {name: family["read"] for name, family in data["families"].items()}
    brands = [
        (brand["name"], [alias.upper() for alias in brand["aliases"]], frozenset(brand["families"]))
        for brand in data["brands"]
    ]



    return group_of, read, brands, frozenset(word.upper() for word in data["words"])


def family(label):
    return catalogue()[0].get(label)


def tokens(text):
    text = unicodedata.normalize("NFKD", str(text or ""))
    words, word = [], ""

    for char in text:
        if unicodedata.combining(char):
            continue

        if char.isascii() and (char.isalnum() or char in "&|!'"):
            word += char
        elif word:
            words.append(word)



            word = ""

    if word:
        words.append(word)

    return words


def _same(read, letter):
    confusable = {
        "l": "I",
        "I": "L",
        "1": "IL",
        "|": "IL",
        "!": "I",
        "0": "O",
        "5": "S",
        "8": "B",
        "6": "G",
        "2": "Z",
    }


    return read.upper() == letter or letter in confusable.get(read, "")


def distance(read, alias):
    old = list(range(len(alias) + 1))

    for i, char in enumerate(read, 1):
        current = [i]

        for j, letter in enumerate(alias, 1):
            current.append(
                min(old[j] + 1, current[j - 1] + 1, old[j - 1] + (not _same(char, letter)))
            )

        old = current

    return old[-1]


def match_line(text, label):
    kind = family(label)
    words = tokens(text)

    if not kind or not words:
        return None


    _, _, brands, known = catalogue()
    letters = sum(len(word) for word in words)
    best = []

    for name, aliases, families in brands:
        if kind not in families:
            continue

        for alias in aliases:
            compact = "".join(alias.split())
            size = len(alias.split())
            short = len(compact) <= 3

            for window_size in range(max(1, size - 1), size + 2):
                for start in range(len(words) - window_size + 1):
                    window = words[start : start + window_size]
                    joined = "".join(window)
                    rest = words[:start] + words[start + window_size :]

                    if short:
                        edits = 0 if joined.upper() == compact else None
                        alone = all(word.upper() in known for word in rest)

                        if edits is None or not alone:
                            continue
                    else:
                        edits = distance(joined, compact)



                        allowed = 1 if len(compact) >= 8 else 0

                        if edits > allowed or abs(len(joined) - len(compact)) > 1:
                            continue

                        if len(joined) / letters < 0.6 and not all(
                            word.upper() in known for word in rest
                        ):
                            continue

                    best.append((len(compact), -edits, name, alias, edits, short))

    if not best:
        return None

    best.sort(reverse=True)
    top = best[0]



    if any(other[2] != top[2] and other[:2] == top[:2] for other in best[1:]):
        return None

    return {"brand": top[2], "alias": top[3], "edits": top[4], "short": top[5]}


def trusted(match, line):
    confidence = {"exact": (0.85, 0.50), "edit": (0.93, 0.70), "short": (0.90, 0.70)}

    if match["short"]:
        rule = "short"
    elif match["edits"]:
        rule = "edit"
    else:
        rule = "exact"

    mean, lowest = confidence[rule]

    return line.get("confidence", 0) >= mean and line.get("lowest", 0) >= lowest


def choose(lines, label):
    agreed, denied = [], []

    for line in lines:
        if not tokens(line.get("text")):
            continue

        match = match_line(line["text"], label)

        if match and trusted(match, line):
            agreed.append(dict(line, **match))
        else:
            denied.append(dict(line, reason="low confidence" if match else "no brand"))

    found = {line["brand"] for line in agreed}
    names = set()

    for name in found:
        extended = any(other != name and other.startswith(name) for other in found)

        if not extended:
            names.add(name)

    if len(names) != 1:
        if names:
            denied.extend(dict(line, reason="conflicting brands") for line in agreed)

        return None, None, denied


    name = names.pop()


    candidates = [line for line in agreed if line["brand"] == name]


    line = max(candidates, key=lambda line: line["confidence"])

    return name, line, denied


def with_brand(attributes, brand):
    rest = {}

    for key, value in (attributes or {}).items():
        if key != "brand":
            rest[key] = value

    return {"brand": brand, **rest} if brand else rest


@lru_cache(maxsize=1)
def ocr_model():
    import torch
    from transformers import AutoProcessor, Florence2ForConditionalGeneration

    torch.set_num_threads(4)
    prep = AutoProcessor.from_pretrained(OCR_DIR, local_files_only=True)
    model = Florence2ForConditionalGeneration.from_pretrained(
        OCR_DIR, local_files_only=True, dtype=torch.float32
    ).eval()

    if pipeline.DEVICE == "cuda":
        model.to("cuda")

    return prep, model


def read_lines(image):
    import torch

    prep, model = ocr_model()
    side = max(image.size)


    square = Image.new("RGB", (side, side), (128, 128, 128))
    square.paste(image.convert("RGB"), (0, 0))

    with torch.inference_mode():
        inputs = pipeline.onDevice(
            prep(text="<OCR_WITH_REGION>", images=square, return_tensors="pt")
        )
        out = model.generate(
            **inputs,
            max_new_tokens=256,
            num_beams=1,
            do_sample=False,
            output_scores=True,
            return_dict_in_generate=True,
        )
        fake = out.sequences[0, -len(out.scores) :].tolist()
        probabilities = [
            float(torch.softmax(score[0].float(), -1)[token])
            for score, token in zip(out.scores, fake)
        ]

    return parse(prep.tokenizer, fake, probabilities, side)


def parse(tokenizer, ids, probabilities, side):
    lines, text, scores, corners = [], [], [], []
    special = set(tokenizer.all_special_ids)

    for token, chance in zip(ids, probabilities):
        piece = tokenizer.convert_ids_to_tokens(token)

        if isinstance(piece, str) and piece.startswith("<loc_"):
            corners.append((int(piece[5:-1]) + 0.5) * side / 1000)

            if len(corners) == 8:
                words = tokenizer.decode(text, skip_special_tokens=True).strip()

                if words and scores:
                    x_spots, y_spots = corners[0::2], corners[1::2]

                    lines.append(
                        {
                            "text": words,
                            "box": [min(x_spots), min(y_spots), max(x_spots), max(y_spots)],
                            "confidence": sum(scores) / len(scores),
                            "lowest": min(scores),
                        }
                    )

                text, scores, corners = [], [], []
        elif token not in special:
            corners = []



            text.append(token)
            scores.append(chance)



    return lines


def confirm_with_clip(crop, label, brand, rivals):
    from .pipeline import load, embed_text
    import torch

    prep, model = load("clip")



    texts = (
        [f"a {label} with the {brand} logo"]
        + [f"a {label} with the {other} logo" for other in rivals]
        + [f"a plain {label} with no logo"]
    )

    with torch.inference_mode():
        image = model.get_image_features(
            **pipeline.onDevice(prep(images=crop, return_tensors="pt"))
        )

        if not isinstance(image, torch.Tensor):
            image = image.pooler_output

        image = torch.nn.functional.normalize(image.float(), dim=-1)
        odds = (image @ embed_text(tuple(texts)).T * model.logit_scale.exp())[0].softmax(-1)

    return int(odds.argmax()) == 0 and float(odds[0]) >= 0.6, float(odds[0])


def region(item, scale):
    points = item.get("polygon") or []

    if points:
        horizontal_positions, vertical_positions = [point[0] for point in points], [
            point[1] for point in points
        ]
        box = [
            min(horizontal_positions),
            min(vertical_positions),
            max(horizontal_positions),
            max(vertical_positions),
        ]
    else:
        box = list(item.get("box") or [0, 0, 0, 0])



    return [box[0] * scale[0], box[1] * scale[1], box[2] * scale[0], box[3] * scale[1]]


def padded(box, size, fraction=0.08):
    margin = max(16, fraction * max(box[2] - box[0], box[3] - box[1]))

    return (
        int(max(0, box[0] - margin)),
        int(max(0, box[1] - margin)),
        int(min(size[0], box[2] + margin)),
        int(min(size[1], box[3] + margin)),
    )


def zoom(line, size):
    box_left, box_top, box_right, box_bottom = line["box"]
    side = max(1.4 * (box_right - box_left), 6 * max(box_bottom - box_top, 4), 160)
    x_mid, y_mid = (box_left + box_right) / 2, (box_top + box_bottom) / 2

    return (
        int(max(0, x_mid - side / 2)),
        int(max(0, y_mid - side / 2)),
        int(min(size[0], x_mid + side / 2)),
        int(min(size[1], y_mid + side / 2)),
    )


def reread(photo, line, label, brand):
    again, line_two, _ = choose(read_lines(photo.crop(zoom(line, photo.size))), label)

    return again == brand and not line_two["edits"], line_two


def _centre_in(line, box, slack=0.02):
    x_mid, y_mid = (line["box"][0] + line["box"][2]) / 2, (line["box"][1] + line["box"][3]) / 2
    x_slack, y_slack = slack * (box[2] - box[0]), slack * (box[3] - box[1])

    return (
        box[0] - x_slack <= x_mid <= box[2] + x_slack
        and box[1] - y_slack <= y_mid <= box[3] + y_slack
    )


def detect(path, items, size):
    started = time.monotonic()
    _, read, brands, _ = catalogue()
    kinds = [family(item["label"]) for item in items]
    report = {"version": VERSION, "reads": 0, "found": [], "rejected": []}
    found = [None] * len(items)
    wanted = [i for i, kind in enumerate(kinds) if kind]

    if wanted:
        photo = Image.open(path).convert("RGB")
        scale = (photo.width / max(size[0], 1), photo.height / max(size[1], 1))
        boxes = [region(item, scale) for item in items]

        def box_area(i):
            return (boxes[i][2] - boxes[i][0]) * (boxes[i][3] - boxes[i][1])

        lines = {item_index: [] for item_index in wanted}
        close_reads = [item_index for item_index in wanted if read[kinds[item_index]] == "item"]

        for item_no in close_reads[:6]:
            crop = padded(boxes[item_no], photo.size)
            report["reads"] += 1

            for line in read_lines(photo.crop(crop)):
                line["box"] = [
                    line["box"][0] + crop[0],
                    line["box"][1] + crop[1],
                    line["box"][2] + crop[0],
                    line["box"][3] + crop[1],
                ]


                if _centre_in(line, boxes[item_no]):
                    lines[item_no].append(line)

        if any(read[kinds[item_index]] == "photo" for item_index in wanted):
            report["reads"] += 1

            for line in read_lines(photo):
                owners = [i for i in range(len(items)) if _centre_in(line, boxes[i], slack=0)]

                if owners:
                    owner = min(owners, key=box_area)
                else:
                    owner = None

                if owner in lines:
                    lines[owner].append(dict(line, whole_photo=True))

        for item_no in wanted:
            brand, line, denied = choose(lines[item_no], items[item_no]["label"])

            if brand:
                report["reads"] += 1
                agreed, again = reread(photo, line, items[item_no]["label"], brand)

                if not agreed:
                    denied.append(
                        dict(line, reason=f"second read: {again['text'] if again else 'no brand'}")
                    )
                    brand = None

            if brand and line["short"]:
                rivals = []

                for name, _, families in brands:
                    if kinds[item_no] in families and name != brand:
                        rivals.append(name)

                rivals = rivals[:6]
                agreed, chance = confirm_with_clip(
                    photo.crop(padded(boxes[item_no], photo.size)),
                    items[item_no]["label"],
                    brand,
                    rivals,
                )


                line = dict(line, clip=round(chance, 3))

                if not agreed:
                    denied.append(dict(line, reason="CLIP disagrees"))
                    brand = None

            found[item_no] = brand

            if brand:
                report["found"].append(
                    _evidence(item_no, items[item_no]["label"], line, brand=brand)
                )

            for line in denied:
                report["rejected"].append(_evidence(item_no, items[item_no]["label"], line))

    report["rejected"] = report["rejected"][:20]
    report["seconds"] = round(time.monotonic() - started, 2)



    return found, report


def _evidence(index, label, line, **extra):
    entry = {
        "item": index,
        "label": label,
        "text": line["text"][:60],
        "confidence": round(line.get("confidence", 0), 3),
    }

    for key in ("reason", "clip", "whole_photo"):
        if key in line:
            entry[key] = line[key]

    return dict(entry, **extra)


def add_brands(path, result):
    if not ocr_available():
        return result

    try:
        found, report = detect(path, result.get("items", []), result.get("size", [1000, 1000]))
    except Exception as exc:
        log.warning("Brand check skipped for %s: %s", path, exc)
        result.setdefault("timings", {})["brands"] = {"error": str(exc)[:300]}

        return result



    for item, brand in zip(result.get("items", []), found):
        item["attributes"] = with_brand(item.get("attributes"), brand)

    result.setdefault("timings", {})["brands"] = report

    return result
