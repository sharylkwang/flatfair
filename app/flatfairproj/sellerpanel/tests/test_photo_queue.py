from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import tempfile
from threading import Barrier
import time
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from sellerpanel import photoqueue
from sellerpanel.device import modelLock
from sellerpanel.photocheck import MODEL_VERSION
from sellerpanel.photos import DRAFTS_KEY, sweepPics, waitChecks
from sellerpanel.photoworker import runWorker
from storefront.models import Listing, PhotoCheckJob
from storefront.tests.helpers import CLEAN, FAIR, FLAT, ROOMS, jpeg, makeUser, makeListing


@patch("django.db.close_old_connections", new=lambda: None)
class PhotoQueueTests(TestCase):

    def setUp(self):
        cache.clear()
        photoqueue.resourceCache.clear()
        self.owner = makeUser("queue@test.com", "seller")
        self.client.force_login(self.owner)
        self.token = self.client.get(reverse("sell")).context["draftToken"]
        self.starter = patch("sellerpanel.photoqueue.ensureWorker")
        self.start = self.starter.start()
        self.addCleanup(self.starter.stop)
        photoStarter = patch("sellerpanel.photos.ensureWorker")
        photoStarter.start()
        self.addCleanup(photoStarter.stop)

    def upload(self, room="kitchen"):
        return self.client.post(
            reverse("photo_check"), {"draftToken": self.token, "room": room, "photo": jpeg()}
        )

    def status(self):
        cache.delete(f"photo-status:{self.owner.pk}")
        return self.client.get(reverse("photo_status"), {"draftToken": self.token})

    def job(self, room="kitchen", **fields):
        path = default_storage.save(f"pending/{time.time_ns()}.jpg", ContentFile(jpeg().read()))
        return PhotoCheckJob.objects.create(owner=self.owner, room=room, pendingPath=path, **fields)

    def finish(self, result=CLEAN):
        PhotoCheckJob.objects.filter(status__in=("queued", "running")).update(
            status="done", result=result or {"unchecked": True}, finishedAt=timezone.now()
        )

    def test_upload_enqueues_immediately(self):
        with (
            patch("sellerpanel.photos.checkPics") as sync,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.upload()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "checking")
        sync.assert_not_called()
        self.start.assert_called_once()
        job = PhotoCheckJob.objects.get()
        self.assertEqual(
            (job.owner, job.room, job.status, job.attempts), (self.owner, "kitchen", "queued", 0)
        )
        self.assertTrue(default_storage.exists(job.pendingPath))
        self.assertIn("no-store", response["Cache-Control"])

    def test_replacement_supersedes_running(self):
        self.upload()
        old = photoqueue.claimJobs()[0]
        self.upload()
        old.refresh_from_db()
        self.assertEqual(old.status, "failed")
        self.assertIn("Replaced", old.error)
        self.assertFalse(default_storage.exists(old.pendingPath))
        changed = PhotoCheckJob.objects.filter(pk=old.pk, status="running").update(
            status="done", result=CLEAN
        )
        self.assertEqual(changed, 0)
        self.assertEqual(PhotoCheckJob.objects.filter(status="queued").count(), 1)
        self.assertEqual(self.status().json()["photos"]["kitchen"]["verdict"], "checking")

    def test_independent_drafts_keep_their(self):
        self.upload()
        firstToken = self.token
        firstJob = PhotoCheckJob.objects.get()
        secondToken = self.client.get(reverse("sell")).context["draftToken"]
        self.assertNotEqual(secondToken, firstToken)
        response = self.client.post(
            reverse("photo_check"),
            {"draftToken": secondToken, "room": "kitchen", "photo": jpeg("blue")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["verdict"], "checking")
        secondJob = PhotoCheckJob.objects.exclude(pk=firstJob.pk).get()
        for token, job in ((firstToken, firstJob), (secondToken, secondJob)):
            with self.subTest(token=token):
                job.refresh_from_db()
                self.assertEqual((job.status, job.attempts), ("queued", 0))
                self.assertTrue(default_storage.exists(job.pendingPath))
                data = self.client.session[DRAFTS_KEY][token]
                self.assertEqual(data["photos"]["kitchen"], job.pendingPath)
                self.assertEqual(data["checks"]["kitchen"]["jobId"], str(job.pk))
                cache.delete(f"photo-status:{self.owner.pk}")
                state = self.client.get(reverse("photo_status"), {"draftToken": token})
                self.assertEqual(state.json()["photos"]["kitchen"]["verdict"], "checking")

    def test_worker_loads_once_batches(self):
        jobs = [self.job(room=ROOMS[index % len(ROOMS)]) for index in range(8)]
        sizes = []

        def score(paths):
            sizes.append(len(paths))
            self.assertTrue(all((Path(path).exists() for path in paths)))
            return [CLEAN for path in paths]

        loader = MagicMock(return_value=score)
        count = runWorker(idleSeconds=0, loader=loader)
        self.assertEqual((count, sizes), (8, [6, 2]))
        loader.assert_called_once_with()
        self.assertEqual(PhotoCheckJob.objects.filter(status="done", attempts=1).count(), 8)
        for job in jobs:
            job.refresh_from_db()
            self.assertEqual(job.result["modelVersion"], MODEL_VERSION)
            self.assertGreaterEqual(job.finishedAt, job.startedAt)
        self.assertIsNone(photoqueue.resourceCache.get(photoqueue.HEARTBEAT_KEY))

    def test_missing_file_not_spend(self):
        missing = self.job(room="kitchen")
        healthy = self.job(room="living_room")
        default_storage.delete(missing.pendingPath)
        result = {**CLEAN, "label": "living_room"}
        scorer = MagicMock(side_effect=lambda paths: [dict(result) for path in paths])
        loader = MagicMock(return_value=scorer)
        with patch("sellerpanel.photoworker.log"):
            runWorker(idleSeconds=0, loader=loader)
        healthy.refresh_from_db()
        missing.refresh_from_db()
        self.assertEqual((healthy.status, healthy.attempts, healthy.result), ("done", 1, result))
        self.assertEqual(
            (missing.status, missing.attempts, missing.result), ("failed", 2, {"unchecked": True})
        )
        self.assertTrue(missing.error)
        self.assertIsNotNone(missing.finishedAt)
        loader.assert_called_once_with()
        scorer.assert_called_once()
        self.assertEqual(len(scorer.call_args.args[0]), 1)

    def test_mixed_batch_keeps_results(self):
        other = makeUser("batch-other@test.com", "seller")
        samples = (
            (self.owner, "kitchen", "red", dict(CLEAN)),
            (self.owner, "bedroom_2", "blue", {**CLEAN, "label": "selfie", "irrelevant": 0.99}),
            (other, "kitchen", "green", {**CLEAN, "aiGenerated": 0.99}),
        )
        expected = []
        resultsByImage = {}
        for owner, room, colour, result in samples:
            image = jpeg(colour).read()
            path = default_storage.save(f"pending/{time.time_ns()}.jpg", ContentFile(image))
            job = PhotoCheckJob.objects.create(owner=owner, room=room, pendingPath=path)
            expected.append((job, owner.pk, room, result))
            resultsByImage[image] = result

        def score(paths):
            return [dict(resultsByImage[Path(path).read_bytes()]) for path in paths]

        scorer = MagicMock(side_effect=score)
        self.assertEqual(runWorker(once=True, loader=lambda: scorer), 3)
        scorer.assert_called_once()
        for job, ownerId, room, result in expected:
            with self.subTest(job=job.pk):
                job.refresh_from_db()
                self.assertEqual(
                    (job.owner_id, job.room, job.status, job.attempts), (ownerId, room, "done", 1)
                )
                self.assertEqual(job.result, result)

    def test_worker_exits_idle_missing(self):
        loader = MagicMock()
        started = time.monotonic()
        self.assertEqual(runWorker(idleSeconds=0.01, loader=loader), 0)
        self.assertLess(time.monotonic() - started, 1)
        loader.assert_not_called()

    def test_worker_once_job_lifetime(self):
        for index in range(7):
            self.job()
        loader = MagicMock(return_value=lambda paths: [CLEAN] * len(paths))
        self.assertEqual(runWorker(once=True, loader=loader), 6)
        self.assertEqual(PhotoCheckJob.objects.filter(status="queued").count(), 1)
        self.assertEqual(runWorker(maxJobs=1, loader=loader), 1)
        self.assertEqual(runWorker(maxLifetime=0, loader=loader), 0)
        with modelLock() as held:
            self.assertTrue(held)

    def test_worker_retries_errors_fails(self):
        job = self.job()
        loader = MagicMock(return_value=MagicMock(side_effect=ValueError("unreadable")))
        with self.assertLogs("sellerpanel.photoworker", "ERROR"):
            runWorker(idleSeconds=0, loader=loader)
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts, job.result), ("failed", 2, {"unchecked": True}))
        self.assertIn("unreadable", job.error)
        loader.assert_called_once()

    def test_model_unavailable_identity(self):
        for result in [
            None,
            {"identityStatus": "identity mismatch", "modelVersion": MODEL_VERSION},
        ]:
            with self.subTest(result=result):
                self.upload()
                runWorker(once=True, loader=lambda: lambda paths: [result] * len(paths))
                reply = self.status().json()["photos"]["kitchen"]
                self.assertEqual(reply["verdict"], "review")
                data = self.client.session[DRAFTS_KEY][self.token]
                self.assertIn("kitchen", data["photos"])
                if result:
                    self.assertEqual(data["checks"]["kitchen"]["identityStatus"], "identity mismatch")

    def test_stale_job_recovery_retries(self):
        old = timezone.now() - timedelta(seconds=100)
        job = self.job(status="running", startedAt=old, attempts=1)
        photoqueue.resourceCache.set(photoqueue.HEARTBEAT_KEY, time.time(), 180)
        self.assertEqual(photoqueue.fixStale(), 0)
        photoqueue.resourceCache.delete(photoqueue.HEARTBEAT_KEY)
        with modelLock() as held:
            self.assertTrue(held)
            self.assertEqual(photoqueue.fixStale(), 0)
        self.assertEqual(photoqueue.fixStale(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, "queued")
        claimed = photoqueue.claimJobs()[0]
        self.assertEqual(claimed.attempts, 2)
        PhotoCheckJob.objects.filter(pk=job.pk).update(startedAt=old)
        self.assertEqual(photoqueue.fixStale(), 1)
        job.refresh_from_db()
        self.assertEqual((job.status, job.result), ("failed", {"unchecked": True}))

    def test_status_owner_checked_never(self):
        self.upload()
        other = makeUser("otherqueue@test.com", "seller")
        foreign = PhotoCheckJob.objects.create(
            owner=other,
            room="bedroom_2",
            pendingPath="pending/foreign.jpg",
            status="done",
            result=CLEAN,
        )
        session = self.client.session
        data = session[DRAFTS_KEY][self.token]
        data["checks"]["bedroom_2"] = {"jobId": str(foreign.pk), "checking": True}
        data["photos"]["bedroom_2"] = foreign.pendingPath
        session.save()
        response = self.status()
        self.assertEqual(response.json()["photos"]["bedroom_2"]["verdict"], "review")
        self.assertNotIn(str(foreign.pk), response.content.decode())
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(
            self.client.get(reverse("photo_status"), {"draftToken": self.token}).status_code, 429
        )
        self.assertEqual(self.client.get(reverse("photo_status"), {"draftToken": "other"}).status_code, 400)

    def test_refusal_frees_slot_keeps(self):
        self.upload()
        self.finish({**CLEAN, "label": "selfie", "irrelevant": 0.99})
        reply = self.status().json()["photos"]["kitchen"]
        self.assertEqual(
            reply,
            {
                "verdict": "block",
                "message": "This looks like a selfie, not the kitchen. Please choose a photo of that room instead.",
                "appealable": True,
            },
        )
        data = self.client.session[DRAFTS_KEY][self.token]
        self.assertNotIn("kitchen", data["photos"])
        self.assertIn("kitchen", data["refused"])

    def test_no_javascript_shows_refresh(self):
        response = self.client.post(
            reverse("sell"),
            {"draftToken": self.token, "description": "Keep my details", "photo_kitchen": jpeg()},
        )
        self.assertContains(response, "Checking… refresh to see the result.")
        self.assertContains(response, f"?draftToken={self.token}#sell-photos")
        self.finish()
        response = self.client.get(reverse("sell"), {"draftToken": self.token})
        self.assertContains(response, "Photo accepted.")
        self.assertEqual(response.context["form"]["description"].value(), "Keep my details")

    def test_submission_blocks_publish(self):
        for room in ROOMS:
            self.upload(room)
        originalWait = waitChecks
        with (
            patch("sellerpanel.views.band", return_value=FAIR),
            patch(
                "sellerpanel.views.waitChecks",
                side_effect=lambda request: originalWait(request, seconds=0),
            ),
        ):
            response = self.client.post(reverse("sell"), {**FLAT, "draftToken": self.token})
            self.assertContains(response, "Your listing hasn’t been published yet.")
            self.assertFalse(Listing.objects.exists())
            self.finish()
            response = self.client.post(reverse("sell"), {**FLAT, "draftToken": self.token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Listing.objects.get().status, "live")

    def test_submission_waits_briefly(self):
        for room in ROOMS:
            self.upload(room)
        with (
            patch("sellerpanel.views.band", return_value=FAIR),
            patch("sellerpanel.photos.time.sleep", side_effect=lambda seconds: self.finish()),
        ):
            response = self.client.post(reverse("sell"), {**FLAT, "draftToken": self.token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Listing.objects.get().status, "live")

    def test_failed_jobs_allow_submission(self):
        for room in ROOMS:
            self.upload(room)
        PhotoCheckJob.objects.update(status="failed", finishedAt=timezone.now(), error="Worker stopped twice")
        with patch("sellerpanel.views.band", return_value=FAIR):
            response = self.client.post(reverse("sell"), {**FLAT, "draftToken": self.token})
        self.assertEqual(response.status_code, 302)
        listing = Listing.objects.get()
        self.assertEqual(listing.status, "review")
        self.assertTrue(all((photo.aiLabel == "unchecked" for photo in listing.photos.all())))

    def test_draft_saving_not_wait(self):
        self.upload()
        with patch("sellerpanel.views.waitChecks") as wait:
            response = self.client.post(reverse("sell"), {"draftToken": self.token, "action": "saveDraft"})
        self.assertEqual(response.status_code, 302)
        wait.assert_not_called()
        self.assertEqual(PhotoCheckJob.objects.get().status, "queued")

    def test_draft_save_keeps_file(self):
        from sellerpanel.models import ListingDraft

        with patch("sellerpanel.views.waitChecks") as wait:
            response = self.client.post(
                reverse("sell"),
                {
                    "draftToken": self.token,
                    "action": "saveDraft",
                    "description": "Keep this draft",
                    "photo_kitchen": jpeg(),
                },
            )
        self.assertEqual(response.status_code, 302)
        wait.assert_not_called()
        job = PhotoCheckJob.objects.get(owner=self.owner, room="kitchen")
        saved = ListingDraft.objects.get(seller=self.owner)
        self.assertEqual(saved.photos["kitchen"], job.pendingPath)
        self.assertEqual(saved.checks["kitchen"]["jobId"], str(job.pk))
        self.assertTrue(default_storage.exists(job.pendingPath))
        self.assertEqual(job.status, "queued")

    def test_pending_saved_draft_restores(self):
        self.upload()
        job = PhotoCheckJob.objects.get()
        response = self.client.post(
            reverse("sell"),
            {
                "draftToken": self.token,
                "action": "saveDraft",
                "description": "Keep this pending photo",
            },
        )
        self.assertEqual(response.status_code, 302)
        restored = self.client_class()
        restored.force_login(self.owner)
        response = restored.get(reverse("sell"))
        self.assertEqual(response.context["draftToken"], self.token)
        self.assertEqual(response.context["form"]["description"].value(), "Keep this pending photo")
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts), ("queued", 0))
        data = restored.session[DRAFTS_KEY][self.token]
        self.assertEqual(data["checks"]["kitchen"]["jobId"], str(job.pk))
        self.assertEqual(data["photos"]["kitchen"], job.pendingPath)
        runWorker(once=True, loader=lambda: lambda paths: [dict(CLEAN) for path in paths])
        cache.delete(f"photo-status:{self.owner.pk}")
        response = restored.get(reverse("photo_status"), {"draftToken": self.token})
        self.assertEqual(response.json()["photos"]["kitchen"]["verdict"], "accept")
        data = restored.session[DRAFTS_KEY][self.token]
        self.assertNotIn("checking", data["checks"]["kitchen"])
        self.assertEqual(data["checks"]["kitchen"]["modelVersion"], MODEL_VERSION)
        self.assertEqual(data["photos"]["kitchen"], job.pendingPath)

    def test_cleanup_deletes_old_finished(self):
        from sellerpanel.models import ListingDraft

        old = timezone.now() - timedelta(days=2)
        orphan = self.job(status="done", finishedAt=old)
        saved = self.job(status="done", finishedAt=old)
        active = self.job()
        ListingDraft.objects.create(
            seller=self.owner, draftToken=self.token, photos={"kitchen": saved.pendingPath}
        )
        sweepPics()
        self.assertFalse(PhotoCheckJob.objects.filter(pk__in=[orphan.pk, saved.pk]).exists())
        self.assertFalse(default_storage.exists(orphan.pendingPath))
        self.assertTrue(default_storage.exists(saved.pendingPath))
        self.assertTrue(default_storage.exists(active.pendingPath))

    def test_ensure_worker_starts_once(self):
        self.starter.stop()
        self.job()
        with (
            patch.object(photoqueue.subprocess, "Popen") as popen,
            patch.object(photoqueue.threading, "Thread"),
        ):
            self.assertTrue(photoqueue.ensureWorker())
            self.assertFalse(photoqueue.ensureWorker())
            popen.assert_called_once()
            self.assertEqual(popen.call_args.kwargs["env"]["CUDA_VISIBLE_DEVICES"], "")
            photoqueue.resourceCache.delete(photoqueue.STARTING_KEY)
            with modelLock() as held:
                self.assertTrue(held)
                self.assertFalse(photoqueue.ensureWorker())
            self.assertEqual(popen.call_count, 1)

    def test_second_worker_not_load(self):
        self.job()
        loader = MagicMock()
        with modelLock() as held:
            self.assertTrue(held)
            self.assertEqual(runWorker(once=True, loader=loader), 0)
        loader.assert_not_called()

    def test_windows_lock_holds_unlocks(self):
        windows = MagicMock()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (
                patch("sellerpanel.device.Path", return_value=root),
                patch("sellerpanel.device.os.name", "nt"),
                patch.dict("sys.modules", {"msvcrt": windows}),
            ):
                with modelLock() as held:
                    self.assertTrue(held)
            self.assertEqual(windows.locking.call_args_list[0].args[1:], (windows.LK_NBLCK, 1))
            self.assertEqual(windows.locking.call_args_list[1].args[1:], (windows.LK_UNLCK, 1))


class PhotoClaimConcurrencyTests(TransactionTestCase):

    def test_two_claimers_get_disjoint(self):
        owner = makeUser("claim@test.com", "seller")
        for index in range(12):
            PhotoCheckJob.objects.create(owner=owner, room="kitchen", pendingPath=f"pending/{index}.jpg")
        barrier = Barrier(2)

        def claim():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                return {job.pk for job in photoqueue.claimJobs(6)}
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = list(executor.map(lambda index: claim(), range(2)))
        self.assertFalse(first & second)
        self.assertTrue(first | second)
        remaining = {job.pk for job in photoqueue.claimJobs(12)}
        self.assertEqual(len(first | second | remaining), 12)
        self.assertEqual(PhotoCheckJob.objects.filter(status="running", attempts=1).count(), 12)


@patch("django.db.close_old_connections", new=lambda: None)
class PhotoWorkerRestartTests(TestCase):

    def setUp(self):
        photoqueue.resourceCache.clear()
        self.owner = makeUser("restart@test.com", "seller")
        self.job = PhotoCheckJob.objects.create(
            owner=self.owner, room="kitchen", pendingPath="pending/not-present.jpg"
        )

    def watchExit(self, token):
        process = MagicMock()
        process.wait.return_value = 1
        with (
            patch.object(photoqueue, "ensureWorker") as restart,
            patch.object(photoqueue.time, "sleep") as sleep,
        ):
            photoqueue.watchWork(process, token)
        process.wait.assert_called_once_with()
        return (restart, sleep)

    def test_three_pre_claim_exits(self):
        for attempt, delay in ((1, 2), (2, 4), (3, None)):
            with self.subTest(attempt=attempt):
                restart, sleep = self.watchExit(str(attempt))
                self.assertEqual(photoqueue.resourceCache.get(photoqueue.STARTUP_FAILURE_KEY), attempt)
                if delay:
                    sleep.assert_called_once_with(delay)
                    restart.assert_called_once_with()
                else:
                    sleep.assert_not_called()
                    restart.assert_not_called()
        self.job.refresh_from_db()
        self.assertEqual(
            (self.job.status, self.job.attempts, self.job.result),
            ("failed", 0, {"unchecked": True}),
        )
        self.assertIn("could not start", self.job.error)
        self.assertIsNotNone(self.job.finishedAt)

    def test_polling_during_cooldown_not(self):
        for attempt in range(3):
            photoqueue.startFail()
        extra = PhotoCheckJob.objects.create(owner=self.owner, room="bedroom", pendingPath="pending/new.jpg")
        with patch.object(photoqueue.subprocess, "Popen") as popen:
            for poll in range(4):
                self.assertFalse(photoqueue.ensureWorker())
        popen.assert_not_called()
        extra.refresh_from_db()
        self.assertEqual((extra.status, extra.result, extra.attempts), ("failed", {"unchecked": True}, 0))

    def test_popen_failures_also_count(self):
        with (
            self.assertLogs("sellerpanel.photoqueue", level="ERROR"),
            patch.object(photoqueue.subprocess, "Popen", side_effect=OSError("cannot start")) as popen,
        ):
            self.assertFalse(photoqueue.ensureWorker())
            self.assertFalse(photoqueue.ensureWorker())
            self.assertEqual(popen.call_count, 1)
            for attempt in (2, 3):
                photoqueue.resourceCache.delete(photoqueue.RETRY_AT_KEY)
                self.assertFalse(photoqueue.ensureWorker())
                self.assertEqual(photoqueue.resourceCache.get(photoqueue.STARTUP_FAILURE_KEY), attempt)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "failed")

    def test_expired_cooldown_allows_new(self):
        photoqueue.resourceCache.set(photoqueue.RETRY_AT_KEY, 1, 300)
        with (
            patch.object(photoqueue.subprocess, "Popen") as popen,
            patch.object(photoqueue.threading, "Thread"),
        ):
            self.assertTrue(photoqueue.ensureWorker())
        token = photoqueue.resourceCache.get(photoqueue.STARTING_KEY)
        self.assertTrue(token)
        self.assertEqual(popen.call_args.kwargs["env"]["FLATFAIR_PHOTO_WORKER_TOKEN"], token)

    def test_claim_resets_startup_failures(self):
        photoqueue.resourceCache.set(photoqueue.STARTUP_FAILURE_KEY, 2, 300)
        photoqueue.resourceCache.set(photoqueue.RETRY_AT_KEY, 9999999999, 300)
        loader = MagicMock()
        with (
            self.assertLogs("sellerpanel.photoworker", level="WARNING"),
            patch.dict("os.environ", {"FLATFAIR_PHOTO_WORKER_TOKEN": "claimed"}),
            patch(
                "django.core.files.storage.default_storage.open",
                side_effect=OSError("missing photo"),
            ),
        ):
            self.assertEqual(runWorker(once=True, loader=loader), 1)
        loader.assert_not_called()
        self.assertIsNone(photoqueue.resourceCache.get(photoqueue.STARTUP_FAILURE_KEY))
        self.assertIsNone(photoqueue.resourceCache.get(photoqueue.RETRY_AT_KEY))
        restart, sleep = self.watchExit("claimed")
        self.assertIsNone(photoqueue.resourceCache.get(photoqueue.STARTUP_FAILURE_KEY))
        restart.assert_called_once_with()
        sleep.assert_not_called()

    def test_idle_not_reset_startup(self):
        self.job.delete()
        photoqueue.resourceCache.set(photoqueue.STARTUP_FAILURE_KEY, 2, 300)
        loader = MagicMock()
        self.assertEqual(runWorker(once=True, loader=loader), 0)
        self.assertEqual(photoqueue.resourceCache.get(photoqueue.STARTUP_FAILURE_KEY), 2)
        loader.assert_not_called()

    def test_another_starting_process_not(self):
        photoqueue.resourceCache.set(photoqueue.STARTING_KEY, "newer-process", 30)
        restart, sleep = self.watchExit("older-process")
        self.assertIsNone(photoqueue.resourceCache.get(photoqueue.STARTUP_FAILURE_KEY))
        self.assertEqual(photoqueue.resourceCache.get(photoqueue.STARTING_KEY), "newer-process")
        restart.assert_not_called()
        sleep.assert_not_called()

    def test_another_lock_not_count(self):
        with patch.object(photoqueue, "modelLock") as lock:
            lock.return_value.__enter__.return_value = False
            restart, sleep = self.watchExit("loser")
        self.assertIsNone(photoqueue.resourceCache.get(photoqueue.STARTUP_FAILURE_KEY))
        restart.assert_not_called()
        sleep.assert_not_called()


