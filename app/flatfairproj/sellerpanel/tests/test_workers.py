import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from adminpanel.models import AuditLog, Decision, DocumentRequest
from sellerpanel import photocheck, device, photoqueue
from sellerpanel.photocheck import MODEL_VERSION
from sellerpanel.photoworker import IDLE_SECONDS, MAX_JOBS, MAX_LIFETIME_SECONDS, runWorker
from storefront.listing_rules import liveIssue
from storefront.models import ListingPhoto, PhotoCheckJob
from storefront.tests.helpers import (
    CLEAN,
    FAIR,
    FLAT,
    ROOMS,
    makeListing,
    makeStaff,
    makeUser,
    jpeg,
)


class PublicationVersionTests(TestCase):

    def setUp(self):
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        setting = override_settings(MEDIA_ROOT=media.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.seller = makeUser("seller@test.com", "seller")
        self.staff = makeStaff("staff@test.com")
        self.client.force_login(self.staff)
        self.flat = makeListing(self.seller, status="review")
        self.photo = ListingPhoto.objects.create(
            listing=self.flat,
            room="kitchen",
            aiIrrelevant=0.01,
            aiGenerated=0.01,
            staffChecked=True,
            modelVersion="old-model",
        )
        self.photo.image.save("room.jpg", ContentFile(b"mock room photo"))
        self.url = reverse("admin_decide", args=[self.flat.pk])

    @patch("sellerpanel.photocheck.verifyModel", new=lambda *args, **kwargs: "verified")
    def test_staff_approval_reruns_stale(self):
        for version in [None, "old-model"]:
            with self.subTest(version=version):
                self.flat.status = "review"
                self.flat.save()
                self.photo.modelVersion = version
                self.photo.save()
                done = SimpleNamespace(returncode=0, stdout=json.dumps([CLEAN]), stderr="")
                with (
                    patch("sellerpanel.photocheck.isOn", return_value=True),
                    patch("sellerpanel.photocheck.subprocess.run", return_value=done) as run,
                ):
                    self.client.post(
                        self.url,
                        {
                            "revision": self.flat.revision,
                            "status": self.flat.status,
                            "decision": "approved",
                        },
                    )
                run.assert_called_once()
                self.flat.refresh_from_db()
                self.photo.refresh_from_db()
                self.assertEqual(self.flat.status, "live")
                self.assertEqual(self.photo.modelVersion, MODEL_VERSION)
                self.assertTrue(self.photo.staffChecked)
                self.assertIsNone(self.photo.staffReviewedBy)
                self.assertFalse(AuditLog.objects.filter(action="photos manually approved").exists())

    def test_unavailable_wrong_recheck_keeps(self):
        for result in [None, {**CLEAN, "modelVersion": "old-model"}]:
            with (
                self.subTest(result=result),
                patch("sellerpanel.photos.checkPics", return_value=[result]) as check,
            ):
                response = self.client.post(
                    self.url,
                    {
                        "revision": self.flat.revision,
                        "status": self.flat.status,
                        "decision": "approved",
                    },
                    follow=True,
                )
                check.assert_called_once_with([b"mock room photo"])
                self.flat.refresh_from_db()
                self.photo.refresh_from_db()
                self.assertEqual(self.flat.status, "review")
                self.assertIsNone(self.photo.modelVersion)
                self.assertIsNone(self.photo.aiIrrelevant)
                self.assertIsNone(self.photo.aiGenerated)
                self.assertFalse(self.photo.staffChecked)
                self.assertFalse(Decision.objects.exists())
                for check_text_1 in (
                    "Photo checks are out of date and could not be rerun.",
                    "I have looked at every photo; approve them manually",
                    'name="manualPhotos"',
                ):
                    self.assertContains(response, check_text_1)
                self.assertIsNone(self.photo.staffReviewedAt)
                self.assertFalse(AuditLog.objects.filter(action="photos manually approved").exists())

    def test_manual_approval_records_photo(self):
        extra = ListingPhoto.objects.create(
            listing=self.flat,
            room="living_room",
            modelVersion=MODEL_VERSION,
            aiIrrelevant=0.01,
            aiGenerated=0.01,
        )
        extra.image.save("living.jpg", ContentFile(b"mock living photo"))
        revision = self.flat.revision
        with (
            patch("sellerpanel.photocheck.isOn", return_value=False),
            patch("sellerpanel.photocheck.verifyModel", return_value="not installed"),
            patch("sellerpanel.photos.checkPics", return_value=[None]) as check,
        ):
            response = self.client.post(
                self.url,
                {
                    "revision": revision,
                    "status": "review",
                    "decision": "approved",
                    "manualPhotos": "on",
                },
                follow=True,
            )
        check.assert_called_once_with([b"mock room photo"])
        self.flat.refresh_from_db()
        self.assertEqual((self.flat.status, self.flat.revision), ("live", revision))
        self.assertContains(response, "Buyers can now see it.")
        for photo in self.flat.photos.all():
            self.assertEqual(photo.staffReviewedBy, self.staff)
            self.assertIsNotNone(photo.staffReviewedAt)
            self.assertEqual(photo.staffReviewedRevision, revision)
            self.assertTrue(photo.staffChecked)
        entry = AuditLog.objects.get(action="photos manually approved")
        self.assertEqual(entry.actor, self.staff)
        self.assertIn(f"Listing #{self.flat.pk}", entry.target)
        self.assertIn(f"revision {revision}: 2 photos", entry.detail)
        self.assertEqual(liveIssue(self.flat, revision, "live"), "")

    def manuallyApprove(self, result=None):
        with patch("sellerpanel.photos.checkPics", return_value=[result]):
            return self.client.post(
                self.url,
                {
                    "revision": self.flat.revision,
                    "status": "review",
                    "decision": "approved",
                    "manualPhotos": "on",
                },
                follow=True,
            )

    def test_identity_mismatch_requires(self):
        self.photo.modelVersion = MODEL_VERSION
        self.photo.aiGenerated = None
        self.photo.save()
        result = {"modelVersion": MODEL_VERSION, "identityStatus": "identity mismatch"}
        with (
            patch("sellerpanel.photocheck.isOn", return_value=True),
            patch("sellerpanel.photocheck.verifyModel", return_value="identity mismatch"),
        ):
            response = self.client.post(
                self.url,
                {"revision": self.flat.revision, "status": "review", "decision": "approved"},
                follow=True,
            )
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")
        self.assertFalse(Decision.objects.exists())
        self.assertContains(response, 'name="manualPhotos"')
        self.manuallyApprove(result)
        self.flat.refresh_from_db()
        self.photo.refresh_from_db()
        self.assertEqual(self.flat.status, "live")
        self.assertEqual(self.photo.staffReviewedBy, self.staff)

    def test_manual_checkbox_not_override(self):
        with patch("sellerpanel.photos.checkPics", return_value=[CLEAN]) as check:
            self.client.post(
                self.url,
                {
                    "revision": self.flat.revision,
                    "status": "review",
                    "decision": "approved",
                    "manualPhotos": "on",
                },
            )
        check.assert_called_once()
        self.flat.refresh_from_db()
        self.photo.refresh_from_db()
        self.assertEqual(self.flat.status, "live")
        self.assertEqual(self.photo.modelVersion, MODEL_VERSION)
        self.assertIsNone(self.photo.staffReviewedBy)
        self.assertFalse(AuditLog.objects.filter(action="photos manually approved").exists())

    def test_scored_photos_missing_current(self):
        for version in [None, "old-model"]:
            with self.subTest(version=version):
                self.photo.modelVersion = version
                self.photo.save()
                response = self.client.get(reverse("admin_queue_item", args=[self.flat.pk]))
                self.assertContains(response, 'name="manualPhotos"')

    def test_verified_photos_not_offer(self):
        self.photo.modelVersion = MODEL_VERSION
        self.photo.staffChecked = False
        self.photo.aiIrrelevant = 0.9
        self.photo.save()
        response = self.client.get(reverse("admin_queue_item", args=[self.flat.pk]))
        self.assertNotContains(response, 'name="manualPhotos"')
        self.assertContains(response, self.photo.publicUrl)

    def test_seller_edit_invalidates_manual(self):
        for room in ROOMS:
            if room != self.photo.room:
                photo = ListingPhoto.objects.create(
                    listing=self.flat,
                    room=room,
                    modelVersion=MODEL_VERSION,
                    aiIrrelevant=0.01,
                    aiGenerated=0.01,
                )
                photo.image.save(f"{room}.jpg", ContentFile(b"mock room photo"))
        self.manuallyApprove()
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "live")
        revision = self.flat.revision
        self.client.force_login(self.seller)
        url = reverse("edit_listing", args=[self.flat.publicId])
        token = self.client.get(url).context["draftToken"]
        with (
            patch("sellerpanel.views.band", return_value=FAIR),
            patch("sellerpanel.photos.checkPics", return_value=[]),
        ):
            response = self.client.post(
                url,
                {
                    **FLAT,
                    "revision": revision,
                    "draftToken": token,
                    "askingPrice": "410000",
                    "confirm": "1",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.flat.refresh_from_db()
        self.photo.refresh_from_db()
        self.assertEqual(self.flat.status, "review")
        self.assertEqual(self.flat.revision, revision + 1)
        self.assertEqual(self.photo.staffReviewedRevision, revision)
        self.assertIn("Photo checks", liveIssue(self.flat, self.flat.revision, "review"))

    def test_photo_replacement_invalidates(self):
        self.manuallyApprove()
        self.flat.refresh_from_db()
        self.photo.refresh_from_db()
        self.photo.image.save("replacement.jpg", ContentFile(b"replacement photo"))
        self.photo.refresh_from_db()
        self.assertIsNone(self.photo.staffReviewedBy)
        self.assertIsNone(self.photo.staffReviewedAt)
        self.assertIsNone(self.photo.staffReviewedRevision)
        self.assertIn("Photo checks", liveIssue(self.flat, self.flat.revision, "live"))

    def test_photo_queryset_replacement(self):
        self.manuallyApprove()
        self.flat.refresh_from_db()
        self.flat.photos.update(image="replacement.jpg")
        self.photo.refresh_from_db()
        self.assertIsNone(self.photo.staffReviewedBy)
        self.assertIn("Photo checks", liveIssue(self.flat, self.flat.revision, "live"))

    def test_photo_changed_during_refresh(self):

        def changePhoto(photos):
            self.flat.photos.update(image="changed.jpg")
            return [None]

        with patch("sellerpanel.photos.checkPics", side_effect=changePhoto):
            response = self.client.post(
                self.url,
                {
                    "revision": self.flat.revision,
                    "status": "review",
                    "decision": "approved",
                    "manualPhotos": "on",
                },
                follow=True,
            )
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")
        self.assertContains(response, "changed during photo checks")
        self.assertFalse(Decision.objects.exists())
        self.assertFalse(AuditLog.objects.filter(action="photos manually approved").exists())

    def test_missing_photo_blocks_approved(self):
        self.photo.image.delete(save=False)
        self.manuallyApprove()
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")
        self.assertFalse(Decision.objects.exists())

    def test_unresolved_documents_blocks(self):
        DocumentRequest.objects.create(listing=self.flat, requestedBy=self.staff, message="Lease papers")
        self.manuallyApprove()
        self.flat.refresh_from_db()
        self.photo.refresh_from_db()
        self.assertEqual(self.flat.status, "review")
        self.assertIsNone(self.photo.staffReviewedBy)
        self.assertFalse(AuditLog.objects.filter(action="photos manually approved").exists())

    def test_staff_relisting_honours_current(self):
        self.manuallyApprove()
        self.flat.refresh_from_db()
        self.flat.status = "hidden"
        self.flat.save(update_fields=["status"])
        with patch("sellerpanel.photos.checkPics", return_value=[None]):
            self.client.post(reverse("admin_listing_action", args=[self.flat.pk]), {"action": "relist"})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "live")
        self.assertEqual(AuditLog.objects.filter(action="photos manually approved").count(), 1)

    def test_staff_relisting_blocks_reuse(self):
        self.manuallyApprove()
        self.flat.refresh_from_db()
        self.flat.status = "hidden"
        self.flat.description = "Changed details"
        self.flat.save(update_fields=["description", "status"])
        with patch("sellerpanel.photos.checkPics", return_value=[None]):
            response = self.client.post(
                reverse("admin_listing_action", args=[self.flat.pk]),
                {"action": "relist"},
                follow=True,
            )
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")
        self.assertContains(response, "On the listing&#x27;s review page, tick")
        self.assertContains(
            self.client.get(reverse("admin_queue_item", args=[self.flat.pk])), 'name="manualPhotos"'
        )

    def test_missing_stale_photo_stays(self):
        self.photo.image.delete(save=False)
        with patch("sellerpanel.photos.checkPics", return_value=[]) as check:
            self.client.post(
                self.url,
                {
                    "revision": self.flat.revision,
                    "status": self.flat.status,
                    "decision": "approved",
                },
            )
        check.assert_called_once_with([])
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")
        self.assertFalse(Decision.objects.exists())

    def test_current_photo_keeps_explicit(self):
        self.photo.modelVersion = MODEL_VERSION
        self.photo.aiIrrelevant = 0.9
        self.photo.save()
        with patch("sellerpanel.photos.checkPics") as check:
            self.client.post(
                self.url,
                {
                    "revision": self.flat.revision,
                    "status": self.flat.status,
                    "decision": "approved",
                },
            )
        check.assert_not_called()
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "live")

    def test_seller_relisting_sold_flat(self):
        self.flat.status = "sold"
        self.flat.statusBeforeSold = "live"
        self.flat.save()
        self.client.force_login(self.seller)
        self.client.post(reverse("toggle_sold", args=[self.flat.publicId]), {"sold": "0"})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")

    def test_staff_relisting_hidden_flat(self):
        self.flat.status = "hidden"
        self.flat.save()
        with patch("sellerpanel.photos.checkPics", return_value=[None]):
            response = self.client.post(
                reverse("admin_listing_action", args=[self.flat.pk]),
                {"action": "relist"},
                follow=True,
            )
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")
        self.assertFalse(Decision.objects.exists())
        self.assertContains(response, "Photo checks are out of date and could not be rerun.")

    def test_staff_relisting_blocks(self):
        self.flat.status = "hidden"
        self.flat.save()
        self.photo.modelVersion = MODEL_VERSION
        self.photo.save()
        documents = DocumentRequest.objects.create(
            listing=self.flat, requestedBy=self.staff, message="Lease papers"
        )
        for status in ("requested", "submitted", "rejected"):
            with self.subTest(status=status):
                documents.status = status
                documents.save()
                response = self.client.post(
                    reverse("admin_listing_action", args=[self.flat.pk]),
                    {"action": "relist"},
                    follow=True,
                )
                self.flat.refresh_from_db()
                self.assertEqual(self.flat.status, "hidden")
                self.assertContains(
                    response,
                    "Review and accept the submitted documents before approving this listing.",
                )
                self.assertFalse(Decision.objects.exists())
        documents.status = "accepted"
        documents.save()
        self.client.post(reverse("admin_listing_action", args=[self.flat.pk]), {"action": "relist"})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "live")

    def test_seller_unselling_blocks_bypass(self):
        self.flat.status = "sold"
        self.flat.statusBeforeSold = "live"
        self.flat.save()
        self.photo.modelVersion = MODEL_VERSION
        self.photo.save()
        DocumentRequest.objects.create(listing=self.flat, requestedBy=self.staff, message="Lease papers")
        self.client.force_login(self.seller)
        self.client.post(reverse("toggle_sold", args=[self.flat.publicId]), {"sold": "0"})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")

    def test_guard_blocks_changed_revision(self):
        from storefront.listing_rules import liveIssue

        self.assertIn("changed", liveIssue(self.flat, self.flat.revision + 1, "review"))
        self.assertIn("changed", liveIssue(self.flat, self.flat.revision, "hidden"))
        self.photo.modelVersion = MODEL_VERSION
        self.photo.staffChecked = False
        self.photo.aiGenerated = None
        self.photo.save()
        self.assertIn("Photo checks", liveIssue(self.flat, self.flat.revision, "review"))
        self.assertIn("Photo checks", liveIssue(self.flat, self.flat.revision, "review", staffAccepted=True))
        self.photo.aiGenerated = 0.01
        self.photo.save()
        self.assertEqual(liveIssue(self.flat, self.flat.revision, "review", staffAccepted=True), "")


