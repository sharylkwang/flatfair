from functools import lru_cache
from pathlib import Path
import io
import time
import numpy as np
from PIL import Image, ImageEnhance
from pretrained import ML

ALIASES = {
    "bedside lamp": "table lamp",
    "framed picture": "painting",
    "roller blind": "window blind",
    "throw pillow": "cushion",
    "bookshelf": "shelf",
    "window shade": "window blind",
    "roller shade": "window blind",
    "bed pillow": "cushion",
    "pendant lamp": "pendant light",
    "chandelier": "pendant light",
    "gas stove": "cooktop",
    "stove": "cooktop",
    "hob": "cooktop",
    "cooker hood": "range hood",
    "extractor hood": "range hood",
    "bar stool": "stool",
    "wash basin": "sink",
    "kitchen sink": "sink",
    "bathroom vanity": "vanity cabinet",
    "rain shower": "shower head",
    "aircon": "air conditioner",
    "washer": "washing machine",
    "front load washer": "washing machine",
    "television": "TV",
}
SMALL_LABELS = {
    "table lamp",
    "floor lamp",
    "pendant light",
    "ceiling fan",
    "painting",
    "potted plant",
    "cushion",
    "window blind",
    "bedside table",
    "side table",
    "lamp",
    "ceiling light",
    "shower head",
    "microwave",
    "stool",
}

LABELS = (
    "sofa, armchair, coffee table, dining table, chair, bed, wardrobe, cabinet, bedside table, "
    "side table, shelf, TV, floor lamp, table lamp, pendant light, ceiling fan, rug, curtains, "
    "potted plant, mirror, painting, kitchen cabinet, refrigerator, sink, bathtub, toilet, shower, "
    "window blind, cushion, oven, microwave, cooktop, range hood, stool, desk, ceiling light, "
    "air conditioner, vanity cabinet, shower head, water heater, washing machine"
).split(", ")

NEGATIVE_LABELS = ["window", "door"]
_SHARED = [
    "ceiling fan",
    "ceiling light",
    "pendant lamp",
    "curtains",
    "window blind",
    "air conditioner",
    "potted plant",
    "painting",
    "window",
    "door",
]

VOCABULARY = {
    "kitchen": [
        [
            "refrigerator",
            "oven",
            "microwave",
            "gas stove",
            "range hood",
            "kitchen sink",
            "kitchen cabinet",
            "washing machine",
            "bar stool",
            "dining table",
            "chair",
        ],
        [
            "pendant lamp",
            "ceiling light",
            "ceiling fan",
            "potted plant",
            "window blind",
            "air conditioner",
            "painting",
            "window",
            "door",
        ],
    ],
    "bathroom": [
        [
            "toilet",
            "bathtub",
            "wash basin",
            "mirror",
            "bathroom vanity",
            "rain shower",
            "water heater",
            "washing machine",
            "potted plant",
            "window",
            "door",
        ],
        ["ceiling light", "pendant lamp", "window blind", "painting", "cabinet", "window", "door"],
    ],
    "bedroom": [
        [
            "bed",
            "wardrobe",
            "bedside table",
            "table lamp",
            "desk",
            "chair",
            "mirror",
            "shelf",
            "rug",
            "cushion",
            "window",
            "door",
        ],
        _SHARED,
    ],
    "living": [
        [
            "sofa",
            "armchair",
            "coffee table",
            "side table",
            "dining table",
            "chair",
            "TV",
            "shelf",
            "cabinet",
            "floor lamp",
            "table lamp",
            "rug",
            "cushion",
            "window",
            "door",
        ],
        [*_SHARED, "washing machine"],
    ],
}


def room_kind(room):
    room = str(room or "")

    if "kitchen" in room:
        return "kitchen"

    if "toilet" in room or "bath" in room:
        return "bathroom"

    if "bedroom" in room:
        return "bedroom"

    return "living"


PALETTES = {
    "colour": ["white", "cream", "beige", "grey", "black", "brown", "blue", "green", "red"],
    "material": ["fabric", "leather", "wood", "metal", "glass", "rattan", "marble", "ceramic"],
    "style": [
        "Scandinavian",
        "modern minimalist",
        "industrial",
        "mid-century",
        "Japandi",
        "classic",
    ],
}

DEVICE = "cpu"


def onDevice(inputs):
    if DEVICE == "cuda":
        return inputs.to("cuda")

    return inputs


def weights_ok():
    for dir_name in ("vision/grounding_dino", "vision/slimsam", "depth", "clip"):
        if not (ML / dir_name / "model.safetensors").exists():
            return False

    return True


@lru_cache(maxsize=4)
def load(kind):
    import torch

    torch.set_num_threads(4)

    from transformers import (
        AutoProcessor,
        AutoModelForZeroShotObjectDetection,
        SamProcessor,
        SamModel,
        CLIPProcessor,
        CLIPModel,
        AutoImageProcessor,
        AutoModelForDepthEstimation,
    )

    proc, model, path = {
        "dino": (AutoProcessor, AutoModelForZeroShotObjectDetection, "vision/grounding_dino"),
        "sam": (SamProcessor, SamModel, "vision/slimsam"),
        "clip": (CLIPProcessor, CLIPModel, "clip"),
        "depth": (AutoImageProcessor, AutoModelForDepthEstimation, "depth"),
    }[kind]

    prep = proc.from_pretrained(ML / path, local_files_only=True)
    network = model.from_pretrained(ML / path, local_files_only=True).eval()



    if DEVICE == "cuda":
        network.to("cuda")

    return prep, network


MATERIALS = {
    "lamp": ["wood", "metal", "glass", "ceramic", "fabric"],
    "table": ["wood", "metal", "glass", "marble"],
    "sofa": ["fabric", "leather"],
    "armchair": ["fabric", "leather", "wood", "rattan"],
    "chair": ["fabric", "leather", "wood", "metal", "rattan"],
    "bed": ["fabric", "wood", "metal"],
    "coffee table": ["wood", "metal", "glass", "marble"],
    "dining table": ["wood", "metal", "glass", "marble"],
    "side table": ["wood", "metal", "glass", "marble"],
    "bedside table": ["wood", "metal", "rattan"],
    "wardrobe": ["wood", "metal"],
    "cabinet": ["wood", "metal"],
    "kitchen cabinet": ["wood", "metal"],
    "window blind": ["fabric", "wood", "metal"],
    "cushion": ["fabric"],
    "shelf": ["wood", "metal"],
    "floor lamp": ["wood", "metal", "glass"],
    "table lamp": ["wood", "metal", "glass", "ceramic"],
    "pendant light": ["metal", "glass", "rattan"],
    "ceiling fan": ["wood", "metal"],
    "curtains": ["fabric"],
    "rug": ["fabric"],
    "mirror": ["glass"],
    "sink": ["ceramic", "metal"],
    "bathtub": ["ceramic"],
    "toilet": ["ceramic"],
    "oven": ["metal", "glass"],
    "cooktop": ["glass", "metal"],
    "range hood": ["metal", "glass"],
    "stool": ["wood", "metal", "fabric", "leather"],
    "desk": ["wood", "metal", "glass"],
    "ceiling light": ["glass", "metal", "fabric"],
    "vanity cabinet": ["wood", "marble"],
    "shower head": ["metal"],
}
STYLE_LABELS = {
    "table",
    "sofa",
    "armchair",
    "chair",
    "bed",
    "coffee table",
    "dining table",
    "side table",
    "bedside table",
    "wardrobe",
    "cabinet",
    "shelf",
    "stool",
    "desk",
    "vanity cabinet",
}


