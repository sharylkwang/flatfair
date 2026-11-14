import fcntl
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from storefront.models import ListingPhoto
from vision import worker
from vision.models import PhotoAnalysis
from vision.queue import recover
from vision.tests.helpers import listing


def executor(result="done", error=None):
    pool = Mock()
    pool._processes = {}
    if error:
        pool.submit.return_value.result.side_effect = error
    else:
        pool.submit.return_value.result.return_value = result
    return pool


class WorkerTests(SimpleTestCase):

    def run_once(self, pool, rss=500, count=0):
        with (
            patch("vision.pipeline.weights_ok", return_value=True),
            patch.object(worker, "_vision_pool", pool),
            patch.object(worker, "_vision_count", count),
            patch.object(worker, "_rss_mb", return_value=rss),
        ):
            status = worker.run_vision(7)
            return (status, worker._vision_pool)

    def test_vision_job_reuses_existing(self):
        pool = executor("done")
        status, left = self.run_once(pool)
        self.assertEqual(status, "done")
        pool.submit.assert_called_once_with(worker._scan_one, 7)
        self.assertIs(left, pool)

    def test_vision_pool_recycles_at(self):
        for rss, count in ((worker.VISION_RSS_CAP_MB + 1, 0), (500, worker.VISION_MAX_JOBS - 1)):
            pool = executor("done")
            status, left = self.run_once(pool, rss=rss, count=count)
            self.assertEqual(status, "done")
            self.assertIsNone(left)
            pool.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

    def test_timed_out_job_terminates(self):
        process = Mock()
        pool = executor(error=TimeoutError())
        pool._processes = {123: process}
        with self.assertLogs("vision.worker", level="ERROR"):
            status, left = self.run_once(pool)
        self.assertIsNone(status)
        self.assertIsNone(left)
        process.terminate.assert_called_once()

    def test_stopping_vision_shuts_down(self):
        pool = executor()
        with patch.object(worker, "_vision_pool", pool):
            worker.end_vision()
            self.assertIsNone(worker._vision_pool)
        pool.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

    def test_missing_weights_prevent_pool(self):
        with (
            patch("vision.pipeline.weights_ok", return_value=False),
            patch.object(worker, "_new_pool") as new,
        ):
            self.assertIsNone(worker.run_vision(7))
        new.assert_not_called()

    def test_new_pool_single_spawn(self):
        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(worker, "ProcessPoolExecutor") as pool,
        ):
            os.environ.pop("MALLOC_ARENA_MAX", None)
            worker._new_pool()
            self.assertEqual(os.environ["MALLOC_ARENA_MAX"], "2")
        self.assertEqual(pool.call_args.kwargs["max_workers"], 1)
        self.assertEqual(pool.call_args.kwargs["mp_context"].get_start_method(), "spawn")
        self.assertEqual(pool.call_args.kwargs["initargs"], (worker.VISION_HARD_MB, None))


@patch("django.db.close_old_connections", new=lambda: None)
class RecoverTests(TestCase):

    def test_recovery_requeues_running(self):
        photo = ListingPhoto.objects.create(listing=listing(), room="kitchen", image="test.jpg")
        stuck = PhotoAnalysis.objects.create(photo=photo, status="running")
        with TemporaryDirectory() as media, self.settings(MEDIA_ROOT=media):
            lock_path = Path(media) / ".locks" / "vision.lock"
            lock_path.parent.mkdir(parents=True)
            with open(lock_path, "a") as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                self.assertEqual(recover(), 0)
            self.assertEqual(recover(), 1)
        stuck.refresh_from_db()
        self.assertEqual(stuck.status, "pending")
