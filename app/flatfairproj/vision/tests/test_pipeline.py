from contextlib import contextmanager
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import tempfile
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import transaction
from django.template import Context, Template
from django.test import override_settings, TestCase
from django.urls import reverse
from PIL import Image

from pretrained import pipelineIdentity
from storefront.models import Listing, ListingPhoto, Profile
from vision.models import DetectedItem, PhotoAnalysis
from vision.pipeline import polygon, weights_ok
from vision.services import links, process, like_pool, like_items
from vision.tests.helpers import listing, makeListing, output


class PipelineTests(TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=self.temp.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.temp.cleanup)
        self.flat = listing()
        self.photo = ListingPhoto.objects.create(listing=self.flat, room="living", image="test.jpg")

    @patch("vision.services.analyse", return_value=output())
    def test_processing_saves_item_geometry(self, mocked):
        with self.captureOnCommitCallbacks(execute=True):
            analysis = process(self.photo)
        self.assertEqual(analysis.status, "done")
        item = analysis.items.get()
        self.assertEqual(len(item.embedding), 512)
        self.assertTrue(all((len(point) == 2 for point in item.polygon)))
        self.assertEqual(item.attributes["colour"], "grey")
        self.assertTrue(Path(item.crop.path).exists())
        mocked.assert_called_once_with(self.photo.image.path, room=self.photo.room)
        process(self.photo)
        self.assertEqual(mocked.call_count, 1)

    @override_settings(PUBLIC_URL="")
    @patch("vision.services.analyse", return_value=output())
    def test_item_links_panel_offer(self, mocked):
        with self.captureOnCommitCallbacks(execute=True):
            record = process(self.photo)
        item = record.items.get()
        search, lens = links(item)
        self.assertIn("q=grey+fabric+sofa", search)
        self.assertFalse(lens)
        with patch("vision.templatetags.vision_tags.weights_ok", return_value=True):
            html = Template("{% load vision_tags %}{% shop_look flat %}").render(Context({"flat": self.flat}))
        self.assertIn("tbm=isch&amp;q=grey+fabric+sofa", html)
        self.assertNotIn("needs a public image URL", html)
        self.assertNotIn("disabled", html)
        self.assertNotIn('<details class="panel vision-panel" open', html)
        self.assertIn('role="tablist"', html)
        self.assertIn("Search with Google Lens", html)
        self.assertIn(f'data-lens-crop="{reverse('vision_crop', args=[item.pk])}"', html)
        self.assertNotIn(item.crop.url, html)
        self.assertIn('data-lens-words="grey fabric sofa"', html)
        self.assertNotIn("Find similar online", html)
        self.assertNotIn("lens.google.com/uploadbyurl", html)
        with override_settings(PUBLIC_URL="https://flatfair.example.com"):
            self.assertIn("https%3A%2F%2Fflatfair.example.com", links(item)[1])
        with override_settings(PUBLIC_URL="https://192.168.1.1"):
            self.assertFalse(links(item)[1])

    @patch("vision.services.analyse", return_value=output())
    def test_similar_items_exclude_photos(self, mocked):
        with self.captureOnCommitCallbacks(execute=True):
            record = process(self.photo)
        item = record.items.get()
        another = ListingPhoto.objects.create(listing=self.flat, room="kitchen", image="a.jpg")
        with self.captureOnCommitCallbacks(execute=True):
            process(another)
        public_flat = listing()
        public_flat.seller = User.objects.create_user("other-seller")
        public_flat.save(update_fields=["seller"])
        other = ListingPhoto.objects.create(listing=public_flat, room="living", image="b.jpg")
        with self.captureOnCommitCallbacks(execute=True):
            other_record = process(other)
        expected = other_record.items.get()
        self.assertEqual([x.pk for x in like_items(item)], [expected.pk])

    def test_lens_public_url_empty(self):
        item = DetectedItem(pk=123, label="sofa", attributes={"colour": "grey"})
        with override_settings(PUBLIC_URL="https://flatfair.example.com/"):
            search, lens = links(item)
        self.assertEqual(
            parse_qs(urlparse(lens).query)["url"],
            ["https://flatfair.example.com" + reverse("vision_crop", args=[item.pk])],
        )
        with override_settings(PUBLIC_URL=""):
            fallbackSearch, fallbackLens = links(item)
        self.assertEqual(fallbackSearch, search)
        self.assertEqual(parse_qs(urlparse(fallbackSearch).query)["q"], ["grey sofa"])
        self.assertEqual(fallbackLens, "")

    def test_completed_empty_panel_shows(self):
        from django.template.loader import render_to_string

        html = render_to_string("vision/panel.html", {"enabled": True, "total": 0})
        self.assertIn('data-vision-state="complete"', html)
        self.assertIn("We didn’t spot any furniture in these photos.", html)
        self.assertNotIn("data-vision-pending", html)

    def test_missing_weights_hide_shop(self):
        with patch("vision.templatetags.vision_tags.weights_ok", return_value=False):
            html = Template("{% load vision_tags %}{% shop_look flat %}").render(Context({"flat": self.flat}))
        self.assertNotIn("Shop this look", html)

    def test_publishing_listing_enqueues(self):
        self.flat.status = "review"
        self.flat.save()
        PhotoAnalysis.objects.filter(photo=self.photo).delete()
        with self.captureOnCommitCallbacks(execute=True):
            self.flat.status = "live"
            self.flat.save()
        self.assertEqual(PhotoAnalysis.objects.get(photo=self.photo).status, "pending")

    def test_enqueue_listing_idempotent(self):
        from vision.queue import queue_flat

        done = PhotoAnalysis.objects.get_or_create(photo=self.photo)[0]
        PhotoAnalysis.objects.filter(pk=done.pk).update(status="done", pipelineIdentity=pipelineIdentity())
        second = ListingPhoto.objects.create(listing=self.flat, room="kitchen", image="kitchen.jpg")
        PhotoAnalysis.objects.filter(photo=second).delete()
        queue_flat(self.flat.pk)
        queue_flat(self.flat.pk)
        self.assertEqual(PhotoAnalysis.objects.get(pk=done.pk).status, "done")
        self.assertEqual(PhotoAnalysis.objects.get(photo=second).status, "pending")
        self.assertEqual(PhotoAnalysis.objects.count(), 2)
        hidden = listing()
        Listing.objects.filter(pk=hidden.pk).update(status="review")
        ListingPhoto.objects.create(listing=hidden, room="living", image="h.jpg")
        queue_flat(hidden.pk)
        self.assertFalse(PhotoAnalysis.objects.filter(photo__listing=hidden).exists())

    @patch("vision.services.analyse", return_value=output())
    def test_removed_crop_row_marks(self, mocked):
        from django.core.files.storage import default_storage

        save = default_storage.save

        def removing(name, content, *args, **kwargs):
            DetectedItem.objects.all().delete()
            return save(name, content, *args, **kwargs)

        with (
            patch.object(default_storage, "save", side_effect=removing),
            self.captureOnCommitCallbacks(execute=True),
        ):
            record = process(self.photo)
        record.refresh_from_db()
        self.assertEqual(record.status, "failed")
        self.assertIn("Crop row removed during publication", record.error)
        self.assertEqual(list(Path(self.temp.name).rglob("*.png")), [])

    def test_polygon_coordinates_integers(self):
        import numpy as np

        points = polygon(np.ones((20, 30), dtype=bool))
        self.assertTrue(all((isinstance(v, int) for p in points for v in p)))
        self.assertEqual(polygon(np.zeros((10, 10), dtype=bool)), [])

    @patch("huggingface_hub.snapshot_download")
    def test_model_download_skips_complete(self, download):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("vision.management.commands.download_vision_models.ML", Path(directory)),
        ):
            call_command("download_vision_models", stdout=io.StringIO())
            self.assertEqual(download.call_count, 4)
            for name in ("vision/grounding_dino", "vision/slimsam", "vision/florence2", "depth"):
                dest = Path(directory) / name
                dest.mkdir(parents=True)
                for f in ("config.json", "model.safetensors", "preprocessor_config.json"):
                    (dest / f).touch()
            call_command("download_vision_models", stdout=io.StringIO())
            self.assertEqual(download.call_count, 4)

    def test_analysis_exception_records(self):
        with patch("vision.services.analyse", side_effect=RuntimeError("missing model")):
            record = process(self.photo)
        self.assertEqual(record.status, "failed")
        self.assertIn("missing model", record.error)

    @patch("vision.services.analyse", return_value=output())
    def test_pending_panel_becomes(self, mocked):
        template = Template("{% load vision_tags %}{% shop_look flat %}")
        with patch("vision.templatetags.vision_tags.weights_ok", return_value=True):
            pending = template.render(Context({"flat": self.flat}))
            with self.captureOnCommitCallbacks(execute=True):
                process(self.photo)
            done = template.render(Context({"flat": self.flat}))
        self.assertIn("data-vision-pending", pending)
        self.assertNotIn('role="tablist"', pending)
        self.assertIn('class="vision-card" tabindex="-1"', done)
        self.assertIn('Search with Google Lens<span class="visually-hidden"> (opens in new tab)</span>', done)