@patch("django.db.close_old_connections", new=lambda: None)
class PhotoWarmupTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("warm-seller@test.com", "seller")

    def test_opening_listing_form_starts(self):
        self.client.force_login(self.seller)
        with patch("sellerpanel.listings.warmWorker") as warm:
            self.client.get(reverse("sell"))
        warm.assert_called_once()

    def test_opening_edit_form_starts(self):
        flat = makeListing(self.seller)
        self.client.force_login(self.seller)
        with patch("sellerpanel.listings.warmWorker") as warm:
            self.client.get(reverse("edit_listing", args=[flat.publicId]))
        warm.assert_called_once()

    def test_tests_never_start_real(self):
        from sellerpanel import photoqueue

        with (
            patch("sellerpanel.photocheck.isOn", return_value=True),
            patch.object(photoqueue.subprocess, "Popen") as popen,
        ):
            self.assertFalse(photoqueue.warmWorker())
        popen.assert_not_called()

    @override_settings(PHOTO_WORKER_ON=True)
    def test_warming_starts_worker_even(self):
        from sellerpanel import photoqueue

        with (
            patch("sellerpanel.photocheck.isOn", return_value=True),
            patch.object(photoqueue.subprocess, "Popen") as popen,
            patch.object(photoqueue.threading, "Thread"),
        ):
            self.assertTrue(photoqueue.warmWorker())
        popen.assert_called_once()
        self.assertEqual(popen.call_args.kwargs["env"]["FLATFAIR_PHOTO_WARM"], "1")
        with patch.object(photoqueue.subprocess, "Popen") as popen:
            self.assertFalse(photoqueue.ensureWorker())
        popen.assert_not_called()

    @override_settings(PHOTO_WORKER_ON=True)
    def test_nothing_starts_checks_off(self):
        from sellerpanel import photoqueue

        with (
            patch("sellerpanel.photocheck.isOn", return_value=False),
            patch.object(photoqueue.subprocess, "Popen") as popen,
        ):
            self.assertFalse(photoqueue.warmWorker())
        popen.assert_not_called()

    def test_warm_worker_loads_model(self):
        import os
        from contextlib import contextmanager
        from sellerpanel import photoworker

        @contextmanager
        def freeLock(*args, **kwargs):
            yield True

        loaded = []
        held = patch("sellerpanel.device.modelLock", freeLock)
        held.start()
        self.addCleanup(held.stop)
        with patch.dict(os.environ, {"FLATFAIR_PHOTO_WARM": "1"}):
            photoworker.runWorker(once=True, loader=lambda: loaded.append(1) or (lambda paths: []))
        self.assertEqual(loaded, [1])
        loaded.clear()
        with patch.dict(os.environ, {"FLATFAIR_PHOTO_WARM": ""}):
            photoworker.runWorker(once=True, loader=lambda: loaded.append(1) or (lambda paths: []))
        self.assertEqual(loaded, [])


class PhotoSearchWarmupTests(TestCase):

    def test_opening_starts_checker(self):
        with patch("sellerpanel.photoqueue.warmWorker") as warm:
            self.client.get(reverse("lens"))
        warm.assert_called_once()

    def test_sample_not_need(self):
        with patch("sellerpanel.photoqueue.warmWorker") as warm:
            self.client.get(reverse("lens"), {"photo": "999999"})
        warm.assert_not_called()

    def test_keeps_waiting_checker_loading(self):
        from sellerpanel import photoqueue

        moments = iter([0, 0, 50, 50, 60, 60, 200, 200])
        with (
            patch.object(photoqueue.time, "monotonic", side_effect=lambda: next(moments, 999)),
            patch.object(photoqueue.time, "sleep"),
            patch.object(photoqueue, "ensureWorker", return_value=False),
            patch.object(photoqueue, "workerAlive", return_value=True) as alive,
        ):
            self.assertIsNone(photoqueue.checkNow([b"jpeg"], seconds=45))
        self.assertTrue(alive.called)
