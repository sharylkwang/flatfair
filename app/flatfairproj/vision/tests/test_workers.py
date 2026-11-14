import io
from pathlib import Path
import signal
from tempfile import TemporaryDirectory
import tempfile
from unittest.mock import patch, MagicMock, Mock

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, transaction
from django.test import override_settings, TestCase, SimpleTestCase

from pretrained import pipelineIdentity
from sellerpanel import device
from storefront.models import ListingPhoto, Profile
from vision import brands, pipeline, worker, queue
from vision.models import DetectedItem, PhotoAnalysis
from vision.services import like_pool
from vision.tests.helpers import listing, makeListing


class BrandTests(TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=self.temp.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.temp.cleanup)
        self.flat = listing()
        self.photo = ListingPhoto.objects.create(listing=self.flat, room="master_bedroom", image="test.jpg")
        self.analysis = PhotoAnalysis.objects.create(
            photo=self.photo,
            status="done",
            pipelineIdentity=pipelineIdentity(),
            timings={"size": [1000, 667]},
        )
        self.aircon = DetectedItem.objects.create(
            analysis=self.analysis,
            label="air conditioner",
            score=0.9,
            box=[1, 2, 3, 4],
            polygon=[[1, 2], [3, 4]],
            attributes={"colour": "white"},
        )
        self.bed = DetectedItem.objects.create(
            analysis=self.analysis, label="bed", score=0.9, box=[5, 6, 7, 8], attributes={}
        )

    def run_command(self, found, *args):
        out = io.StringIO()
        with (
            patch.object(brands, "ocr_available", return_value=True),
            patch.object(
                brands, "detect", return_value=(found, {"version": brands.VERSION, "reads": 1})
            ) as detect,
        ):
            call_command("detect_brands", *args, stdout=out)
        return (detect, out.getvalue())

    def test_command_updates_attributes(self):
        detect, out = self.run_command(["Daikin", None])
        path, items, size = detect.call_args.args
        self.assertEqual(path, self.photo.image.path)
        self.assertEqual(
            items[0], {"label": "air conditioner", "polygon": [[1, 2], [3, 4]], "box": [1, 2, 3, 4]}
        )
        self.assertEqual(size, [1000, 667])
        self.aircon.refresh_from_db()
        self.bed.refresh_from_db()
        self.analysis.refresh_from_db()
        self.assertEqual(list(self.aircon.attributes.items()), [("brand", "Daikin"), ("colour", "white")])
        self.assertEqual(self.bed.attributes, {})
        self.assertEqual(self.analysis.timings["brands"]["version"], brands.VERSION)
        self.assertEqual(self.analysis.timings["size"], [1000, 667])
        self.assertIn("air conditioner: Daikin", out)
        detect, _ = self.run_command([None, None])
        detect.assert_not_called()
        detect, _ = self.run_command([None, None], "--force")
        detect.assert_called_once()
        self.aircon.refresh_from_db()
        self.assertEqual(self.aircon.attributes, {"colour": "white"})

    def test_command_skips_other_listings(self):
        detect, _ = self.run_command(["Daikin", None], "--listing", str(self.flat.pk + 1))
        detect.assert_not_called()
        PhotoAnalysis.objects.filter(pk=self.analysis.pk).update(status="running")
        detect, _ = self.run_command(["Daikin", None], "--listing", str(self.flat.pk))
        detect.assert_not_called()

    def test_command_retries_missing_failed(self):
        PhotoAnalysis.objects.filter(pk=self.analysis.pk).update(
            timings={"size": [1000, 667], "brands": {"version": brands.VERSION, "reads": 1}}
        )
        waiting = []
        for room, timings in [
            ("kitchen", {}),
            ("bathroom", {"brands": {"error": "out of memory"}}),
            ("living_room", {"brands": {"version": brands.VERSION - 1}}),
            ("balcony", {"brands": None}),
        ]:
            photo = ListingPhoto.objects.create(listing=self.flat, room=room, image="test.jpg")
            waiting.append(
                PhotoAnalysis.objects.create(
                    photo=photo, status="done", pipelineIdentity=pipelineIdentity(), timings=timings
                )
            )
        detect, out = self.run_command([])
        self.assertEqual(detect.call_count, 4)
        self.assertIn("Checked 4 photo(s)", out)
        self.assertNotIn(f"Photo {self.photo.pk} ", out)
        PhotoAnalysis.objects.filter(pk__in=[a.pk for a in waiting]).update(timings={})
        detect, out = self.run_command([], "--limit", "2")
        self.assertEqual(detect.call_count, 2)
        self.assertIn(f"Photo {waiting[0].photo_id} ", out)
        self.assertIn(f"Photo {waiting[1].photo_id} ", out)

    def test_command_requires_available_ocr(self):
        with (
            patch.object(brands, "ocr_available", return_value=False),
            self.assertRaises(CommandError),
        ):
            call_command("detect_brands", stdout=io.StringIO())