class RealTests(TestCase):

    def test_models_extract_items_embeddings(self):
        if os.environ.get("FLATFAIR_REAL_MODELS") != "1":
            self.skipTest("Loads the real models (GBs of memory): run with FLATFAIR_REAL_MODELS=1")
        if not weights_ok():
            self.skipTest("local vision weights missing")
        from django.conf import settings
        from storefront.interiors import PHOTO_DIR

        paths = sorted((Path(settings.MEDIA_ROOT) / "demo").glob("*/*living*")) or sorted(
            PHOTO_DIR.glob("*living*")
        )
        if not paths:
            paths = list((Path(settings.MEDIA_ROOT) / "listings").glob("*/*"))
        paths = [p for p in paths if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
        if not paths:
            self.skipTest("demo photos missing")
        furnished = [p for p in paths if p.stem in ("living-07", "living-12", "living-22", "living-24")]
        paths = furnished or paths
        from vision.pipeline import analyse

        result = analyse(paths[0])
        self.assertIn("total_seconds", result["timings"])
        self.assertLessEqual(len(result["items"]), 12)
        self.assertGreater(len(result["items"]), 0)
        self.assertIsInstance(result["flooring"], dict)
        from vision.brands import ocr_available, VERSION

        if ocr_available():
            self.assertEqual(result["timings"]["brands"].get("version"), VERSION)
        for item in result["items"]:
            self.assertEqual(len(item["embedding"]), 512)
            self.assertTrue(item["polygon"])
            self.assertEqual(Image.open(io.BytesIO(item["png"])).mode, "RGBA")


class StaleTests(TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=self.temp.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.temp.cleanup)
        self.flat = listing()
        self.flat.status = "live"
        self.flat.save()
        Path(self.temp.name, "room.jpg").write_bytes(b"first photo")
        self.photo = ListingPhoto.objects.create(listing=self.flat, room="living", image="room.jpg")

    def test_changed_photo_hash_requeues(self):
        from vision.services import is_stale

        with patch("vision.services.analyse", return_value=output()) as mocked:
            with self.captureOnCommitCallbacks(execute=True):
                record = process(self.photo)
            self.assertTrue(record.timings["source_sha256"])
            self.assertFalse(is_stale(record))
            process(self.photo)
            self.assertEqual(mocked.call_count, 1)
            Path(self.temp.name, "room.jpg").write_bytes(b"a different photo")
            record.refresh_from_db()
            self.assertTrue(is_stale(record))
            with patch("vision.templatetags.vision_tags.weights_ok", return_value=True):
                html = Template("{% load vision_tags %}{% shop_look flat %}").render(
                    Context({"flat": self.flat})
                )
            self.assertIn('data-vision-state="pending"', html)
            self.assertNotIn("<polygon", html)
            record.refresh_from_db()
            self.assertEqual(record.status, "pending")
            with (
                patch(
                    "vision.management.commands.analyse_listing_photos.weights_ok",
                    return_value=True,
                ),
                self.captureOnCommitCallbacks(execute=True),
            ):
                call_command("analyse_listing_photos", stdout=io.StringIO())
            self.assertEqual(mocked.call_count, 2)
            record.refresh_from_db()
            self.assertFalse(is_stale(record))

    def test_current_identity_missing_source(self):
        from vision.services import is_stale

        record = PhotoAnalysis.objects.create(
            photo=self.photo,
            status="done",
            pipelineIdentity=pipelineIdentity(),
            timings={"size": [10, 10]},
        )
        self.assertFalse(is_stale(record))

    @patch("vision.services.analyse", return_value=output())
    def test_completed_panel_shows_flooring(self, mocked):
        with self.captureOnCommitCallbacks(execute=True):
            process(self.photo)
        with patch("vision.templatetags.vision_tags.weights_ok", return_value=True):
            html = Template("{% load vision_tags %}{% shop_look flat %}").render(Context({"flat": self.flat}))
        self.assertIn("Floor: Parquet", html)
        self.assertIn("It isn’t part of the sale.", html)
        self.assertNotIn("Similar in other flats (0)", html)
        self.assertIn("<polygon", html)


class ReanalysisTests(TestCase):

    def setUp(self):
        self.media = TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        setting = override_settings(MEDIA_ROOT=self.media.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.seller = User.objects.create_user("asdf@test.com", password="Zxcv-9876!")
        Profile.objects.create(user=self.seller, role="seller")
        self.flat = self.make_flat()

    def make_flat(self, **values):
        data = dict(
            seller=self.seller,
            town="SENGKANG",
            block="123",
            postalCode="540123",
            flatType="4 ROOM",
            flatModel="Model A",
            floorAreaSqm=93,
            storey=8,
            leaseCommenceYear=1995,
            bedrooms=3,
            bathrooms=2,
            hasBalcony=False,
            askingPrice=600000,
            estimatedPrice=600000,
            bandLow=500000,
            bandHigh=700000,
        )
        return Listing.objects.create(**dict(data, **values))

    @patch("vision.services.analyse", return_value=output())
    def test_prefetched_candidates_support(self, analysis):
        photo = ListingPhoto.objects.create(listing=self.flat, room="living_room", image="test.jpg")
        with self.captureOnCommitCallbacks(execute=True):
            process(photo)
        other = self.make_flat(seller=User.objects.create_user("other@test.com"))
        other_photo = ListingPhoto.objects.create(listing=other, room="living_room", image="other.jpg")
        with self.captureOnCommitCallbacks(execute=True):
            record = process(other_photo)
        item = record.items.get()
        with self.assertNumQueries(1):
            candidates = like_pool(other.pk)
        with self.assertNumQueries(0):
            for _ in range(3):
                self.assertEqual(len(like_items(item, candidates)), 1)

    @patch("vision.services.analyse", return_value=output())
    def test_forced_replaces_previous_crop(self, analysis):
        photo = ListingPhoto.objects.create(listing=self.flat, room="living_room", image="test.jpg")
        record = process(photo)
        old = record.items.get().crop.path
        with self.captureOnCommitCallbacks(execute=True):
            record = process(photo, force=True)
        self.assertFalse(Path(old).exists())
        self.assertTrue(Path(record.items.get().crop.path).exists())

    @patch("vision.services.analyse", return_value=output())
    def test_failed_keeps_previous_items(self, analysis):
        photo = ListingPhoto.objects.create(listing=self.flat, room="living_room", image="test.jpg")
        record = process(photo)
        before = set(Path(self.media.name).rglob("*.png"))
        with patch.object(DetectedItem, "save", side_effect=RuntimeError("save failed")):
            result = process(photo, force=True)
        self.assertEqual(result.status, "failed")
        self.assertEqual(set(Path(self.media.name).rglob("*.png")), before)
        self.assertEqual(record.items.count(), 1)

    @patch("vision.services.analyse", return_value=output())
    def test_initial_forced_analysis_run(self, analysis):
        held = []

        @contextmanager
        def lock():
            held.append(True)
            try:
                yield
            finally:
                held.pop()

        def infer(*args, **kwargs):
            self.assertEqual(held, [True])
            return output()

        photo = ListingPhoto.objects.create(listing=self.flat, room="living_room", image="test.jpg")
        with (
            patch("vision.services.ai_lock", side_effect=lock),
            patch("vision.services.analyse", side_effect=infer),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                result = process(photo)
            self.assertEqual(result.status, "done")
            with self.captureOnCommitCallbacks(execute=True):
                result = process(photo, force=True)
            self.assertEqual(result.status, "done")


class CommitTests(TestCase):

    def setUp(self):
        self.seller = User.objects.create_user("asdf")
        Profile.objects.create(user=self.seller, role="seller")
        self.flat = makeListing(self.seller, "live")
        self.media = TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        setting = override_settings(MEDIA_ROOT=self.media.name)
        setting.enable()
        self.addCleanup(setting.disable)

    @patch("vision.services.analyse", return_value=output())
    def test_rolled_back_analysis_creation(self, analysis):
        with transaction.atomic():
            photo = ListingPhoto.objects.create(listing=self.flat, room="living", image="test.jpg")
            record = process(photo)
            crop = Path(record.items.get().crop.path)
            self.assertFalse(crop.exists())
            transaction.set_rollback(True)
        self.assertFalse(crop.exists())
        self.assertFalse(PhotoAnalysis.objects.filter(pk=record.pk).exists())

    @patch("vision.services.analyse", return_value=output())
    def test_forced_reanalysis_deletes(self, analysis):
        photo = ListingPhoto.objects.create(listing=self.flat, room="living", image="test.jpg")
        with self.captureOnCommitCallbacks(execute=True):
            record = process(photo)
        old = Path(record.items.get().crop.path)
        self.assertTrue(old.exists())
        with self.captureOnCommitCallbacks(execute=True):
            record = process(photo, force=True)
            self.assertTrue(old.exists())
        self.assertFalse(old.exists())
        self.assertTrue(Path(record.items.get().crop.path).exists())

    @patch("vision.services.analyse", return_value=output())
    def test_partial_storage_failure_cleans(self, analysis):
        photo = ListingPhoto.objects.create(listing=self.flat, room="living", image="test.jpg")
        from django.core.files.storage import default_storage

        save = default_storage.save

        def partial(name, content, *args, **kwargs):
            save(name, content, *args, **kwargs)
            raise OSError("Storage failed after creating the file")

        with (
            patch.object(default_storage, "save", side_effect=partial),
            self.captureOnCommitCallbacks(execute=True),
        ):
            record = process(photo)
        record.refresh_from_db()
        self.assertEqual(record.status, "failed")
        self.assertEqual(list(Path(self.media.name).rglob("*.png")), [])


class QualityTests(TestCase):

    def test_room_thresholds_penalize(self):
        from vision.pipeline import room_cutoff

        self.assertGreater(room_cutoff("kitchen cabinet", "master_bedroom"), 0.8)
        self.assertGreater(room_cutoff("bed", "kitchen"), 0.8)
        self.assertGreater(room_cutoff("sofa", "common_toilet"), 0.8)
        self.assertLess(room_cutoff("bed", "master_bedroom"), 0.6)

    def test_non_maximum_suppression_removes(self):
        from vision.pipeline import nms

        boxes = [[0, 0, 100, 100], [5, 5, 105, 105], [60, 0, 160, 100], [200, 0, 300, 100]]
        self.assertEqual(nms(boxes, [0.9, 0.8, 0.7, 0.6], labels=["painting"] * 4), [0, 2, 3])
        self.assertEqual(nms([[0, 0, 100, 100], [20, 20, 80, 80]], [0.9, 0.8], labels=["mirror"] * 2), [0])
        self.assertEqual(nms(boxes[:2], [0.9, 0.8], labels=["painting", "mirror"]), [0])

    def test_usable_mask_requires_enough(self):
        import numpy as np
        from vision.pipeline import mask_ok

        mask = np.zeros((100, 100), dtype=bool)
        mask[:5, :5] = True
        self.assertFalse(mask_ok(mask, [0, 0, 10, 10]))
        mask[:20, :20] = True
        self.assertFalse(mask_ok(mask, [0, 0, 100, 100]))
        self.assertTrue(mask_ok(mask, [0, 0, 20, 20]))

    def test_clip_verification_relabels(self):
        from vision.pipeline import sure_label, MATERIALS, STYLE_LABELS

        self.assertEqual(
            sure_label("floor lamp", ["floor lamp", "table lamp", "pendant light"], [0.1, 0.8, 0.1]),
            "table lamp",
        )
        self.assertEqual(sure_label("painting", ["painting", "window", "mirror"], [0.05, 0.9, 0.05]), "")
        self.assertEqual(
            sure_label("painting", ["painting", "window", "mirror"], [0.5, 0.3, 0.2]), "painting"
        )
        self.assertEqual(sure_label("painting", ["painting", "window", "mirror"], [0.03, 0.5, 0.47]), "")
        from vision.pipeline import mixups

        self.assertIn("coffee table", mixups("side table"))
        self.assertIn("desk", mixups("bedside table"))
        self.assertNotIn("rattan", MATERIALS.get("painting", []))
        self.assertNotIn("painting", STYLE_LABELS)

    @patch("vision.queue.threading.Thread")
    def test_queue_start_creates_one(self, thread):
        from vision import queue

        with (
            patch.object(queue, "_started", False),
            patch.object(queue, "runtime_enabled", return_value=True),
            patch("vision.pipeline.weights_ok", return_value=True),
        ):
            queue.start()
            queue.start()
        self.assertEqual(thread.call_count, 1)
        thread.return_value.start.assert_called_once()

    @patch("vision.queue.threading.Thread")
    def test_queue_runtime_skips_reloader(self, thread):
        from vision import queue

        with (
            patch.dict(os.environ, {"RUN_MAIN": "false"}),
            patch("sys.argv", ["manage.py", "runserver"]),
        ):
            queue.start()
        thread.assert_not_called()
        with patch("sys.argv", ["manage.py", "migrate"]):
            self.assertFalse(queue.runtime_enabled())
        with patch("sys.argv", ["manage.py", "runserver", "--noreload"]):
            self.assertTrue(queue.runtime_enabled())

    def test_canonical_room_labels_bedroom(self):
        from vision.pipeline import canonical, room_cutoff

        for term, expected in [
            ("bedside lamp", "table lamp"),
            ("framed picture", "painting"),
            ("roller blind", "window blind"),
            ("window shade", "window blind"),
            ("throw pillow", "cushion"),
            ("bookshelf", "shelf"),
        ]:
            self.assertEqual(canonical(term), expected)
        self.assertLess(room_cutoff("table lamp", "master_bedroom"), 0.3)
        self.assertLess(room_cutoff("painting", "master_bedroom"), 0.3)
        self.assertGreater(room_cutoff("kitchen cabinet", "master_bedroom"), 0.8)

    def test_small_object_masks_label(self):
        import numpy as np
        from vision.pipeline import mask_ok

        mask = np.zeros((500, 500), dtype=bool)
        mask[10:30, 10:30] = True
        self.assertTrue(mask_ok(mask, [10, 10, 30, 30], "table lamp"))
        self.assertFalse(mask_ok(mask, [10, 10, 30, 30], "sofa"))
        self.assertFalse(mask_ok(mask, [0, 0, 100, 100], "table lamp"))
        mask[:2, :2] = True
        self.assertFalse(mask_ok(mask, [0, 0, 3, 3], "painting"))

    @patch("pretrained.tagErrors", new=lambda: [])
    def test_analysis_cache_image_content(self):
        from vision.pipeline import analyse, _ANALYSIS_CACHE

        _ANALYSIS_CACHE.clear()
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "a.jpg"
            second = Path(directory) / "b.jpg"
            first.write_bytes(b"identical image")
            second.write_bytes(b"identical image")
            with (
                patch("vision.pipeline._analyse", side_effect=lambda *args: output()) as inference,
                patch("vision.brands.ocr_available", return_value=False),
            ):
                self.assertFalse(analyse(first, "master_bedroom")["timings"]["cache_hit"])
                self.assertTrue(analyse(second, "master_bedroom")["timings"]["cache_hit"])
                self.assertFalse(analyse(second, "kitchen")["timings"]["cache_hit"])
                self.assertEqual(inference.call_count, 2)
        _ANALYSIS_CACHE.clear()

    def test_ambiguous_table_types_generic(self):
        from vision.pipeline import sure_label

        choices = [
            "dining table",
            "coffee table",
            "side table",
            "bedside table",
            "desk",
            "cabinet",
            "chair",
        ]
        self.assertEqual(
            sure_label("bedside table", choices, [0.16, 0.42, 0.08, 0.01, 0.28, 0.01, 0.04]),
            "coffee table",
        )
        self.assertEqual(sure_label("side table", choices, [0.3, 0.36, 0.1, 0.01, 0.15, 0.04, 0.04]), "table")
        self.assertEqual(
            sure_label("window blind", ["window blind", "curtains", "ceiling"], [0.1, 0.8, 0.1]), ""
        )

    def test_painting_mask_must_fill(self):
        import numpy as np
        from vision.pipeline import mask_ok

        mask = np.zeros((200, 200), dtype=bool)
        mask[10:60, 10:110] = True
        self.assertFalse(mask_ok(mask, [10, 10, 110, 110], "painting"))
        mask[10:110, 10:110] = True
        self.assertTrue(mask_ok(mask, [10, 10, 110, 110], "painting"))

    def test_blind_surface_allows_smooth(self):
        import numpy as np
        from vision.pipeline import blind_ok

        shade = Image.new("RGBA", (40, 40), (180, 175, 155, 255))
        self.assertTrue(blind_ok(shade))
        textured = np.zeros((40, 40, 4), dtype=np.uint8)
        textured[:, :, 3] = 255
        pattern = (np.indices((40, 40)).sum(axis=0) % 2 * 255).astype(np.uint8)
        textured[:, :, :3] = pattern[:, :, None]
        self.assertFalse(blind_ok(Image.fromarray(textured)))
        textured[:, :, :3] = (np.indices((40, 40))[0] % 2 * 255)[:, :, None]
        self.assertTrue(blind_ok(Image.fromarray(textured)))

    def test_bathroom_thresholds_reject(self):
        from vision.pipeline import room_cutoff

        self.assertGreaterEqual(room_cutoff("table lamp", "common_toilet"), 0.82)
        self.assertGreaterEqual(room_cutoff("cushion", "common_toilet"), 0.82)

    def test_contextual_table_labels_depend(self):
        from vision.pipeline import fit_label

        self.assertEqual(fit_label("bedside table", "living_room", 0.5), "table")
        self.assertEqual(fit_label("bedside table", "master_bedroom", 0.5), "bedside table")
        self.assertEqual(fit_label("dining table", "master_bedroom", 0.5), "table")
        self.assertEqual(fit_label("dining table", "master_bedroom", 0.9), "dining table")

    def test_kitchen_decor_checks_include(self):
        from vision.pipeline import mixups, room_cutoff

        choices = mixups("table lamp")
        for negative in ("kettle", "blender", "vase", "wall hook", "ceiling light"):
            self.assertIn(negative, choices)
        self.assertGreaterEqual(room_cutoff("table lamp", "kitchen"), 0.55)
        self.assertGreaterEqual(room_cutoff("cushion", "kitchen"), 0.55)
        self.assertLess(room_cutoff("table lamp", "master_bedroom"), 0.3)

    def test_plant_crop_requires_green(self):
        import numpy as np
        from vision.pipeline import plant_ok

        data = np.full((50, 30, 4), 255, dtype=np.uint8)
        data[:35, :, :3] = [30, 120, 25]
        data[35:, :, :3] = [130, 70, 30]
        self.assertTrue(plant_ok(Image.fromarray(data)))
        data[:, :, :3] = [30, 120, 25]
        self.assertFalse(plant_ok(Image.fromarray(data)))
        data[:, :, :3] = [240, 240, 240]
        self.assertFalse(plant_ok(Image.fromarray(data)))

    def test_canonical_vocabulary_chunks(self):
        from vision.pipeline import canonical, VOCABULARY, LABELS, NEGATIVE_LABELS, room_kind

        for term, expected in [
            ("gas stove", "cooktop"),
            ("induction hob", "cooktop"),
            ("cooker hood", "range hood"),
            ("pendant lamp", "pendant light"),
            ("chandelier", "pendant light"),
            ("bar stool", "stool"),
            ("wash basin", "sink"),
            ("bathroom vanity", "vanity cabinet"),
            ("rain shower", "shower head"),
            ("window", "window"),
            ("window blind", "window blind"),
            ("kitchen cabinet", "kitchen cabinet"),
        ]:
            self.assertEqual(canonical(term), expected)
        for chunks in VOCABULARY.values():
            for chunk in chunks:
                self.assertLessEqual(len(chunk), 15)
                for phrase in chunk:
                    self.assertIn(canonical(phrase), LABELS + NEGATIVE_LABELS)
        self.assertEqual(
            [room_kind(r) for r in ("kitchen", "common_toilet", "master_toilet", "bedroom_2", "living", "")],
            ["kitchen", "bathroom", "bathroom", "bedroom", "living", "living"],
        )

    def test_shape_rules_reject_implausible(self):
        from vision.pipeline import shape_ok

        size = (1000, 667)
        self.assertFalse(shape_ok("refrigerator", [0, 0, 40, 660], size))
        self.assertTrue(shape_ok("refrigerator", [100, 60, 300, 640], size))
        self.assertFalse(shape_ok("painting", [3, 2, 995, 663], size))
        self.assertTrue(shape_ok("painting", [400, 100, 520, 260], size))
        self.assertFalse(shape_ok("ceiling light", [400, 560, 480, 600], size))
        self.assertFalse(shape_ok("air conditioner", [838, 377, 883, 600], size))
        self.assertFalse(shape_ok("pendant light", [970, 0, 1000, 40], size))
        self.assertFalse(shape_ok("range hood", [200, 450, 400, 660], size))
        self.assertTrue(shape_ok("oven", [200, 450, 400, 660], size))

    def test_nested_detections_rejected_sink(self):
        from vision.pipeline import in_other, anchored

        mirror = {"label": "mirror", "box": [0, 0, 100, 100]}
        self.assertTrue(in_other("sink", [10, 10, 50, 50], [mirror]))
        self.assertTrue(
            in_other("painting", [20, 20, 40, 40], [{"label": "refrigerator", "box": [0, 0, 100, 300]}])
        )
        self.assertFalse(in_other("cushion", [10, 10, 50, 50], [{"label": "sofa", "box": [0, 0, 100, 100]}]))
        sink = {"label": "sink", "box": [40, 200, 160, 260]}
        vanity = {"label": "vanity cabinet", "box": [0, 220, 200, 400]}
        self.assertTrue(anchored(vanity, [vanity, sink]))
        self.assertFalse(anchored(vanity, [vanity]))
        glass = {"label": "mirror", "needs_sink": True, "box": [20, 0, 180, 210]}
        self.assertTrue(anchored(glass, [glass, sink]))
        self.assertFalse(anchored(dict(glass, box=[400, 0, 560, 210]), [glass, sink]))
        self.assertTrue(anchored({"label": "mirror", "box": [400, 0, 560, 210]}, []))

    def test_painting_verification_requires(self):
        from vision.pipeline import sure_label, mixups

        choices = mixups("painting")
        for confuser in ("window louvres", "kitchen cabinet door", "fridge magnet", "tiled wall"):
            self.assertIn(confuser, choices)
        probs = [0.0] * len(choices)
        probs[choices.index("painting")], probs[choices.index("window louvres")] = (0.45, 0.4)
        self.assertEqual(sure_label("painting", choices, probs), "")
        probs[choices.index("painting")], probs[choices.index("window louvres")] = (0.8, 0.05)
        self.assertEqual(sure_label("painting", choices, probs), "painting")
        seating = mixups("sofa")
        probs = [0.0] * len(seating)
        probs[seating.index("sofa")], probs[seating.index("chaise lounge")] = (0.51, 0.47)
        self.assertEqual(sure_label("sofa", seating, probs), "sofa")

    def test_room_thresholds_distinguish(self):
        from vision.pipeline import room_cutoff

        self.assertGreaterEqual(room_cutoff("oven", "master_bedroom"), 0.82)
        self.assertGreaterEqual(room_cutoff("air conditioner", "kitchen"), 0.82)
        self.assertGreaterEqual(room_cutoff("window blind", "common_toilet"), 0.82)
        self.assertLess(room_cutoff("cooktop", "kitchen"), 0.4)
        self.assertLess(room_cutoff("vanity cabinet", "common_toilet"), 0.4)
