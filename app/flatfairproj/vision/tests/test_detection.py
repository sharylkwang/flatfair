import io
from pathlib import Path
from tempfile import TemporaryDirectory
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.template import Context, Template
from django.test import override_settings, SimpleTestCase, TestCase
from django.urls import reverse
import numpy as np
from PIL import Image

from pretrained import pipelineIdentity
from storefront.models import ListingPhoto, Profile
from vision import brands, pipeline
from vision.brands import choose, match_line, with_brand
from vision.models import DetectedItem, ItemPick, PhotoAnalysis
from vision.services import item_words, process, find_words, runPick
from vision.tests.helpers import listing, makeListing


def line(text, box=(40, 20, 80, 30), confidence=0.98, lowest=0.9):
    return {"text": text, "box": list(box), "confidence": confidence, "lowest": lowest}


def output(attributes):
    buf = io.BytesIO()
    Image.new("RGBA", (10, 10)).save(buf, "PNG")
    return {
        "items": [
            dict(
                label="air conditioner",
                score=0.8,
                box=[0, 0, 10, 10],
                polygon=[[0, 0], [10, 0], [10, 10]],
                attributes=attributes,
                placement="back-left",
                embedding=[1.0] + [0.0] * 511,
                png=buf.getvalue(),
            )
        ],
        "flooring": {},
        "layout": "",
        "timings": {"total_seconds": 1},
        "size": [10, 10],
    }


class MatchTests(SimpleTestCase):

    def brand(self, text, label="air conditioner"):
        found = match_line(text, label)
        return found and found["brand"]

    def test_brand_matching_allows_case(self):
        self.assertEqual(self.brand("DAIKIN"), "Daikin")
        self.assertEqual(self.brand("Daikin"), "Daikin")
        self.assertEqual(self.brand("DAIKIN INVERTER"), "Daikin")
        self.assertEqual(self.brand("Samsung", "TV"), "Samsung")
        self.assertEqual(self.brand("FUJIOH", "range hood"), "Fujioh")
        self.assertEqual(self.brand("Rheem", "water heater"), "Rheem")

    def test_brand_matching_handles(self):
        self.assertEqual(self.brand("MITSUBISHI ELECTRIC"), "Mitsubishi Electric")
        self.assertEqual(self.brand("MITSUBISHIELECTRIC"), "Mitsubishi Electric")
        self.assertEqual(self.brand("MITSUBISHI HEAVY INDUSTRIES, LTD."), "Mitsubishi Heavy Industries")
        self.assertEqual(self.brand("MITSUBISHI"), "Mitsubishi")
        self.assertEqual(self.brand("Fisher & Paykel", "refrigerator"), "Fisher & Paykel")
        self.assertEqual(self.brand("FISHER AND PAYKEL", "refrigerator"), "Fisher & Paykel")

    def test_brand_matching_cleans_common(self):
        self.assertEqual(self.brand("DAlKIN"), "Daikin")
        self.assertEqual(self.brand("PANAS0NIC", "TV"), "Panasonic")
        self.assertEqual(self.brand("SAM5UNG", "TV"), "Samsung")
        self.assertEqual(match_line("DAlKIN", "air conditioner")["edits"], 0)

    def test_brand_matching_allows_limited(self):
        self.assertEqual(
            match_line("Electroluk", "oven"),
            {"brand": "Electrolux", "alias": "ELECTROLUX", "edits": 1, "short": False},
        )
        self.assertEqual(match_line("Panasonc", "air conditioner")["edits"], 1)
        self.assertIsNone(match_line("DAIKN", "air conditioner"))
        self.assertIsNone(match_line("Rhem", "water heater"))
        self.assertIsNone(match_line("Pansoniac", "air conditioner"))

    def test_brand_matches_limited(self):
        self.assertIsNone(match_line("DAIKIN", "sofa"))
        self.assertIsNone(match_line("SAMSUNG", "sofa"))
        self.assertIsNone(match_line("IKEA", "air conditioner"))
        self.assertEqual(self.brand("IKEA", "sofa"), "IKEA")
        self.assertIsNone(match_line("Sony", "refrigerator"))
        self.assertIsNone(match_line("DAIKIN", "toilet"))

    def test_short_brand_names_require(self):
        self.assertEqual(
            match_line("LG", "refrigerator"),
            {"brand": "LG", "alias": "LG", "edits": 0, "short": True},
        )
        self.assertEqual(self.brand("LG Life's Good", "TV"), "LG")
        self.assertEqual(self.brand("KDK", "ceiling fan"), "KDK")
        for text in ("LGX", "L6", "LG 42LB5610", "EFX"):
            self.assertIsNone(match_line(text, "TV"), text)
        self.assertIsNone(match_line("EF", "sofa"))

    def test_ambiguous_ocr_text_noise(self):
        for text in (
            "Dalkn",
            "DAIJIN",
            "SAMYUNG",
            "BOYCH",
            "Rinwai",
            "Panasonic Xtrnma Qwerty",
            "١٠٠",
            "-",
            "0.0",
            "",
            "CURT",
        ):
            self.assertIsNone(match_line(text, "air conditioner"), text)
            self.assertIsNone(match_line(text, "range hood"), text)

    def test_brand_choice_requires(self):
        self.assertEqual(choose([line("DAIKIN")], "air conditioner")[0], "Daikin")
        brand, _, rejected = choose([line("DAIKIN", confidence=0.6)], "air conditioner")
        self.assertIsNone(brand)
        self.assertEqual(rejected[0]["reason"], "low confidence")
        self.assertIsNone(choose([line("Electroluk", confidence=0.9)], "oven")[0])
        self.assertIsNone(choose([line("DAIKIN"), line("PANASONIC")], "air conditioner")[0])
        self.assertEqual(
            choose([line("MITSUBISHI"), line("MITSUBISHI ELECTRIC")], "air conditioner")[0],
            "Mitsubishi Electric",
        )
        self.assertEqual(choose([line("INVERTER"), line("DAIKIN")], "air conditioner")[0], "Daikin")

    def test_brand_attribute_inserted_first(self):
        self.assertEqual(
            list(with_brand({"colour": "white"}, "Daikin").items()),
            [("brand", "Daikin"), ("colour", "white")],
        )
        self.assertEqual(with_brand({"brand": "Daikin", "colour": "white"}, None), {"colour": "white"})