def answer(paths):
    return [dict(CLEAN, label=f"asdf{number}") for number in range(len(paths))]


@patch("django.db.close_old_connections", new=lambda: None)
class WarmWorkerTests(TestCase):

    def setUp(self):
        photoqueue.resourceCache.clear()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.marker = Path(folder.name) / "gpu_disabled_until.txt"
        marker = patch.object(device, "MARKER", self.marker)
        marker.start()
        self.addCleanup(marker.stop)
        self.photo = jpeg().read()

    def job(self):
        path = default_storage.save(f"pending/{time.time_ns()}.jpg", ContentFile(self.photo))
        return PhotoCheckJob.objects.create(
            owner=makeUser(f"{time.time_ns()}@test.com", "seller"), room="kitchen", pendingPath=path
        )

    def worker(self, loader):
        return patch.object(
            photoqueue, "ensureWorker", side_effect=lambda: runWorker(once=True, loader=loader)
        )

    def test_model_reused_photo_batch(self):
        loader = MagicMock(return_value=answer)
        with (
            self.worker(loader) as start,
            patch.object(default_storage, "delete", wraps=default_storage.delete) as delete,
        ):
            results = photoqueue.checkNow([self.photo, self.photo])
        self.assertEqual([result["label"] for result in results], ["asdf0", "asdf1"])
        start.assert_called_once_with()
        loader.assert_called_once_with()
        self.assertFalse(PhotoCheckJob.objects.exists())
        self.assertEqual(delete.call_count, 2)
        for call in delete.call_args_list:
            self.assertFalse(default_storage.exists(call.args[0]))

    def test_reuses_model_later_jobs(self):
        loader = MagicMock(return_value=answer)
        self.job()
        later = []

        def idle(seconds):
            if not later:
                later.append(self.job())
                later.append(self.job())

        self.assertEqual(runWorker(idleSeconds=0.2, loader=loader, sleep=idle), 3)
        loader.assert_called_once_with()
        self.assertEqual(PhotoCheckJob.objects.filter(status="done").count(), 3)
        self.assertIsNone(photoqueue.resourceCache.get(photoqueue.HEARTBEAT_KEY))

    def test_photo_wait_timeout_clears(self):
        with (
            patch.object(photoqueue, "ensureWorker") as start,
            self.assertLogs("sellerpanel.photoqueue", "WARNING"),
        ):
            self.assertIsNone(photoqueue.checkNow([self.photo], seconds=0.3))
        self.assertGreaterEqual(start.call_count, 1)
        self.assertFalse(PhotoCheckJob.objects.exists())

    def test_failed_photo_job_is_removed(self):

        def crash():
            PhotoCheckJob.objects.filter(status="queued").update(status="failed", result={"unchecked": True})

        with (
            patch.object(photoqueue, "ensureWorker", side_effect=crash),
            self.assertLogs("sellerpanel.photoqueue", "WARNING"),
        ):
            self.assertIsNone(photoqueue.checkNow([self.photo]))
        with (
            patch.object(PhotoCheckJob.objects, "create", side_effect=RuntimeError("asdf")),
            self.assertLogs("sellerpanel.photoqueue", "ERROR"),
        ):
            self.assertIsNone(photoqueue.checkNow([self.photo]))
        self.assertFalse(PhotoCheckJob.objects.exists())

    def checks(self, worker, atomic=False):
        child = SimpleNamespace(returncode=0, stdout='[{"label": "qwer"}]')
        with (
            override_settings(PHOTO_WORKER_ON=True),
            patch.object(photocheck, "isOn", return_value=True),
            patch.object(photocheck, "verifyModel", return_value="verified"),
            patch.object(photocheck, "connection", SimpleNamespace(in_atomic_block=atomic)),
            patch.object(photoqueue, "checkNow", return_value=worker) as asked,
            patch.object(photocheck.subprocess, "run", return_value=child) as run,
        ):
            return (photocheck.checkPics([self.photo]), asked, run)

    def test_result_skips_new_process(self):
        results, asked, run = self.checks([{"label": "asdf"}])
        self.assertEqual(results, [{"label": "asdf"}])
        asked.assert_called_once()
        run.assert_not_called()

    def test_missing_cpu_process(self):
        results, asked, run = self.checks(None)
        self.assertEqual(results, [{"label": "qwer"}])
        asked.assert_called_once()
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["env"]["CUDA_VISIBLE_DEVICES"], "")

    def test_transaction_skips(self):
        results, asked, run = self.checks([{"label": "asdf"}], atomic=True)
        self.assertEqual(results, [{"label": "qwer"}])
        asked.assert_not_called()
        with (
            patch.object(photocheck, "isOn", return_value=True),
            patch.object(photocheck, "verifyModel", return_value="verified"),
            patch.object(photoqueue, "checkNow") as asked,
            patch.object(
                photocheck.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout="[{}]"),
            ),
        ):
            self.assertEqual(photocheck.checkPics([self.photo]), [{}])
        asked.assert_not_called()

    def test_gpu_timeout_switches_to_cpu(self):
        slow = subprocess.TimeoutExpired("python", photocheck.TIMEOUT)
        with (
            override_settings(FLATFAIR_GPU="1"),
            patch.object(photocheck, "isOn", return_value=True),
            patch.object(photocheck, "verifyModel", return_value="verified"),
        ):
            with (
                patch.object(photocheck.subprocess, "run", side_effect=slow) as run,
                self.assertLogs("sellerpanel", "ERROR"),
            ):
                self.assertEqual(photocheck.checkPics([self.photo]), [None])
            self.assertEqual(run.call_args.kwargs["env"]["CUDA_VISIBLE_DEVICES"], "1")
            self.assertTrue(device.gpuOff())
            with patch.object(
                photocheck.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout="[{}]"),
            ) as run:
                self.assertEqual(photocheck.checkPics([self.photo]), [{}])
            self.assertEqual(run.call_args.kwargs["env"]["CUDA_VISIBLE_DEVICES"], "")
        self.marker.unlink()
        with (
            patch.object(photocheck, "isOn", return_value=True),
            patch.object(photocheck, "verifyModel", return_value="verified"),
            patch.object(photocheck.subprocess, "run", side_effect=slow),
            self.assertLogs("sellerpanel", "ERROR"),
        ):
            self.assertEqual(photocheck.checkPics([self.photo]), [None])
        self.assertFalse(self.marker.exists())

    def test_stuck_gpu_worker_is_killed(self):
        self.job()
        with (
            override_settings(FLATFAIR_GPU="1"),
            patch.object(photoqueue.subprocess, "Popen") as popen,
            patch.object(photoqueue.threading, "Thread") as thread,
        ):
            self.assertTrue(photoqueue.ensureWorker())
        self.assertEqual(popen.call_args.kwargs["env"]["CUDA_VISIBLE_DEVICES"], "1")
        self.assertEqual(thread.call_args.kwargs["args"][2], 1)
        process = MagicMock()
        process.wait.side_effect = [subprocess.TimeoutExpired("python", 1), 0]
        with (
            patch.object(photoqueue, "ensureWorker"),
            patch.object(photoqueue.time, "sleep"),
            self.assertLogs("sellerpanel.device", "ERROR"),
        ):
            photoqueue.watchWork(process, "asdf", 1)
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_args_list[0].kwargs, {"timeout": photoqueue.STUCK_SECONDS})
        self.assertTrue(device.gpuOff())

    def test_gpu_model_error_leaves(self):

        def broken(paths):
            raise RuntimeError("CUDA asdf")

        first = self.job()
        self.job()
        with self.assertLogs("sellerpanel", "ERROR"):
            self.assertEqual(runWorker(idleSeconds=0, maxJobs=1, loader=lambda: broken), 1)
        self.assertFalse(device.gpuOff())
        with self.assertLogs("sellerpanel", "ERROR"):
            self.assertEqual(runWorker(idleSeconds=0, loader=lambda: broken, onGpu=True), 0)
        self.assertTrue(device.gpuOff())
        first.refresh_from_db()
        self.assertIn(first.status, ("queued", "failed"))

    def test_lock_dead_cleanup(self):
        self.assertFalse(photoqueue.workerAlive())
        with photoqueue.modelLock() as held:
            self.assertTrue(held)
            self.assertTrue(photoqueue.workerAlive())
        photoqueue.resourceCache.set(photoqueue.STARTING_KEY, "asdf", 30)
        self.assertTrue(photoqueue.workerAlive())
        photoqueue.resourceCache.delete(photoqueue.STARTING_KEY)

        def dies():
            PhotoCheckJob.objects.filter(status="queued").update(status="running")
            return False

        began = time.monotonic()
        with (
            patch.object(photoqueue, "ensureWorker", side_effect=dies),
            self.assertLogs("sellerpanel.photoqueue", "WARNING"),
        ):
            self.assertIsNone(photoqueue.checkNow([self.photo], seconds=30))
        self.assertLess(time.monotonic() - began, 5)
        self.assertFalse(PhotoCheckJob.objects.exists())

    def test_worker_limits_fit_timeout(self):
        self.assertLessEqual(IDLE_SECONDS, 300)
        self.assertLessEqual(MAX_JOBS, 200)
        self.assertLessEqual(MAX_LIFETIME_SECONDS, 900)
        self.assertGreater(photoqueue.STUCK_SECONDS, MAX_LIFETIME_SECONDS + photocheck.TIMEOUT)