@patch("django.db.close_old_connections", new=lambda: None)
class WorkerTests(TestCase):

    def test_command_runs_child_analyser(self):
        photo = ListingPhoto.objects.create(listing=listing(), room="kitchen", image="test.jpg")
        record = PhotoAnalysis.objects.get_or_create(photo=photo)[0]
        handlers = {}

        def worker(pk):
            PhotoAnalysis.objects.filter(pk=pk).update(status="done", pipelineIdentity=pipelineIdentity())
            handlers[signal.SIGTERM]()
            return "done"

        with (
            patch(
                "signal.signal",
                side_effect=lambda number, handler: handlers.setdefault(number, handler),
            ),
            patch("vision.queue.recover"),
            patch("vision.worker.run_vision", side_effect=worker) as run,
            patch("vision.worker.end_vision") as stop,
            patch("vision.services.process") as process,
        ):
            call_command("vision_worker", stdout=io.StringIO())
        run.assert_called_once_with(record.pk)
        stop.assert_called()
        process.assert_not_called()
        record.refresh_from_db()
        self.assertEqual(record.status, "done")


class VisionDeviceTests(SimpleTestCase):

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        marker = patch.object(device, "MARKER", Path(folder.name) / "gpu_disabled_until.txt")
        marker.start()
        self.addCleanup(marker.stop)

    def test_pool_uses_selected_gpu(self):
        with (
            patch.object(worker, "ProcessPoolExecutor") as pool,
            patch.object(worker, "pickGpu", return_value=1) as pick,
            patch.object(worker, "_vision_gpu", None),
        ):
            worker._new_pool()
            self.assertEqual(worker._vision_gpu, 1)
        self.assertEqual(pick.call_args.args[1], worker.VISION_GPU_NEED_MB)
        self.assertEqual(pool.call_args.kwargs["initargs"], (worker.VISION_HARD_MB, 1))

    def run_once(self, gpu):
        pool = Mock()
        pool._processes = {}
        pool.submit.return_value.result.side_effect = TimeoutError()
        with (
            patch("vision.pipeline.weights_ok", return_value=True),
            patch.object(worker, "_vision_pool", pool),
            patch.object(worker, "_vision_gpu", gpu),
            self.assertLogs("vision.worker", "ERROR"),
        ):
            return worker.run_vision(7)

    def test_gpu_failure_blocks_next(self):
        self.assertIsNone(self.run_once(None))
        self.assertFalse(device.gpuOff())
        with self.assertLogs("sellerpanel.device", "ERROR"):
            self.assertIsNone(self.run_once(1))
        self.assertTrue(device.gpuOff())
        with patch.object(device.subprocess, "run") as run:
            self.assertIsNone(device.pickGpu("auto", worker.VISION_GPU_NEED_MB))
        run.assert_not_called()

    def test_gpu_error_only_for_cuda(self):
        with patch.object(pipeline, "DEVICE", "cpu"):
            self.assertFalse(worker.gpu_failed("CUDA out of memory"))
        with patch.object(pipeline, "DEVICE", "cuda"):
            self.assertTrue(worker.gpu_failed("CUDA out of memory"))
            self.assertTrue(worker.gpu_failed("cuDNN error: asdf"))
            self.assertFalse(worker.gpu_failed("Model identity mismatch"))

    def test_inputs_move_only_for_cuda(self):
        inputs = MagicMock()
        with patch.object(pipeline, "DEVICE", "cpu"):
            self.assertIs(pipeline.onDevice(inputs), inputs)
        inputs.to.assert_not_called()
        with patch.object(pipeline, "DEVICE", "cuda"):
            self.assertIs(pipeline.onDevice(inputs), inputs.to.return_value)
        inputs.to.assert_called_once_with("cuda")

    def test_registers_gpu_cleanup(self):
        with (
            patch.object(worker, "startDevice", return_value="cuda"),
            patch("atexit.register") as register,
            patch.object(pipeline, "DEVICE", "cpu"),
        ):
            worker.use_device()
            self.assertEqual(pipeline.DEVICE, "cuda")
        register.assert_called_once_with(worker.exitWorker)
        with (
            patch.object(worker, "startDevice", return_value="cpu"),
            patch("atexit.register") as register,
            patch.object(pipeline, "DEVICE", "cpu"),
        ):
            worker.use_device()
            self.assertEqual(pipeline.DEVICE, "cpu")
        register.assert_not_called()

    def test_gpu_failure_requeues_exits(self):
        record = Mock()
        analyses = Mock()
        analyses.filter.return_value.select_related.return_value.first.return_value = record
        failed = Mock(status="failed", error="CUDA out of memory. asdf")
        with (
            patch("vision.models.PhotoAnalysis.objects", analyses),
            patch("vision.services.process", return_value=failed),
            patch("django.db.close_old_connections"),
            patch.object(pipeline, "DEVICE", "cuda"),
            patch.object(worker, "disableGpu") as disable,
            patch.object(worker.os, "_exit", side_effect=SystemExit) as leave,
        ):
            with self.assertRaises(SystemExit):
                worker._scan_one(7)
        leave.assert_called_once_with(5)
        disable.assert_called_once()
        self.assertEqual(
            analyses.filter.return_value.update.call_args.kwargs, {"status": "pending", "error": ""}
        )
        with (
            patch("vision.models.PhotoAnalysis.objects", analyses),
            patch("vision.services.process", return_value=failed),
            patch("django.db.close_old_connections"),
            patch.object(pipeline, "DEVICE", "cpu"),
            patch.object(worker, "disableGpu") as disable,
            patch.object(worker.os, "_exit") as leave,
        ):
            self.assertEqual(worker._scan_one(7), "failed")
        leave.assert_not_called()
        disable.assert_not_called()