class DetectTests(SimpleTestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "photo.jpg"
        Image.new("RGB", (2000, 1000), "white").save(self.path)
        self.aircon = {
            "label": "air conditioner",
            "polygon": [[100, 50], [300, 50], [300, 150], [100, 150]],
            "box": [100, 50, 300, 150],
        }
        self.sofa = {
            "label": "sofa",
            "polygon": [[400, 250], [900, 250], [900, 480], [400, 480]],
            "box": [400, 250, 900, 480],
        }

    def detect(self, items, reads, clip=(True, 0.9)):
        with (
            patch.object(brands, "read_lines", side_effect=reads) as reader,
            patch.object(brands, "confirm_with_clip", return_value=clip) as checker,
        ):
            found, report = brands.detect(self.path, items, [1000, 500])
        return (found, report, reader, checker)

    def test_detected_brand_item_crop(self):
        found, report, reader, _ = self.detect(
            [self.aircon], [[line("DAIKIN", (150, 60, 250, 80))], [line("DAIKIN")]]
        )
        self.assertEqual(found, ["Daikin"])
        crop = reader.call_args_list[0].args[0]
        self.assertEqual(crop.size, (464, 264))
        self.assertEqual(report["found"][0]["brand"], "Daikin")
        self.assertEqual(report["reads"], 2)

    def test_brand_text_outside_item(self):
        found, _, _, _ = self.detect([self.aircon], [[line("DAIKIN", (0, 0, 20, 10))]])
        self.assertEqual(found, [None])

    def test_brand_second_read_must(self):
        found, report, _, _ = self.detect(
            [self.aircon],
            [[line("MITSUBISHU ELECTRIC", (100, 60, 300, 80))], [line("MITSUBISHU ELECTRIC")]],
        )
        self.assertEqual(found, [None])
        self.assertIn("second read", report["rejected"][0]["reason"])
        found, _, _, _ = self.detect(
            [self.aircon],
            [[line("MITSUBISHU ELECTRIC", (100, 60, 300, 80))], [line("MITSUBISHI ELECTRIC")]],
        )
        self.assertEqual(found, ["Mitsubishi Electric"])
        found, _, _, _ = self.detect([self.aircon], [[line("DAIKIN", (150, 60, 250, 80))], []])
        self.assertEqual(found, [None])

    def test_short_brand_detection_requires(self):

        def reads():
            return [[line("LG", (180, 60, 220, 80))], [line("LG")]]

        found, report, _, checker = self.detect([self.aircon], reads(), clip=(False, 0.2))
        self.assertEqual(found, [None])
        self.assertEqual(report["rejected"][-1]["reason"], "CLIP disagrees")
        found, _, _, checker = self.detect([self.aircon], reads(), clip=(True, 0.7))
        self.assertEqual(found, ["LG"])
        self.assertEqual(checker.call_args.args[2], "LG")

    def test_clip_brand_confirmation(self):
        import torch

        class Model:
            logit_scale = torch.tensor(0.0)

            def get_image_features(self, **inputs):
                return torch.tensor([[1.0, 0.0]])

        def check(prompts):
            with (
                patch("vision.pipeline.load", return_value=(lambda **kwargs: {}, Model())),
                patch("vision.pipeline.embed_text", return_value=torch.tensor(prompts)),
            ):
                return brands.confirm_with_clip(Image.new("RGB", (8, 8)), "TV", "LG", ["Samsung"])

        agreed, probability = check([[5.0, 0], [0, 0], [0, 0]])
        self.assertTrue(agreed)
        self.assertGreater(probability, 0.9)
        self.assertFalse(check([[0.2, 0], [0.1, 0], [0, 0]])[0])
        self.assertFalse(check([[0, 0], [5.0, 0], [0, 0]])[0])

    def test_sofa_brand_whole_photo(self):
        found, report, reader, _ = self.detect(
            [self.sofa], [[line("IKEA", (1200, 700, 1300, 740))], [line("IKEA")]]
        )
        self.assertEqual(found, ["IKEA"])
        self.assertEqual(reader.call_args_list[0].args[0].size, (2000, 1000))
        self.assertTrue(report["found"][0]["whole_photo"])

    def test_brand_text_on_nearby(self):
        painting = {"label": "painting", "polygon": [], "box": [550, 260, 650, 320]}
        found, report, reader, _ = self.detect(
            [self.sofa, painting], [[line("IKEA", (1150, 540, 1250, 580))]]
        )
        self.assertEqual(found, [None, None])
        self.assertEqual(reader.call_count, 1)

    def test_unsupported_item_label_skips(self):
        found, report, reader, _ = self.detect(
            [{"label": "toilet", "polygon": [], "box": [0, 0, 10, 10]}], []
        )
        self.assertEqual(found, [None])
        reader.assert_not_called()
        self.assertEqual(report["reads"], 0)
        self.assertEqual(report["version"], brands.VERSION)


class DisplayTests(TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=self.temp.name, PUBLIC_URL="")
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.temp.cleanup)
        self.flat = listing()
        self.photo = ListingPhoto.objects.create(listing=self.flat, room="master_bedroom", image="test.jpg")

    def test_brand_visual_attributes_form(self):
        item = DetectedItem(
            label="air conditioner", score=1, attributes={"colour": "white", "brand": "Daikin"}
        )
        self.assertEqual(item_words(item), ["Daikin", "white"])
        self.assertEqual(find_words(item), "Daikin white air conditioner")
        item.attributes = {"colour": "grey", "material": "fabric"}
        self.assertEqual(find_words(item), "grey fabric air conditioner")

    def test_saved_brand_appears_in(self):
        with (
            patch(
                "vision.services.analyse",
                return_value=output({"brand": "Daikin", "colour": "white"}),
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            process(self.photo)
        with patch("vision.templatetags.vision_tags.weights_ok", return_value=True):
            html = Template("{% load vision_tags %}{% shop_look flat %}").render(Context({"flat": self.flat}))
        self.assertIn("<p>Daikin · white</p>", html)
        self.assertIn('data-lens-words="Daikin white air conditioner"', html)
        self.assertIn("q=Daikin+white+air+conditioner", html)

    @patch("pretrained.tagErrors", new=lambda: [])
    def test_analysis_adds_detected_brand(self):
        from vision.pipeline import analyse, _ANALYSIS_CACHE

        _ANALYSIS_CACHE.clear()
        self.addCleanup(_ANALYSIS_CACHE.clear)
        path = Path(self.temp.name) / "new.jpg"
        path.write_bytes(b"new photo")
        with (
            patch("vision.pipeline._analyse", return_value=output({"colour": "white"})),
            patch.object(brands, "ocr_available", return_value=True),
            patch.object(
                brands, "detect", return_value=(["Daikin"], {"version": brands.VERSION, "reads": 2})
            ) as detect,
        ):
            result = analyse(path, "master_bedroom")
        self.assertEqual(
            list(result["items"][0]["attributes"].items()),
            [("brand", "Daikin"), ("colour", "white")],
        )
        self.assertEqual(result["timings"]["brands"]["version"], brands.VERSION)
        self.assertEqual(detect.call_args.args[2], [10, 10])

    @patch("pretrained.tagErrors", new=lambda: [])
    def test_brand_detection_failure_missing(self):
        from vision.pipeline import analyse, _ANALYSIS_CACHE

        _ANALYSIS_CACHE.clear()
        self.addCleanup(_ANALYSIS_CACHE.clear)
        path = Path(self.temp.name) / "broken.jpg"
        path.write_bytes(b"broken photo")
        with (
            patch("vision.pipeline._analyse", return_value=output({"colour": "white"})),
            patch.object(brands, "ocr_available", return_value=True),
            patch.object(brands, "detect", side_effect=RuntimeError("out of memory")),
            self.assertLogs("vision.brands", "WARNING"),
        ):
            result = analyse(path, "master_bedroom")
        self.assertEqual(result["items"][0]["attributes"], {"colour": "white"})
        self.assertIn("out of memory", result["timings"]["brands"]["error"])
        with (
            patch("vision.pipeline._analyse", return_value=output({"colour": "white"})),
            patch.object(brands, "ocr_available", return_value=False),
            patch.object(brands, "detect") as detect,
        ):
            _ANALYSIS_CACHE.clear()
            analyse(path, "master_bedroom")
        detect.assert_not_called()


def png():
    buf = io.BytesIO()
    Image.new("RGBA", (10, 10), "red").save(buf, "PNG")
    return buf.getvalue()


def found(label="washing machine", confident=True):
    return {
        "empty": False,
        "label": label,
        "confident": confident,
        "box": [1, 1, 5, 5],
        "polygon": [[1, 1], [5, 1], [5, 5]],
        "embedding": [1.0] + [0.0] * 511,
        "png": png(),
        "size": [10, 10],
    }


class PickMaskTests(TestCase):

    def test_whole_object_under_tap(self):
        part = np.zeros((100, 100), bool)
        part[40:50, 40:50] = True
        whole = np.zeros((100, 100), bool)
        whole[30:70, 30:70] = True
        elsewhere = np.zeros((100, 100), bool)
        elsewhere[0:20, 0:20] = True
        chosen = pipeline.best_mask([(part, 0.95), (whole, 0.93), (elsewhere, 0.99)], (45, 45), (100, 100))
        self.assertIs(chosen, whole)

    def test_tiny_huge_rejected(self):
        tiny = np.zeros((100, 100), bool)
        tiny[45, 45] = True
        huge = np.ones((100, 100), bool)
        self.assertIsNone(pipeline.best_mask([(tiny, 0.9), (huge, 0.9)], (45, 45), (100, 100)))

    def test_part_under_tap_kept(self):
        mask = np.zeros((20, 20), bool)
        mask[2:5, 2:5] = True
        mask[12:18, 12:18] = True
        kept = pipeline.part_at(mask, 14, 14)
        self.assertTrue(kept[14, 14])
        self.assertFalse(kept[3, 3])

    def test_big_area_touching_two(self):
        ceiling = np.zeros((100, 100), bool)
        ceiling[0:30, 0:100] = True
        sofa = np.zeros((100, 100), bool)
        sofa[50:90, 20:80] = True
        self.assertTrue(pipeline.is_room(ceiling))
        self.assertFalse(pipeline.is_room(sofa))

    def test_bathroom_words_stay_out(self):
        self.assertNotIn("toilet", pipeline.pick_names("living_room"))
        self.assertIn("washing machine", pipeline.pick_names("kitchen"))
        self.assertIn("toilet", pipeline.pick_names("common_toilet"))
        self.assertNotIn("oven", pipeline.pick_names("common_toilet"))


class PickViewTests(TestCase):

    def setUp(self):
        self.media = TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        setting = override_settings(MEDIA_ROOT=self.media.name)
        setting.enable()
        self.addCleanup(setting.disable)
        seller = User.objects.create_user("pick-seller@test.com")
        Profile.objects.create(user=seller, role="seller")
        self.flat = makeListing(seller, "live")
        photo = ListingPhoto.objects.create(listing=self.flat, room="kitchen", image="test.jpg")
        self.analysis = PhotoAnalysis.objects.create(
            photo=photo, status="done", pipelineIdentity=pipelineIdentity()
        )
        self.url = reverse("vision_pick", args=[self.analysis.pk])
        on = patch("vision.views.picking", return_value=True)
        on.start()
        self.addCleanup(on.stop)

    def test_tap_queues_visitor(self):
        with patch("vision.views.queue.wake") as wake:
            response = self.client.post(self.url, {"x": "0.5", "y": "0.6"})
        self.assertEqual(response.status_code, 202)
        pick = ItemPick.objects.get()
        self.assertEqual((pick.status, pick.x, pick.y), ("pending", 0.5, 0.6))
        self.assertEqual(pick.sessionKey, self.client.session.session_key)
        wake.assert_called_once()
        again = self.client.post(self.url, {"x": "0.51", "y": "0.6"})
        self.assertEqual(again.json()["id"], pick.pk)
        self.assertEqual(ItemPick.objects.count(), 1)

    def test_bad_taps_hidden_flats(self):
        self.assertEqual(self.client.post(self.url, {"x": "2", "y": "0.5"}).status_code, 400)
        self.assertEqual(self.client.post(self.url, {"x": "nan", "y": "0.5"}).status_code, 400)
        with patch("vision.views.picking", return_value=False):
            self.assertEqual(self.client.post(self.url, {"x": "0.5", "y": "0.5"}).status_code, 503)
        self.flat.status = "review"
        self.flat.save(update_fields=["status"])
        self.assertEqual(self.client.post(self.url, {"x": "0.5", "y": "0.5"}).status_code, 404)

    def test_finished_returns_card_visitor(self):
        with patch("vision.views.queue.wake"):
            data = self.client.post(self.url, {"x": "0.5", "y": "0.5"}).json()
        pick = ItemPick.objects.get()
        with (
            patch("vision.services.pick_item", return_value=found()),
            patch("vision.services.ai_lock"),
        ):
            runPick(pick)
        result = self.client.get(data["url"]).json()
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["label"], "Washing machine")
        self.assertIn("tbm=isch", result["search"])
        self.assertEqual(self.client.get(result["crop"]).status_code, 200)
        other = self.client_class()
        self.assertEqual(other.get(data["url"]).status_code, 404)
        self.assertEqual(other.get(result["crop"]).status_code, 404)

    def test_tap_on_wall_finds(self):
        with patch("vision.views.queue.wake"):
            first = self.client.post(self.url, {"x": "0.1", "y": "0.1"}).json()
            second = self.client.post(self.url, {"x": "0.8", "y": "0.8"}).json()
        wall, unsure = ItemPick.objects.order_by("pk")
        with (
            patch("vision.services.pick_item", return_value={"empty": True, "label": "wall"}),
            patch("vision.services.ai_lock"),
        ):
            runPick(wall)
        with (
            patch("vision.services.pick_item", return_value=found("stool", confident=False)),
            patch("vision.services.ai_lock"),
        ):
            runPick(unsure)
        self.assertIn(
            "Try tapping the middle of a piece of furniture",
            self.client.get(first["url"]).json()["message"],
        )
        self.assertEqual(self.client.get(second["url"]).json()["label"], "Item you picked")

    def test_panel_offers_tapping_available(self):
        from vision.templatetags.vision_tags import shop_look

        with patch("vision.templatetags.vision_tags.weights_ok", return_value=True):
            context = shop_look({"csrf_token": "token"}, self.flat)
        self.assertTrue(context["picking"])
        from django.template.loader import render_to_string

        html = render_to_string("vision/panel_content.html", context)
        self.assertIn(f'data-pick-url="{self.url}"', html)
        self.assertIn("Tap anything in the photo to look it up.", html)

    def test_listing_page_fold_offers(self):
        with patch("vision.templatetags.vision_tags.weights_ok", return_value=True):
            page = self.client.get(reverse("listing_detail", args=[self.flat.publicId])).content.decode()
        self.assertIn(f'data-pick-url="{self.url}"', page)
        self.assertIn('name="csrfmiddlewaretoken"', page.split('id="shop-the-look"', 1)[1])