def smi(text):
    return SimpleNamespace(returncode=0, stdout=text)


class DeviceTests(SimpleTestCase):

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.marker = Path(folder.name) / "gpu_disabled_until.txt"
        marker = patch.object(device, "MARKER", self.marker)
        marker.start()
        self.addCleanup(marker.stop)

    def test_pick_gpu_with_free_memory(self):
        with patch.object(
            device.subprocess, "run", return_value=smi("0, 19484, 24564\n1, 1501, 8192\n")
        ) as run:
            self.assertEqual(device.pickGpu("auto"), 1)
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        with patch.object(device.subprocess, "run", return_value=smi("0, 500, 24564\n1, 1501, 8192\n")):
            self.assertEqual(device.pickGpu("auto"), 0)
        with patch.object(device.subprocess, "run", return_value=smi("0, 500, 24564\n1, 1501, 8192\n")):
            self.assertEqual(device.pickGpu("asdf"), 0)

    def test_pick_gpu_needs_enough(self):
        with patch.object(device.subprocess, "run", return_value=smi("0, 19484, 24564\n1, 5500, 8192\n")):
            self.assertIsNone(device.pickGpu("auto"))
        with patch.object(device.subprocess, "run", return_value=smi("0, 19484, 24564\n1, 1501, 8192\n")):
            self.assertEqual(device.pickGpu("auto", 5000), 1)
            self.assertIsNone(device.pickGpu("auto", 7000))
        with patch.object(device.subprocess, "run", return_value=smi("0, 13000, 16000\n")):
            self.assertIsNone(device.pickGpu("auto"))

    def test_manual_gpu_skips_scan(self):
        with patch.object(device.subprocess, "run") as run:
            self.assertIsNone(device.pickGpu("off"))
            self.assertIsNone(device.pickGpu(" OFF "))
            self.assertEqual(device.pickGpu("1"), 1)
            self.assertEqual(device.pickGpu(0), 0)
        run.assert_not_called()

    def test_disabled_gpu_waits_marker(self):
        with self.assertLogs("sellerpanel.device", "ERROR"):
            device.disableGpu("asdf")
        self.assertGreater(float(self.marker.read_text()), time.time() + 5 * 3600)
        with patch.object(device.subprocess, "run", return_value=smi("0, 500, 24564\n")) as run:
            self.assertIsNone(device.pickGpu("auto"))
            self.assertIsNone(device.pickGpu("0"))
            run.assert_not_called()
            self.marker.write_text(str(time.time() - 1))
            self.assertEqual(device.pickGpu("auto"), 0)
            self.marker.write_text("qwerty")
            self.assertEqual(device.pickGpu("0"), 0)

    def test_gpu_scan_bad_output(self):
        for problem in (
            FileNotFoundError("nvidia-smi"),
            subprocess.TimeoutExpired("nvidia-smi", 5),
        ):
            with (
                patch.object(device.subprocess, "run", side_effect=problem),
                self.assertLogs("sellerpanel.device", "INFO"),
            ):
                self.assertIsNone(device.pickGpu("auto"))
        for text in ("asdf\n", "0, N/A, 24564\n", ""):
            with patch.object(device.subprocess, "run", return_value=smi(text)):
                self.assertIsNone(device.pickGpu("auto"))

    def test_torch_falls_back_cpu(self):
        self.assertEqual(device.gpuVars(None), {"CUDA_VISIBLE_DEVICES": ""})
        self.assertEqual(device.gpuVars(1), {"CUDA_VISIBLE_DEVICES": "1", "CUDA_DEVICE_ORDER": "PCI_BUS_ID"})
        torch = MagicMock()
        with (
            patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": ""}),
            patch.dict("sys.modules", {"torch": torch}),
        ):
            self.assertEqual(device.startDevice(), "cpu")
            self.assertEqual(device.gpuUsedMb(), 0)
        torch.zeros.assert_not_called()
        with (
            patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": "1"}),
            patch.dict("sys.modules", {"torch": torch}),
        ):
            torch.cuda.memory_reserved.return_value = 3 * 2**20
            self.assertEqual(device.startDevice(), "cuda")
            self.assertEqual(device.gpuUsedMb(), 3)
            torch.zeros.return_value.to.side_effect = RuntimeError("CUDA asdf")
            with self.assertLogs("sellerpanel.device", "WARNING") as logs:
                self.assertEqual(device.startDevice(), "cpu")
        self.assertEqual(len(logs.output), 1)

    def test_photo_gpu_uses_setting(self):
        with patch.object(device.subprocess, "run") as run:
            with override_settings(FLATFAIR_GPU="auto"):
                self.assertIsNone(photocheck.photoGpu())
            with override_settings(FLATFAIR_GPU="off"):
                self.assertIsNone(photocheck.photoGpu())
            with override_settings(FLATFAIR_GPU="1"):
                self.assertEqual(photocheck.photoGpu(), 1)
                self.marker.write_text(str(time.time() + 60))
                self.assertIsNone(photocheck.photoGpu())
        run.assert_not_called()