def canonical(label):
    terms = sorted(list(ALIASES) + LABELS + NEGATIVE_LABELS, key=len, reverse=True)
    found = ""

    for term in terms:
        if term.lower() in str(label).lower():
            found = term

            break

    return ALIASES.get(found, found)


KITCHEN_ONLY = {"kitchen cabinet", "refrigerator", "oven", "microwave", "cooktop", "range hood"}
BATHROOM_ONLY = {"toilet", "bathtub", "shower", "shower head", "vanity cabinet", "water heater"}


def room_cutoff(label, room):
    thresholds = {
        "bed": 0.45,
        "dining table": 0.40,
        "painting": 0.25,
        "mirror": 0.43,
        "kitchen cabinet": 0.43,
        "cabinet": 0.40,
        "floor lamp": 0.24,
        "table lamp": 0.22,
        "pendant light": 0.25,
        "ceiling fan": 0.27,
        "curtains": 0.35,
        "potted plant": 0.28,
        "window blind": 0.28,
        "cushion": 0.30,
        "bedside table": 0.28,
        "side table": 0.30,
        "coffee table": 0.32,
        "table": 0.30,
        "rug": 0.50,
        "lamp": 0.22,
        "oven": 0.33,
        "microwave": 0.35,
        "cooktop": 0.30,
        "range hood": 0.30,
        "stool": 0.33,
        "desk": 0.38,
        "ceiling light": 0.33,
        "air conditioner": 0.35,
        "vanity cabinet": 0.35,
        "shower head": 0.30,
        "water heater": 0.38,
        "refrigerator": 0.40,
        "TV": 0.40,
        "washing machine": 0.30,
    }


    cutoff = thresholds.get(label, 0.38)
    banned = set()
    kind = room_kind(room)


    if "bedroom" in room:
        banned = KITCHEN_ONLY | BATHROOM_ONLY | {"sink", "dining table", "stool", "washing machine"}
    elif kind == "bathroom":
        banned = KITCHEN_ONLY | {
            "bed",
            "sofa",
            "armchair",
            "dining table",
            "coffee table",
            "wardrobe",
            "floor lamp",
            "table lamp",
            "lamp",
            "ceiling fan",
            "cushion",
            "TV",
            "desk",
            "rug",
            "curtains",
            "window blind",
            "pendant light",
            "air conditioner",
        }
    elif kind == "kitchen":
        banned = BATHROOM_ONLY | {
            "bed",
            "wardrobe",
            "sofa",
            "armchair",
            "coffee table",
            "bedside table",
            "desk",
            "TV",
            "rug",
            "air conditioner",
        }


        if label in ("table lamp", "floor lamp", "lamp", "cushion"):
            cutoff = max(cutoff, 0.55)

        if label == "window blind":
            cutoff = max(cutoff, 0.45)
    elif room in ("living_room", "living"):
        banned = BATHROOM_ONLY | {"bed", "cooktop", "range hood"}

    if kind == "bathroom" and label == "window blind":
        cutoff = max(cutoff, 0.50)



    return max(cutoff, 0.82) if label in banned else cutoff


def shape_ok(label, box, size):
    shapes = {
        "refrigerator": (0.04, 0.07, (1.1, 4.5)),
        "wardrobe": (0.05, 0.08, (0.8, 5)),
        "bed": (0.06, 0.10, (0.15, 2.5)),
        "sofa": (0.03, 0.06, (0.15, 2.0)),
        "oven": (0.008, 0.05, (0.35, 2.5)),
        "microwave": (0.004, 0.03, (0.3, 1.6)),
        "cooktop": (0.004, 0.03, (0.08, 1.4)),
        "range hood": (0.004, 0.03, (0.12, 3.0)),
        "washing machine": (0.010, 0.06, (0.6, 2.6)),
        "painting": (0.0025, 0.035, (0.28, 3.5)),
        "mirror": (0.006, 0.05, (0.3, 4.0)),
        "TV": (0.006, 0.05, (0.3, 2.6)),
        "curtains": (0.01, 0.03, (0.8, 25)),
        "floor lamp": (0.003, 0.02, (1.5, 12)),
        "window blind": (0.008, 0.03, (0.05, 3.0)),
        "toilet": (0.01, 0.06, (0.6, 3.5)),
        "bathtub": (0.02, 0.08, (0.12, 1.6)),
        "sink": (0.004, 0.04, (0.15, 2.0)),
        "air conditioner": (0.004, 0.03, (0.15, 1.2)),
        "stool": (0.004, 0.03, (0.6, 4.0)),
        "chair": (0.005, 0.04, (0.6, 4.0)),
        "armchair": (0.012, 0.06, (0.5, 2.2)),
        "potted plant": (0.0015, 0.025, (0.35, 6.0)),
        "cushion": (0.0015, 0.025, (0.3, 2.0)),
        "ceiling light": (0.001, 0.015, (0.05, 3.0)),
        "pendant light": (0.0015, 0.02, (0.15, 6.0)),
        "water heater": (0.012, 0.06, (0.8, 4.0)),
        "ceiling fan": (0.004, 0.02, (0.08, 1.5)),
        "kitchen cabinet": (0.012, 0.06, (0.1, 6.0)),
    }
    max_area = {
        "painting": 0.25,
        "mirror": 0.35,
        "shower head": 0.03,
        "ceiling light": 0.06,
        "pendant light": 0.12,
        "ceiling fan": 0.20,
        "air conditioner": 0.10,
        "microwave": 0.08,
        "cooktop": 0.20,
        "range hood": 0.15,
        "potted plant": 0.20,
        "cushion": 0.08,
        "table lamp": 0.12,
        "vanity cabinet": 0.30,
        "sink": 0.25,
        "water heater": 0.06,
        "TV": 0.35,
        "stool": 0.20,
        "chair": 0.35,
        "oven": 0.25,
        "side table": 0.15,
        "bedside table": 0.15,
        "washing machine": 0.35,
    }
    band = {
        "ceiling light": (0, 0.45),
        "pendant light": (0, 0.55),
        "ceiling fan": (0, 0.45),
        "air conditioner": (0, 0.55),
        "range hood": (0, 0.55),
        "shower head": (0, 0.60),
        "water heater": (0, 0.65),
        "cooktop": (0.30, 1),
        "rug": (0.50, 1),
        "floor lamp": (0.20, 1),
        "toilet": (0.30, 1),
        "bathtub": (0.30, 1),
        "oven": (0.35, 1),
        "washing machine": (0.25, 1),
    }

    width, height = size
    box_wide, box_tall = box[2] - box[0], box[3] - box[1]

    if box_wide <= 0 or box_tall <= 0:
        return False

    area, short = box_wide * box_tall / (width * height), min(box_wide / width, box_tall / height)


    if label in shapes:
        min_area, min_short, (low_ratio, top_ratio) = shapes[label]
    else:
        min_area = 0.003 if label in SMALL_LABELS else 0.008
        min_short, low_ratio, top_ratio = 0.03, 0.12, 8.0

    if (
        area < min_area
        or area > max_area.get(label, 0.70)
        or short < min_short
        or not low_ratio <= box_tall / box_wide <= top_ratio
    ):
        return False

    top, bottom = band.get(label, (0, 1))



    if not top <= (box[1] + box[3]) / (2 * height) <= bottom:
        return False

    if label in ("ceiling light", "pendant light", "ceiling fan") and (
        box[0] <= 0.01 * width or box[2] >= 0.99 * width
    ):
        return False

    edge = box[0] <= 0.01 * width or box[2] >= 0.99 * width


    return not (
        edge
        and label not in ("curtains", "window blind")
        and box_wide < 0.10 * width
        and box_tall / box_wide > 2.5
    )