class StopSupervisor(BaseException):
    pass


@patch("django.db.close_old_connections", new=lambda: None)
class QueueTests(TestCase):

    def setUp(self):
        self.flat = makeListing(None, "live")
        self.record = self.analysis("living_room")

    def analysis(self, room):
        photo = ListingPhoto.objects.create(listing=self.flat, room=room, image="test.jpg")
        return PhotoAnalysis.objects.get_or_create(photo=photo)[0]

    def supervise(self, worker):
        with (
            patch.object(queue, "recover"),
            patch("vision.worker.run_vision", side_effect=worker) as run,
        ):
            with (
                patch.object(queue._wake, "wait", side_effect=StopSupervisor),
                patch("vision.worker.vision_up", return_value=False),
            ):
                with self.assertRaises(StopSupervisor):
                    queue._supervise()
        return run

    def test_interrupted_worker_retried(self):
        states = []

        def interrupted(pk):
            self.record.refresh_from_db()
            states.append(self.record.status)
            PhotoAnalysis.objects.filter(pk=pk).update(status="running")
            return None

        run = self.supervise(interrupted)
        self.assertEqual(run.call_count, queue.MAX_ATTEMPTS)
        self.assertEqual(states, ["pending", "pending"])
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, "failed")
        self.assertIn("stopped twice", self.record.error)

    def test_successful_jobs_reset(self):
        outcomes = iter(("done", "done", None, "done"))
        states = []

        def waiting():
            if len(states) >= 4:
                return []
            PhotoAnalysis.objects.filter(pk=self.record.pk, status="done").update(status="pending")
            return [self.record.pk]

        def worker(pk):
            self.record.refresh_from_db()
            states.append(self.record.status)
            status = next(outcomes)
            PhotoAnalysis.objects.filter(pk=pk).update(
                status=status or "running", pipelineIdentity=pipelineIdentity()
            )
            return status

        with patch.object(queue, "_waiting", side_effect=waiting):
            run = self.supervise(worker)
        self.assertEqual(run.call_count, 4)
        self.assertEqual(states, ["pending"] * 4)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, "done")

    def test_interrupted_worker_complete_on(self):
        outcomes = iter((None, "done"))
        states = []

        def worker(pk):
            self.record.refresh_from_db()
            states.append(self.record.status)
            status = next(outcomes)
            PhotoAnalysis.objects.filter(pk=pk).update(
                status=status or "running", pipelineIdentity=pipelineIdentity()
            )
            return status

        run = self.supervise(worker)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(states, ["pending", "pending"])
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, "done")
        self.assertEqual(self.record.error, "")

    def test_retry_database_error_not(self):
        second = self.analysis("kitchen")
        calls = []

        def worker(pk):
            calls.append(pk)
            if pk == self.record.pk:
                PhotoAnalysis.objects.filter(pk=pk).update(status="running")
                return None
            PhotoAnalysis.objects.filter(pk=pk).update(status="done", pipelineIdentity=pipelineIdentity())
            return "done"

        with patch.object(queue, "_retry", side_effect=OperationalError("database is locked")):
            with self.assertLogs("vision.queue", level="ERROR"):
                self.supervise(worker)
        self.assertEqual(calls, [self.record.pk, second.pk])
        second.refresh_from_db()
        self.assertEqual(second.status, "done")

    def test_worker_exceptions_exhaust_retry(self):

        def worker(pk):
            PhotoAnalysis.objects.filter(pk=pk).update(status="running")
            raise RuntimeError("Worker unavailable")

        with self.assertLogs("vision.queue", level="ERROR"):
            run = self.supervise(worker)
        self.assertEqual(run.call_count, queue.MAX_ATTEMPTS)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, "failed")
        self.assertIn("stopped twice", self.record.error)


