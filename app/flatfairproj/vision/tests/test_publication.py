from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.storage import default_storage
from django.db import connection
from django.template import Context, Template
from django.test import override_settings, TestCase, TransactionTestCase, SimpleTestCase

import pretrained as identity
from storefront.models import ListingPhoto
from storefront.tests.helpers import makeListing, makeUser
from vision import queue
from vision.models import PhotoAnalysis
from vision.services import process, like_pool, is_stale, like_items
from vision.tests.helpers import listing, output


class TimingTests(TransactionTestCase):

    def setUp(self):
        self.media = TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        override = override_settings(MEDIA_ROOT=self.media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.flat = listing()
        self.flat.seller = User.objects.create_user("seller")
        self.flat.save(update_fields=["seller"])
        self.photo = ListingPhoto.objects.create(listing=self.flat, room="living", image="test.jpg")

    @patch("vision.services.analyse", return_value=output())
    @patch("vision.templatetags.vision_tags.weights_ok", return_value=True)
    def test_analysis_stays_unpublished(self, weights, analyse):
        save = default_storage.save
        observed = []

        def publishing(name, content, *args, **kwargs):
            self.assertTrue(connection.get_autocommit())
            reader = connection.copy(alias="publication_reader")
            try:
                with reader.cursor() as cursor:
                    cursor.execute(
                        "SELECT status FROM vision_photoanalysis WHERE photo_id = %s",
                        [self.photo.pk],
                    )
                    self.assertEqual(cursor.fetchone()[0], "running")
            finally:
                reader.close()
            record = PhotoAnalysis.objects.get(photo=self.photo)
            self.assertFalse(default_storage.exists(name))
            html = Template("{% load vision_tags %}{% shop_look flat %}").render(Context({"flat": self.flat}))
            self.assertIn('data-vision-state="pending"', html)
            self.assertNotIn(record.items.first().crop.url, html)
            self.assertEqual(like_pool(-1), {})
            observed.append(name)
            return save(name, content, *args, **kwargs)

        result = output()
        result["items"].append(dict(result["items"][0]))
        with (
            patch("vision.services.analyse", return_value=result),
            patch.object(default_storage, "save", side_effect=publishing),
        ):
            record = process(self.photo)
        record.refresh_from_db()
        self.assertEqual(record.status, "done")
        self.assertEqual(len(observed), 2)
        self.assertTrue(all((item.crop.storage.exists(item.crop.name) for item in record.items.all())))
        html = Template("{% load vision_tags %}{% shop_look flat %}").render(Context({"flat": self.flat}))
        self.assertIn('data-vision-state="complete"', html)
        self.assertTrue(like_pool(-1))


class GenerationTests(TestCase):

    def setUp(self):
        self.media = TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        override = override_settings(MEDIA_ROOT=self.media.name)
        override.enable()
        self.addCleanup(override.disable)
        self.photo = ListingPhoto.objects.create(listing=listing(), room="living", image="test.jpg")

    @patch("vision.services.analyse", return_value=output())
    def test_stale_publication_callback(self, analyse):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            process(self.photo)
            second = process(self.photo, force=True)
        self.assertEqual(len(callbacks), 3)
        callbacks[0]()
        second.refresh_from_db()
        self.assertEqual(second.status, "running")
        self.assertEqual(list(Path(self.media.name).rglob("*.png")), [])
        for callback in callbacks[1:]:
            callback()
        second.refresh_from_db()
        self.assertEqual(second.status, "done")
        self.assertTrue(second.items.get().crop.storage.exists(second.items.get().crop.name))

    @patch("vision.services.analyse", return_value=output())
    def test_old_storage_failure_keeps(self, analyse):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            record = process(self.photo)
        save = default_storage.save

        def replaced(name, content, *args, **kwargs):
            save(name, content, *args, **kwargs)
            PhotoAnalysis.objects.filter(pk=record.pk).update(timings={"publication_generation": "newer"})
            raise OSError("old generation storage failure")

        with patch.object(default_storage, "save", side_effect=replaced):
            callbacks[0]()
        record.refresh_from_db()
        self.assertEqual(record.status, "running")
        self.assertEqual(record.timings["publication_generation"], "newer")
        self.assertEqual(list(Path(self.media.name).rglob("*.png")), [])


class SetupTests(TestCase):

    def test_analysis_fixture_temporary(self):
        case = GenerationTests("test_stale_publication_callback")
        with override_settings(BASE_DIR=Path("/nonexistent/flatfairproj")):
            try:
                case.setUp()
                self.assertTrue(Path(case.media.name).exists())
            finally:
                case.doCleanups()


class PipelineIdentityTests(SimpleTestCase):

    def test_records_version_model_names(self):
        current = identity.pipelineIdentity()
        self.assertEqual(current["version"], identity.PIPELINE_VERSION)
        self.assertEqual(
            set(current["models"]),
            {
                "IDEA-Research/grounding-dino-tiny",
                "Zigeng/SlimSAM-uniform-77",
                "florence-community/Florence-2-base",
                "depth-anything/Depth-Anything-V2-Small-hf",
                "openai/clip-vit-base-patch32",
            },
        )
        for model in current["models"]:
            with self.subTest(model=model), patch.dict(identity.REVISIONS, {model: "new-revision"}):
                self.assertNotEqual(current, identity.pipelineIdentity())
        with patch.object(identity, "PIPELINE_VERSION", "new-pipeline"):
            self.assertNotEqual(current, identity.pipelineIdentity())


@patch("django.db.close_old_connections", new=lambda: None)
class VisionVersionTests(TestCase):

    def setUp(self):
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        setting = override_settings(MEDIA_ROOT=media.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.flat = makeListing(makeUser("seller@test.com", "seller"))
        self.photo = ListingPhoto.objects.create(listing=self.flat, room="kitchen", image="mock-photo.jpg")

    def analyse(self, photo):
        with patch("vision.services.analyse", return_value=output()) as analyse:
            with self.captureOnCommitCallbacks(execute=True):
                record = process(photo)
        return (record, analyse)

    def test_processing_records_identity(
        self,
    ):
        record, analyse = self.analyse(self.photo)
        self.assertEqual(record.pipelineIdentity, identity.pipelineIdentity())
        self.assertEqual(record.version, identity.PIPELINE_VERSION)
        self.assertEqual(record.status, "done")
        self.assertFalse(is_stale(record))
        with patch.object(identity, "PIPELINE_VERSION", "next"):
            self.assertTrue(is_stale(record))
            updated, analyse = self.analyse(self.photo)
            analyse.assert_called_once()
            self.assertEqual(updated.pipelineIdentity, identity.pipelineIdentity())
            self.assertFalse(is_stale(updated))

    def test_legacy_results_changed(
        self,
    ):
        record, _ = self.analyse(self.photo)
        changedRevision = identity.pipelineIdentity()
        changedRevision["models"]["openai/clip-vit-base-patch32"] = "previous-revision"
        for stored in [{}, {"version": "old", "models": {}}, changedRevision]:
            with self.subTest(stored=stored):
                PhotoAnalysis.objects.filter(pk=record.pk).update(status="done", pipelineIdentity=stored)
                with patch("vision.templatetags.vision_tags.weights_ok", return_value=True):
                    html = Template("{% load vision_tags %}{% shop_look flat %}").render(
                        Context({"flat": self.flat})
                    )
                self.assertIn('data-vision-state="pending"', html)
                self.assertNotIn("<polygon", html)
                self.assertNotIn('class="vision-card"', html)
                self.assertNotIn("Floor:", html)
                record.refresh_from_db()
                self.assertEqual(record.status, "pending")

    def test_similar_retrieval_excludes(
        self,
    ):
        source, _ = self.analyse(self.photo)
        otherFlat = makeListing(makeUser("other@test.com", "seller"))
        otherPhoto = ListingPhoto.objects.create(listing=otherFlat, room="kitchen", image="other.jpg")
        other, _ = self.analyse(otherPhoto)
        item = source.items.get()
        candidates = like_pool(self.flat.pk)
        self.assertEqual(like_items(item, candidates), [other.items.get()])
        PhotoAnalysis.objects.filter(pk=other.pk).update(pipelineIdentity={})
        self.assertEqual(like_pool(self.flat.pk), {})
        candidates["seating"][0][0].analysis.pipelineIdentity = {}
        self.assertEqual(like_items(item, candidates), [])
        source.pipelineIdentity = {}
        item.analysis = source
        self.assertEqual(like_items(item), [])

    def test_queue_requeues_legacy_old(
        self,
    ):
        record, _ = self.analyse(self.photo)
        for version in [{}, {"version": "old"}]:
            with self.subTest(version=version):
                PhotoAnalysis.objects.filter(pk=record.pk).update(status="done", pipelineIdentity=version)
                self.assertIn(record.pk, queue._waiting())
                record.refresh_from_db()
                self.assertEqual(record.status, "pending")
        for status in ["done", "failed", "running"]:
            PhotoAnalysis.objects.filter(pk=record.pk).update(
                status=status, pipelineIdentity=identity.pipelineIdentity()
            )
            self.assertNotIn(record.pk, queue._waiting())
            record.refresh_from_db()
            self.assertEqual(record.status, status)
        PhotoAnalysis.objects.filter(pk=record.pk).update(status="done", pipelineIdentity={})
        queue.queue_flat(self.flat.pk)
        record.refresh_from_db()
        self.assertEqual(record.status, "pending")