def mask_ok(mask, box, label=""):
    box_left, box_top, box_right, box_bottom = box

    if label in {"bedside table", "side table", "coffee table", "table", "shelf"}:
        minimum = 0.006
    elif label in SMALL_LABELS:
        minimum = 0.0006
    else:
        minimum = 0.010


    if label == "painting":
        fill = 0.70
    elif label == "window blind":
        fill = 0.12
    else:
        fill = 0.25

    if box_right <= box_left or box_bottom <= box_top:
        return False

    visible = mask[box_top:box_bottom, box_left:box_right]

    return visible.sum() >= 100 and visible.sum() / mask.size >= minimum and visible.mean() >= fill


def nms(boxes, scores, threshold=0.6, labels=None, limit=12):
    keep = []

    for try_no in np.argsort(scores)[::-1]:
        try_box = np.asarray(boxes[try_no])
        twin = False

        for kept_no in keep:
            kept_box = np.asarray(boxes[kept_no])
            overlap = _iou(try_box, kept_box)
            same = labels is not None and labels[try_no] == labels[kept_no]
            inter = np.prod(
                np.maximum(
                    0, np.minimum(try_box[2:], kept_box[2:]) - np.maximum(try_box[:2], kept_box[:2])
                )
            )


            inside = inter / max(
                min(np.prod(try_box[2:] - try_box[:2]), np.prod(kept_box[2:] - kept_box[:2])), 1
            )

            if overlap > threshold or (same and (overlap > 0.35 or inside > 0.8)):
                twin = True

                break

        if not twin:
            keep.append(int(try_no))

        if len(keep) == limit:
            break

    return keep


# Find other possible object names
def mixups(label):
    if label in ("dining table", "coffee table", "side table", "bedside table"):
        return [
            "dining table",
            "coffee table",
            "side table",
            "bedside table",
            "desk",
            "cabinet",
            "chair",
            "bare floor",
            "rug",
            "kitchen island counter",
        ]



    if label == "window blind":
        return [
            "window blind",
            "curtains",
            "window glass",
            "blank wall",
            "ceiling",
            "range hood",
            "radiator",
            "arched doorway",
            "cabinet",
            "air conditioner",
            "glass door",
        ]


    if label == "cushion":
        return [
            "cushion",
            "folded blanket",
            "chair",
            "towel",
            "bag or clothing",
            "unrecognizable object",
        ]


    if label == "potted plant":
        return [
            "potted plant",
            "vase",
            "painting",
            "kitchen utensil",
            "pan",
            "cut flowers in a vase",
            "empty flowerpot",
            "tree outside a window",
            "bag or clothing",
            "plant sticker on a wall",
            "unrecognizable object",
        ]



    if label == "curtains":
        return [
            "curtains",
            "window blind",
            "shower curtain",
            "door",
            "door frame",
            "blank wall",
            "wall corner",
            "window",
            "towel",
        ]


    if label == "rug":
        return ["rug", "fitted carpet flooring", "floor tiles", "blanket", "shadow"]

    if label == "painting":
        return [
            "painting",
            "window",
            "window louvres",
            "mirror",
            "glass shower door",
            "blank wall",
            "tiled wall",
            "kitchen cabinet door",
            "fridge magnet",
            "paper notice",
            "air conditioner",
            "wall clock",
            "curtains",
            "TV",
            "potted plant",
            "hanging clothes",
            "towel",
        ]

    groups = [
        [
            "floor lamp",
            "table lamp",
            "pendant light",
            "ceiling fan",
            "ceiling light",
            "wall light",
            "coat stand",
            "shower fixture",
            "kettle",
            "blender",
            "range hood",
            "exhaust fan",
            "wall hook",
            "oven glove",
            "kitchen utensil",
            "vase",
            "decorative sculpture",
            "bottle",
            "air conditioner",
            "smoke detector",
            "blank ceiling",
        ],
        [
            "mirror",
            "window",
            "painting",
            "glass shower door",
            "blank wall",
            "tiled wall",
            "cabinet door",
            "TV",
            "countertop",
        ],
        [
            "refrigerator",
            "oven",
            "microwave",
            "cooktop",
            "range hood",
            "kitchen cabinet",
            "tall cabinet",
            "door",
            "washing machine",
            "TV",
            "wall panel",
            "countertop",
            "tiled wall",
            "window",
            "sink",
        ],
        [
            "TV",
            "oven",
            "microwave",
            "window",
            "mirror",
            "painting",
            "kitchen cabinet",
            "blank wall",
            "air conditioner",
        ],
        [
            "air conditioner",
            "window blind",
            "cabinet",
            "range hood",
            "water heater",
            "blank wall",
            "speaker",
        ],
        [
            "cabinet",
            "bedside table",
            "side table",
            "wardrobe",
            "kitchen cabinet",
            "vanity cabinet",
            "shelf",
            "door",
            "window",
            "countertop",
            "blank wall",
        ],
        [
            "sofa",
            "armchair",
            "chair",
            "stool",
            "bed",
            "bench",
            "chaise lounge",
            "table",
            "bare floor",
            "trash bin",
            "laundry basket",
            "storage box",
        ],
        [
            "sink",
            "bathtub",
            "toilet",
            "shower head",
            "vanity cabinet",
            "mirror",
            "water heater",
            "towel",
            "tap",
            "window",
            "tiled wall",
            "cabinet",
            "countertop",
        ],
        ["desk", "dining table", "coffee table", "cabinet", "shelf", "chair"],
    ]

    for group in groups:
        if label in group:
            return group


    return [label, "unrecognizable object", "empty wall", "window", "door"]