class CropTests(TestCase):

    def setUp(self):
        self.media = TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        setting = override_settings(MEDIA_ROOT=self.media.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.seller = User.objects.create_user("seller")
        Profile.objects.create(user=self.seller, role="seller")

    def item(self):
        flat = makeListing(self.seller, "sold")
        photo = ListingPhoto.objects.create(listing=flat, image="test.jpg", room="living")
        analysis = PhotoAnalysis.objects.create(
            photo=photo, status="done", pipelineIdentity=pipelineIdentity()
        )
        item = DetectedItem.objects.create(analysis=analysis, label="sofa", score=0.9)
        item.crop.save(f"{item.pk}.png", ContentFile(b"crop"))
        return item

    def test_file_deleted_commit_cascade(self):
        for target in ("photo", "listing", "analysis", "item"):
            with self.subTest(target=target):
                item = self.item()
                path = Path(item.crop.path)
                photo = item.analysis.photo
                obj = {
                    "photo": photo,
                    "listing": photo.listing,
                    "analysis": item.analysis,
                    "item": item,
                }[target]
                with self.captureOnCommitCallbacks(execute=False) as callbacks:
                    obj.delete()
                self.assertTrue(path.exists())
                self.assertFalse(DetectedItem.objects.filter(pk=item.pk).exists())
                for callback in callbacks:
                    callback()
                self.assertFalse(path.exists())

    def test_rolled_back_photo_deletion(self):
        item = self.item()
        path = Path(item.crop.path)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            try:
                with transaction.atomic():
                    item.analysis.photo.delete()
                    raise ValueError("Roll back the photo deletion")
            except ValueError:
                pass
        self.assertEqual(callbacks, [])
        self.assertTrue(path.exists())
        self.assertTrue(DetectedItem.objects.filter(pk=item.pk).exists())


class CandidateTests(TestCase):

    def test_similar_include_public_listings(self):
        active = User.objects.create_user("active")
        disabled = User.objects.create_user("disabled", is_active=False)
        Profile.objects.create(user=active, role="seller")
        Profile.objects.create(user=disabled, role="seller")
        expected = []
        for seller, status, previous, visible in (
            (active, "live", "", True),
            (disabled, "live", "", False),
            (None, "live", "", False),
            (User.objects.create_user("review"), "review", "", False),
            (active, "sold", "live", True),
            (active, "sold", "review", False),
        ):
            flat = makeListing(seller, status)
            flat.statusBeforeSold = previous
            flat.save(update_fields=["statusBeforeSold"])
            photo = ListingPhoto.objects.create(listing=flat, image="test.jpg", room="living")
            analysis = PhotoAnalysis.objects.create(
                photo=photo, status="done", pipelineIdentity=pipelineIdentity()
            )
            item = DetectedItem.objects.create(
                analysis=analysis, label="sofa", score=0.9, embedding=[1.0] + [0.0] * 511
            )
            if visible:
                expected.append(item.pk)
        candidates = like_pool(-1)
        self.assertEqual([item.pk for item in candidates["seating"][0]], expected)
        excluded = like_pool(DetectedItem.objects.get(pk=expected[0]).analysis.photo.listing_id)
        self.assertEqual([item.pk for item in excluded["seating"][0]], expected[1:])