FAMILIES = [
    {"sofa", "chaise lounge"},
    {"table lamp", "floor lamp", "pendant light", "ceiling light", "wall light"},
    {"cabinet", "kitchen cabinet", "tall cabinet", "wardrobe"},
]


def sure_label(label, choices, probabilities):
    rank = np.argsort(probabilities)
    best = int(rank[-1])
    margin = float(probabilities[best] - probabilities[rank[-2]])
    family = None

    for try_group in FAMILIES:
        if label in try_group:
            family = try_group

            break

    if (
        family
        and choices[best] in family
        and probabilities[best] >= 0.25
        and sum(
            probability for choice, probability in zip(choices, probabilities) if choice in family
        )
        >= 0.60
    ):
        return choices[best] if choices[best] in LABELS else label

    if label in ("coffee table", "dining table", "side table", "bedside table"):
        table_ids = [
            i
            for i, choice in enumerate(choices)
            if choice in ("coffee table", "dining table", "side table", "bedside table")
        ]

        if (
            sum(probabilities[table_index] for table_index in table_ids) >= 0.65
            and best in table_ids
            and probabilities[best] >= 0.35
        ):
            return choices[best] if margin >= 0.10 else "table"

    if (
        label in ("chair", "armchair")
        and choices[best] in ("chair", "armchair")
        and probabilities[best] >= 0.35
    ):
        if choices[best] == "armchair" and probabilities[best] >= 0.65 and margin >= 0.20:
            return "armchair"



        return "chair"

    if label == "window blind" and (
        choices[best] != label or probabilities[best] < 0.50 or margin < 0.15
    ):
        return ""

    if label == "painting" and (
        choices[best] != label or probabilities[best] < 0.45 or margin < 0.15
    ):
        return ""

    if choices[best] != label and probabilities[best] >= 0.48 and margin >= 0.15:
        return choices[best] if choices[best] in LABELS else ""

    if label in SMALL_LABELS:
        min_chance, min_gap = 0.32, 0.07
    else:
        min_chance, min_gap = 0.40, 0.10


    if choices[best] != label or probabilities[best] < min_chance or margin < min_gap:
        return ""

    return label


# Check the photo's object name
def check_crop(label, score, box, room, cut, source, clip_scores):
    if label == "potted plant" and not plant_ok(cut):
        return "", {}

    if label == "lamp":
        label = "table lamp"

    if label == "window blind" and not blind_ok(cut):
        return "", {}

    choices = mixups(label)

    details = {
        "window blind": "roller window shade",
        "cushion": "cushion or decorative throw pillow",
        "painting": "framed painting or picture hanging on a wall",
        "window louvres": "louvred window with glass slats",
        "cooktop": "kitchen hob or gas stove",
        "range hood": "kitchen cooker hood",
        "ceiling light": "ceiling light fixture",
        "air conditioner": "wall-mounted air conditioner unit",
        "stool": "stool or bar stool",
        "sink": "sink or wash basin",
        "vanity cabinet": "bathroom vanity cabinet under a basin",
        "TV": "flat-screen TV",
        "oven": "built-in oven with a glass door",
    }

    prompts = [f"a photo of a {details.get(choice, choice)}" for choice in choices]
    odds = clip_scores(source.crop(padded(box, source.size)), prompts)
    sure = sure_label(label, choices, odds)



    if (
        sure
        and sure != label
        and label in choices
        and not shape_ok(sure, box, source.size)
        and shape_ok(label, box, source.size)
    ):
        rank = np.argsort(odds)



        second = int(rank[-2])

        if choices[second] == label and odds[second] >= 0.10:
            sure = label
        else:
            sure = ""

    if (
        label == "mirror"
        and not sure
        and score >= 0.40
        and choices[int(np.argmax(odds))] in ("mirror", "glass shower door", "window")
    ):
        sure = "mirror?"

    if label == "window blind" and not sure and score >= 0.45:
        best = choices[int(np.argmax(odds))]
        alpha = np.asarray(cut.convert("RGBA"))[:, :, 3] > 0
        image_width, image_height = source.size

        if (
            best in ("ceiling", "blank wall")
            and alpha.sum() / (image_width * image_height) > 0.03
            and box[0] > 0.03 * image_width
            and box[2] < 0.97 * image_width
            and box[3] < 0.5 * image_height
        ):
            sure = "window blind"


    if sure in ("table lamp", "floor lamp") and box[1] <= 0.02 * source.size[1]:
        sure = "pendant light"

    if sure in ("table lamp", "floor lamp"):
        alpha = np.asarray(cut.convert("RGBA"))[:, :, 3] > 0
        height, width = alpha.shape
        top = alpha[: max(1, height // 3)].any(axis=0).sum()



        bottom = alpha[height * 2 // 3 :].any(axis=0).sum()

        if width / max(height, 1) > 0.8 and bottom / max(top, 1) > 0.65:
            sure = "lamp"

    sure = fit_label(sure, room, score)

    if sure in ("armchair", "chair") and (box[2] - box[0]) / max(box[3] - box[1], 1) > 2.2:
        sure = "sofa"



    return sure, dict(zip(choices, odds.tolist()))


LABEL_LIMITS = {
    "painting": 3,
    "chair": 4,
    "stool": 4,
    "cushion": 3,
    "potted plant": 3,
    "kitchen cabinet": 2,
    "curtains": 2,
    "refrigerator": 1,
    "range hood": 1,
    "cooktop": 1,
    "microwave": 1,
    "oven": 1,
    "bed": 1,
    "TV": 1,
    "bathtub": 1,
    "toilet": 1,
    "vanity cabinet": 1,
    "water heater": 1,
    "shower head": 1,
    "sofa": 2,
    "mirror": 2,
    "washing machine": 2,
}


def anchored(item, items):
    item_box = item["box"]
    sinks = []



    for other in items:
        if other["label"] != "sink":
            continue


        centre = (other["box"][0] + other["box"][2]) / 2

        if item_box[0] <= centre <= item_box[2]:
            sinks.append(other["box"])

    if item["label"] == "vanity cabinet":
        return any(
            sink_box[3] >= item_box[1] and sink_box[1] <= (item_box[1] + item_box[3]) / 2
            for sink_box in sinks
        )

    if item.get("needs_sink"):
        return any(
            item_box[3] <= (sink_box[1] + sink_box[3]) / 2 + 0.05 * (item_box[3] - item_box[1])
            for sink_box in sinks
        )

    return True


def in_other(label, box, items, fraction=0.75):
    containers = {
        "mirror",
        "refrigerator",
        "TV",
        "oven",
        "microwave",
        "window blind",
        "painting",
        "air conditioner",
        "wardrobe",
        "range hood",
        "toilet",
        "bathtub",
        "washing machine",
    }

    item_box = np.asarray(box, dtype=float)



    area = max(np.prod(item_box[2:] - item_box[:2]), 1)

    for other in items:
        if other["label"] not in containers or other["label"] == label:
            continue

        box_two = np.asarray(other["box"], dtype=float)
        inter = np.prod(
            np.maximum(
                0, np.minimum(item_box[2:], box_two[2:]) - np.maximum(item_box[:2], box_two[:2])
            )
        )

        if inter / area >= fraction and np.prod(box_two[2:] - box_two[:2]) > area:
            return True

    return False


def padded(box, size, fraction=0.08):
    width, height = size
    x_pad, y_pad = (box[2] - box[0]) * fraction, (box[3] - box[1]) * fraction

    return (
        max(0, int(box[0] - x_pad)),
        max(0, int(box[1] - y_pad)),
        min(width, int(box[2] + x_pad)),
        min(height, int(box[3] + y_pad)),
    )


def fit_label(label, room, score):
    if label == "bedside table" and "bedroom" not in room:
        return "table"

    if label == "dining table" and "bedroom" in room and score < 0.82:
        return "table"

    return label


def plant_ok(cut):
    rgba = np.asarray(cut.convert("RGBA"))
    mask = rgba[:, :, 3] > 0
    rgb = rgba[:, :, :3].astype(float)
    green = (rgb[:, :, 1] > rgb[:, :, 0] + 5) & (rgb[:, :, 1] > rgb[:, :, 2] + 5) & mask

    if green.sum() / max(mask.sum(), 1) < 0.03:
        return False

    lower = mask[mask.shape[0] * 2 // 3 :]
    green_low = green[mask.shape[0] * 2 // 3 :]

    return lower.sum() > 20 and (lower & ~green_low).sum() / max(lower.sum(), 1) > 0.25


def blind_ok(cut):
    rgba = np.asarray(cut.convert("RGBA"))
    mask = rgba[:, :, 3] > 0

    if mask.sum() < 100:
        return False

    gray = rgba[:, :, :3].mean(axis=2)

    if gray[mask].std() < 18:
        return True

    x_diffs = np.abs(np.diff(gray, axis=1))
    y_diffs = np.abs(np.diff(gray, axis=0))
    x_mask = mask[:, 1:] & mask[:, :-1]
    y_mask = mask[1:] & mask[:-1]
    x_change = float(x_diffs[x_mask].mean()) if x_mask.any() else 0
    y_change = float(y_diffs[y_mask].mean()) if y_mask.any() else 0


    return max(x_change, y_change) / max(min(x_change, y_change), 0.1) >= 2.8


def _iou(a, b):
    size = np.maximum(0, np.minimum(a[2:], b[2:]) - np.maximum(a[:2], b[:2]))
    inter = np.prod(size)

    return inter / max(np.prod(a[2:] - a[:2]) + np.prod(b[2:] - b[:2]) - inter, 1)


def polygon(mask):
    mask_rows = np.flatnonzero(mask.any(axis=1))

    if not len(mask_rows):
        return []

    rows = np.unique(np.linspace(mask_rows[0], mask_rows[-1], min(32, len(mask_rows))).astype(int))
    spans = [(int(row), np.flatnonzero(mask[row])) for row in rows]
    spans = [(row, columns) for row, columns in spans if len(columns)]
    left_edge = [[int(columns[0]), row] for row, columns in spans]
    right_edge = [[int(columns[-1]), row] for row, columns in reversed(spans)]

    return left_edge + right_edge


def _analyse(path, room=""):
    import torch

    start = time.monotonic()
    image = Image.open(path).convert("RGB")
    image.thumbnail((1000, 1000))
    width, height = image.size

    with torch.inference_mode():
        dino_prep, dino = load("dino")
        boxes, scores, labels = [], [], []


        clues = []
        wrongs = []

        def detect(view, vocabulary, offset=(0, 0), small_only=False):
            inputs = dino_prep(
                images=view,
                text=". ".join(vocabulary) + ".",
                return_tensors="pt",
                size={"shortest_edge": 640, "longest_edge": 1000},
            )
            inputs = onDevice(inputs)

            result = dino_prep.post_process_grounded_object_detection(
                dino(**inputs),
                inputs.input_ids,
                threshold=0.21,
                text_threshold=0.19,
                target_sizes=[(view.height, view.width)],
            )[0]

            for box, score, raw_tag in zip(
                result["boxes"].tolist(),
                result["scores"].tolist(),
                result.get("text_labels", result.get("labels")),
            ):
                label = canonical(raw_tag)



                box = [
                    box[0] + offset[0],
                    box[1] + offset[1],
                    box[2] + offset[0],
                    box[3] + offset[1],
                ]

                if label in NEGATIVE_LABELS:
                    if score >= 0.30:
                        wrongs.append((box, float(score)))

                    continue

                if (
                    not label
                    or (small_only and label not in SMALL_LABELS)
                    or score < room_cutoff(label, room)
                ):
                    continue

                if small_only and (
                    (offset[0] and box[0] - offset[0] < 3) or (offset[1] and box[1] - offset[1] < 3)
                ):
                    continue

                if not shape_ok(label, box, (width, height)):
                    clues.append(
                        {
                            "label": label,
                            "reason": "shape",
                            "box": [int(coordinate) for coordinate in box],
                        }
                    )


                    continue

                boxes.append(box)
                scores.append(float(score))
                labels.append(label)

        for chunk in VOCABULARY[room_kind(room)]:
            detect(image, chunk)


        tile_w, tile_h = int(width * 0.62), int(height * 0.62)

        small_text = [
            "table lamp",
            "floor lamp",
            "pendant lamp",
            "ceiling fan",
            "framed picture",
            "potted plant",
            "cushion",
            "roller blind",
            "side table",
            "window",
            "door",
        ]

        if room_kind(room) == "kitchen":
            small_text = [
                "pendant lamp",
                "potted plant",
                "microwave",
                "bar stool",
                "framed picture",
                "window",
                "door",
            ]
        elif room_kind(room) == "bathroom":
            small_text = [
                "rain shower",
                "potted plant",
                "ceiling light",
                "framed picture",
                "window",
                "door",
            ]

        if len(nms(boxes, scores, labels=labels)) >= 4:
            tiles = []
        else:
            tiles = [
                (0, 0),
                (width - tile_w, 0),
                (0, height - tile_h),
                (width - tile_w, height - tile_h),
            ]

        for here, y_now in tiles:
            detect(
                image.crop((here, y_now, here + tile_w, y_now + tile_h)),
                small_text,
                (here, y_now),
                small_only=True,
            )

        framed = (
            "painting",
            "mirror",
            "TV",
            "refrigerator",
            "wardrobe",
            "kitchen cabinet",
            "cabinet",
            "window blind",
            "shelf",
        )

        for box_no in range(len(boxes)):
            if labels[box_no] not in framed:
                continue

            real_gap = False

            for wrong_box, wrong_score in wrongs:
                overlap = _iou(np.asarray(boxes[box_no]), np.asarray(wrong_box))

                if overlap > 0.55 and wrong_score >= scores[box_no]:
                    real_gap = True

                    break

            if real_gap:
                clues.append(
                    {
                        "label": labels[box_no],
                        "reason": "window/door",
                        "box": [int(coordinate) for coordinate in boxes[box_no]],
                    }
                )
                scores[box_no] = 0.0

        merged = set()

        for box_no, box in enumerate(boxes):
            if labels[box_no] != "painting":
                continue

            big_box = np.asarray(box)
            area = np.prod(big_box[2:] - big_box[:2])
            kids = []

            for kid_no, in_box in enumerate(boxes):
                if (
                    kid_no != box_no
                    and labels[kid_no] == "painting"
                    and np.prod(np.asarray(in_box[2:]) - in_box[:2]) < 0.65 * area
                    and in_box[0] >= big_box[0] - 3
                    and in_box[1] >= big_box[1] - 3
                    and in_box[2] <= big_box[2] + 3
                    and in_box[3] <= big_box[3] + 3
                ):
                    kids.append(kid_no)

            apart = False

            for kid_no in kids:
                for other_no in kids:
                    if (
                        kid_no < other_no
                        and _iou(np.asarray(boxes[kid_no]), np.asarray(boxes[other_no])) < 0.15
                    ):
                        apart = True

                        break

                if apart:
                    break

            if apart:
                merged.add(box_no)

        eligible = [i for i in range(len(boxes)) if i not in merged and scores[i] > 0]

        kept = nms(
            [boxes[box_index] for box_index in eligible],
            [scores[box_index] for box_index in eligible],
            labels=[labels[box_index] for box_index in eligible],
            limit=48,
        )

        ids = [eligible[kept_index] for kept_index in kept]
        sam_prep, sam = load("sam")
        base = sam_prep(images=image, return_tensors="pt")
        traits = sam.get_image_embeddings(onDevice(base["pixel_values"]))

        def segment(**prompts):
            data = sam_prep(images=image, return_tensors="pt", **prompts)
            prompt_in = {}

            for key, value in data.items():
                if key not in ("pixel_values", "original_sizes", "reshaped_input_sizes"):
                    prompt_in[key] = onDevice(value)


            out = sam(image_embeddings=traits, **prompt_in)
            masks = sam_prep.post_process_masks(
                out.pred_masks.cpu(), data["original_sizes"], data["reshaped_input_sizes"]
            )[0]
            order = out.iou_scores[0].argsort(dim=-1, descending=True)

            return [
                [masks[i, int(mask_index)].numpy().astype(bool) for mask_index in order[i]]
                for i in range(len(order))
            ]

        masks = []

        for box_no, choices in zip(
            ids, segment(input_boxes=[[boxes[box_no] for box_no in ids]]) if ids else []
        ):
            box = [max(0, int(coordinate)) for coordinate in boxes[box_no]]
            box[2], box[3] = min(width, box[2]), min(height, box[3])
            masks.append(
                next(
                    (choice for choice in choices if mask_ok(choice, box, labels[box_no])),
                    choices[0],
                )
            )

        dep, dem = load("depth")
        depth_in = dep(
            images=image.resize((252, 252)), return_tensors="pt", size={"height": 252, "width": 252}
        )
        raw = dem(**onDevice(depth_in)).predicted_depth[:, None]
        depth = torch.nn.functional.interpolate(
            raw, (height, width), mode="bicubic", align_corners=False
        )
        depth = depth[0, 0].cpu().numpy()
        clip_prep, clip = load("clip")
        cache = {}
        ready = []

        for box_no, mask in zip(ids, masks):
            box = [max(0, int(coordinate)) for coordinate in boxes[box_no]]
            box[2], box[3] = min(width, box[2]), min(height, box[3])

            if box[2] <= box[0] or box[3] <= box[1]:
                continue

            clipped = np.zeros_like(mask)
            clipped[box[1] : box[3], box[0] : box[2]] = mask[box[1] : box[3], box[0] : box[2]]
            rgba = image.convert("RGBA")


            rgba.putalpha(Image.fromarray((clipped * 255).astype("uint8")))
            cut = rgba.crop(box)
            white = Image.new("RGB", cut.size, "white")
            white.paste(cut, mask=cut.getchannel("A"))
            ready.extend([image.crop(padded(box, image.size)), white])

        if ready:
            vectors = clip.get_image_features(
                **onDevice(clip_prep(images=ready, return_tensors="pt"))
            )

            if not isinstance(vectors, torch.Tensor):
                vectors = vectors.pooler_output

            vectors = torch.nn.functional.normalize(vectors.float(), dim=-1)

            for ready_pic, vector in zip(ready, vectors):
                cache[(ready_pic.size, ready_pic.tobytes())] = vector[None]

        last_image, last_feature = None, None

        def image_feature(im):
            nonlocal last_image, last_feature

            if im is not last_image:
                key = (im.size, im.tobytes())
                vector = cache.get(key)


                if vector is None:
                    vector = clip.get_image_features(
                        **onDevice(clip_prep(images=im, return_tensors="pt"))
                    )

                    if not isinstance(vector, torch.Tensor):
                        vector = vector.pooler_output

                last_image = im
                last_feature = torch.nn.functional.normalize(vector.float(), dim=-1)

            return last_feature


        def clip_scores(im, texts):
            logits = image_feature(im) @ embed_text(tuple(texts)).T * clip.logit_scale.exp()

            return logits[0].softmax(-1).cpu().numpy()

        items = []
        taken = np.zeros((height, width), dtype=bool)
        depth_lo, depth_hi = np.quantile(depth, [0.33, 0.66])

        for box_no, mask in zip(ids, masks):
            box = [max(0, int(coordinate)) for coordinate in boxes[box_no]]


            box[2], box[3] = min(width, box[2]), min(height, box[3])


            if labels[box_no] == "window blind" and (
                (box[2] - box[0]) < 0.12 * width or (box[1] + box[3]) / 2 > 0.65 * height
            ):
                continue

            if labels[box_no] == "cushion" and (box[2] - box[0]) / max(box[3] - box[1], 1) > 2.0:
                continue

            if not mask_ok(mask, box, labels[box_no]):
                clues.append({"label": labels[box_no], "reason": "mask", "box": box})

                continue


            clipped = np.zeros_like(mask)
            clipped[box[1] : box[3], box[0] : box[2]] = mask[box[1] : box[3], box[0] : box[2]]
            mask = clipped

            if not mask_ok(mask, box, labels[box_no]):
                clues.append({"label": labels[box_no], "reason": "mask", "box": box})

                continue

            rgba = image.convert("RGBA")
            rgba.putalpha(Image.fromarray((mask * 255).astype("uint8")))
            cut = rgba.crop(box)
            white = Image.new("RGB", cut.size, "white")
            white.paste(cut, mask=cut.getchannel("A"))
            label, odds = check_crop(
                labels[box_no], scores[box_no], box, room, cut, image, clip_scores
            )
            needs_sink = label == "mirror?"

            if needs_sink:
                label = "mirror"

            clues.append(
                {"label": labels[box_no], "verified": label, "box": box, "probabilities": odds}
            )

            if (
                not label
                or scores[box_no] < room_cutoff(label, room)
                or not mask_ok(mask, box, label)
                or not shape_ok(label, box, (width, height))
            ):
                continue

            on_floor = label not in SMALL_LABELS or label in ("bedside table", "side table")

            if on_floor and label not in ("curtains", "mirror"):
                taken |= mask

            if sum(item["label"] == label for item in items) >= LABEL_LIMITS.get(label, 2):
                continue

            attrs = {}

            for key, all_choices in PALETTES.items():
                if key == "material":
                    choices = MATERIALS.get(label, [])
                elif key == "style":
                    choices = all_choices if label in STYLE_LABELS else []
                else:
                    choices = all_choices

                if not choices:
                    continue


                options = choices + ["uncertain"]
                probs = clip_scores(white, [f"a photo of a {option} {label}" for option in options])
                rank = np.argsort(probs)

                if (
                    rank[-1] < len(choices)
                    and probs[rank[-1]] >= 0.55
                    and probs[rank[-1]] - probs[rank[-2]] >= 0.20
                ):
                    attrs[key] = choices[rank[-1]]

            emb = image_feature(white)[0].cpu().numpy()
            median = float(np.median(depth[mask]))

            if median > depth_hi:
                place = "front"
            elif median < depth_lo:
                place = "back"
            else:
                place = "middle"

            x_mid = (box[0] + box[2]) / (2 * width)

            if x_mid < 0.33:
                place += "-left"
            elif x_mid > 0.66:
                place += "-right"
            else:
                place += "-centre"

            buf = io.BytesIO()
            cut.save(buf, format="PNG")

            items.append(
                dict(
                    label=label,
                    needs_sink=needs_sink,
                    score=float(scores[box_no]),
                    box=box,
                    polygon=polygon(mask),
                    attributes=attrs,
                    placement=place,
                    embedding=emb.tolist(),
                    png=buf.getvalue(),
                )
            )

        items = [item for item in items if anchored(item, items)]
        within = []

        for here in items:
            others = [other_item for other_item in items if other_item is not here]

            if in_other(here["label"], here["box"], others):
                within.append(here)

        for here in within:
            clues.append({"label": here["label"], "reason": "inside", "box": here["box"]})

        items = [
            item for item in items if not any(item is contained_item for contained_item in within)
        ]

        for here in items:
            here.pop("needs_sink", None)

        kept = nms(
            [item["box"] for item in items],
            [item["score"] for item in items],
            labels=[item["label"] for item in items],
            limit=10,
        )
        items = [items[kept_index] for kept_index in kept]
        lower = np.indices((height, width))[0] >= height * 0.64
        layer = lower & (depth >= np.quantile(depth[lower], 0.18)) & ~taken
        seeds = []

        for here in (0.10, 0.25, 0.50, 0.75, 0.90):
            for y_now in (0.91, 0.78):
                x_pixel, y_pixel = min(width - 1, int(width * here)), min(
                    height - 1, int(height * y_now)
                )

                if layer[y_pixel, x_pixel]:
                    seeds.append([x_pixel, y_pixel])

        sam_floor = np.zeros((height, width), dtype=bool)

        if seeds:
            candidates = [
                choices[0]
                for choices in segment(
                    input_points=[[[point] for point in seeds]], input_labels=[[[1] for _ in seeds]]
                )
            ]
            valid = []

            for try_mask in candidates:
                if (
                    0.02 < try_mask.mean() < 0.80
                    and (try_mask & lower).sum() > 0.015 * width * height
                ):
                    valid.append(try_mask & ~taken)

            if valid:
                sam_floor = max(valid, key=lambda m: (m & layer).sum())

        if (layer & sam_floor).sum() >= 0.015 * width * height:
            floor = layer & sam_floor
        else:
            floor = layer

        flooring = (
            tag_floor(image, floor, clip_scores, image_feature, clip) if room != "view" else {}
        )
        spots = []


        for here in items:
            spots.append(f"{here['label'].capitalize()} {here['placement']}")

        layout = (
            "; ".join(spots)
            + ". Relative positions only; orientation and distances are not estimated."
        )

        return {
            "items": items,
            "flooring": flooring,
            "layout": layout,
            "timings": {"total_seconds": round(time.monotonic() - start, 2), "quality": clues},
            "size": [width, height],
        }


@lru_cache(maxsize=256)
def embed_text(texts):
    import torch

    clip_prep, clip = load("clip")

    with torch.inference_mode():
        vectors = clip.get_text_features(
            **onDevice(clip_prep(text=list(texts), return_tensors="pt", padding=True))
        )

        if not isinstance(vectors, torch.Tensor):
            vectors = vectors.pooler_output

        return torch.nn.functional.normalize(vectors.float(), dim=-1)


def tag_floor(image, floor, clip_scores, image_feature, model):
    materials = [
        "carpet",
        "vinyl wood plank",
        "parquet",
        "marble tile",
        "porcelain tile",
        "terrazzo",
        "polished concrete",
        "laminate",
    ]

    width, height = image.size

    if floor.mean() < 0.015:
        return {}

    size = max(24, int(min(width, height) * 0.12))
    patches = []

    for y_spot in np.linspace(height * 0.69, height - size - 1, 7).astype(int):
        for x_spot in np.linspace(0, width - size - 1, 12).astype(int):
            share = float(floor[y_spot : y_spot + size, x_spot : x_spot + size].mean())

            if share >= 0.75:
                patches.append(
                    (share, (int(x_spot), int(y_spot), int(x_spot + size), int(y_spot + size)))
                )

    patches.sort(reverse=True)
    picked = []

    for _, box in patches:
        if not any(_iou(np.asarray(box), np.asarray(old)) > 0.1 for old in picked):
            picked.append(box)

        if len(picked) == 3:
            break

    if not picked:
        return {}

    prompts = [
        "a close-up photo of {} flooring",
        "a photo of a {} floor",
        "the room has {} flooring",
    ]
    texts = []

    for made_of in materials:
        for prompt in prompts:
            texts.append(prompt.format(made_of))

    texts = tuple(texts)
    logits = []
    checks = []



    for box in picked:
        patch = image.crop(box)
        traits = image_feature(patch)
        source = (traits @ embed_text(texts).T * model.logit_scale.exp())[0].cpu().numpy()
        contrast = 4.0 if np.asarray(patch).mean(axis=2).std() < 12 else 2.0
        texture = ImageEnhance.Contrast(patch).enhance(contrast)



        better = (
            (image_feature(texture) @ embed_text(texts).T * model.logit_scale.exp())[0]
            .cpu()
            .numpy()
        )
        values = (0.25 * source + 0.75 * better).reshape(len(materials), 3).mean(axis=1)
        logits.append(values)
        floor_ok = clip_scores(
            patch,
            [
                "a close-up photo of flooring",
                "a photo of furniture upholstery",
                "a photo of a wall",
            ],
        )
        checks.append(float(floor_ok[0]))

    values = np.mean(logits, axis=0)
    exp = np.exp(values - values.max())
    odds = exp / exp.sum()



    rank = np.argsort(odds)
    best = int(rank[-1])
    margin = float(odds[best] - odds[rank[-2]])
    certain = odds[best] >= 0.35 and margin >= 0.10
    made_of = materials[best]

    return {
        "label": made_of if certain else "Possibly " + made_of,
        "material": made_of,
        "score": float(odds[best]),
        "margin": margin,
        "possible": not bool(certain),
        "floor_validation": float(np.mean(checks)),
        "polygon": polygon(floor),
        "sample_boxes": [list(sample_box) for sample_box in picked],
    }


_ANALYSIS_CACHE = {}


def analyse(path, room=""):
    import hashlib
    from pretrained import tagErrors

    issues = tagErrors()

    if issues:
        raise ValueError("Model identity mismatch; staff review required: " + "; ".join(issues))



    start = time.monotonic()
    key = (hashlib.sha256(Path(path).read_bytes()).hexdigest(), room)

    if key in _ANALYSIS_CACHE:
        result = _ANALYSIS_CACHE[key]

        return dict(
            result,
            timings=dict(
                result["timings"],
                total_seconds=round(time.monotonic() - start, 3),
                inference_seconds=result["timings"]["total_seconds"],
                cache_hit=True,
            ),
        )

    from .brands import add_brands

    result = add_brands(path, _analyse(path, room))
    result["timings"]["cache_hit"] = False

    if len(_ANALYSIS_CACHE) >= 32:
        _ANALYSIS_CACHE.pop(next(iter(_ANALYSIS_CACHE)))

    _ANALYSIS_CACHE[key] = result

    return result


PICK_BACKGROUND = [
    "wall",
    "floor",
    "ceiling",
    "window",
    "door",
    "tiled wall",
    "countertop",
    "empty room",
]


def pick_names(room):
    kind = room_kind(room)
    labels = [label for label in LABELS if label != "sink"]

    if kind != "bathroom":
        labels = [label for label in labels if label not in BATHROOM_ONLY]

    if kind == "bathroom":
        labels = [label for label in labels if label not in KITCHEN_ONLY]

    return labels + ["kitchen sink" if kind != "bathroom" else "wash basin"]


def best_mask(choices, point, size):
    width, height = size
    usable = []

    for mask, quality in choices:
        share = mask.sum() / (width * height)


        if mask[point[1], point[0]] and 0.002 <= share <= 0.55:
            usable.append((quality, share, mask))

    if not usable:
        return None

    best = max(quality for quality, _, _ in usable)
    close = [choice for choice in usable if choice[0] >= best - 0.04]

    return max(close, key=lambda choice: choice[1])[2]


def is_room(mask):
    edges = sum(bool(edge.any()) for edge in (mask[0], mask[-1], mask[:, 0], mask[:, -1]))

    return mask.mean() > 0.20 and edges >= 2


def part_at(mask, x, y):
    from scipy import ndimage

    parts, _ = ndimage.label(mask)

    return parts == parts[y, x] if parts[y, x] else mask


def pick_item(path, x, y, room=""):
    import torch

    image = Image.open(path).convert("RGB")
    image.thumbnail((1000, 1000))
    width, height = image.size
    x_pixel, y_pixel = min(width - 1, max(0, int(x * width))), min(
        height - 1, max(0, int(y * height))
    )

    with torch.inference_mode():
        sam_prep, sam = load("sam")
        data = sam_prep(
            images=image,
            input_points=[[[[x_pixel, y_pixel]]]],
            input_labels=[[[1]]],
            return_tensors="pt",
        )
        traits = sam.get_image_embeddings(onDevice(data["pixel_values"]))

        prompts = {
            field_name: onDevice(field_value)
            for field_name, field_value in data.items()
            if field_name not in ("pixel_values", "original_sizes", "reshaped_input_sizes")
        }

        out = sam(image_embeddings=traits, **prompts)
        masks = sam_prep.post_process_masks(
            out.pred_masks.cpu(), data["original_sizes"], data["reshaped_input_sizes"]
        )[0]
        grades = out.iou_scores[0, 0].float().cpu().numpy()
        choices = [
            (masks[0, mask_index].numpy().astype(bool), float(grades[mask_index]))
            for mask_index in range(masks.shape[1])
        ]



        mask = best_mask(choices, (x_pixel, y_pixel), (width, height))

        if mask is None:
            return {"empty": True, "label": ""}

        mask = part_at(mask, x_pixel, y_pixel)

        if is_room(mask):
            return {"empty": True, "label": "wall"}



        y_spots, x_spots = np.nonzero(mask)
        box = [
            int(x_spots.min()),
            int(y_spots.min()),
            int(x_spots.max()) + 1,
            int(y_spots.max()) + 1,
        ]
        rgba = image.convert("RGBA")
        rgba.putalpha(Image.fromarray((mask * 255).astype("uint8")))
        cut = rgba.crop(box)
        white = Image.new("RGB", cut.size, "white")
        white.paste(cut, mask=cut.getchannel("A"))
        clip_prep, clip = load("clip")
        vector = clip.get_image_features(**onDevice(clip_prep(images=white, return_tensors="pt")))



        if not isinstance(vector, torch.Tensor):
            vector = vector.pooler_output

        vector = torch.nn.functional.normalize(vector.float(), dim=-1)
        labels = pick_names(room)
        texts = [f"a photo of a {label}" for label in labels + PICK_BACKGROUND]
        words = embed_text(tuple(texts)).T * clip.logit_scale.exp()
        probs = (vector @ words)[0].softmax(-1).cpu().numpy()

        if mask.mean() > 0.08:
            around = clip.get_image_features(
                **onDevice(
                    clip_prep(images=image.crop(padded(box, image.size)), return_tensors="pt")
                )
            )

            if not isinstance(around, torch.Tensor):
                around = around.pooler_output

            around = torch.nn.functional.normalize(around.float(), dim=-1)
            context = (around @ words)[0].softmax(-1).cpu().numpy()

            if context[len(labels) :].max() > max(0.5, context[: len(labels)].max()):
                return {
                    "empty": True,
                    "label": PICK_BACKGROUND[int(np.argmax(context[len(labels) :]))],
                }

    best = int(np.argmax(probs[: len(labels)]))

    if probs[len(labels) :].max() > probs[best]:
        return {"empty": True, "label": PICK_BACKGROUND[int(np.argmax(probs[len(labels) :]))]}

    buf = io.BytesIO()
    cut.save(buf, format="PNG")

    return {
        "empty": False,
        "label": canonical(labels[best]) or labels[best],
        "confident": bool(probs[best] >= 0.3),
        "box": box,
        "polygon": polygon(mask),
        "embedding": vector[0].cpu().numpy().tolist(),
        "png": buf.getvalue(),
        "size": [width, height],
    }
