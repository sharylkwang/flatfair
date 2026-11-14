from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from PIL import Image

from adminpanel.documents import MAX_DOCUMENT_SIZE
from adminpanel.models import AuditLog, Decision, DocumentFile, DocumentRequest
from flatfair.urls import media
from storefront.models import ActivityNotice, ListingPhoto
from storefront.tests.helpers import CLEAN, jpeg, makeListing, makeStaff, makeUser
from storefront.tests.helpers import closeResponse


def pdf(name="lease.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF", content_type="application/pdf")


def png():
    output = BytesIO()
    Image.new("RGB", (16, 16), "white").save(output, "PNG")
    return SimpleUploadedFile("receipt.png", output.getvalue(), content_type="image/png")


class DocumentWorkflowTests(TestCase):

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = override_settings(
            MEDIA_ROOT=Path(directory.name) / "media",
            PRIVATE_DOCUMENT_ROOT=Path(directory.name) / "private",
        )
        settings.enable()
        self.addCleanup(settings.disable)
        self.admin = makeStaff("documentadmin@test.com")
        self.otherAdmin = makeStaff("documentadmin2@test.com", "Admin")
        self.seller = makeUser("documentowner@test.com", "seller")
        self.other = makeUser("documentother@test.com", "seller")
        self.flat = makeListing(self.seller, status="review")
        self.decisionUrl = reverse("admin_decide", args=[self.flat.pk])
        self.client.force_login(self.admin)
        self.client.post(
            self.decisionUrl,
            {
                "revision": self.flat.revision,
                "status": self.flat.status,
                "decision": "documents",
                "reason": "Send the HDB lease papers.",
            },
        )
        self.documents = DocumentRequest.objects.get()
        self.uploadUrl = reverse("submit_documents", args=[self.documents.pk])
        self.client.force_login(self.seller)

    def upload(self, files=None):
        return self.client.post(self.uploadUrl, {"documents": files if files is not None else [pdf()]})

    def review(self, choice, reason=""):
        self.flat.refresh_from_db()
        self.documents.refresh_from_db()
        self.client.force_login(self.admin)
        return self.client.post(
            self.decisionUrl,
            {
                "revision": self.flat.revision,
                "status": self.flat.status,
                "documentVersion": self.documents.updatedAt.isoformat(),
                "decision": choice,
                "reason": reason,
                "documentRequest": self.documents.pk,
            },
        )

    def test_asking_creates_request_reason(self):
        self.assertEqual(self.documents.listing, self.flat)
        self.assertEqual(self.documents.requestedBy, self.admin)
        self.assertEqual(self.documents.message, "Send the HDB lease papers.")
        self.assertEqual(self.documents.status, "requested")
        self.assertIsNone(self.documents.submittedAt)
        self.assertEqual(Decision.objects.get().decision, "documents")
        self.assertTrue(AuditLog.objects.filter(actor=self.admin, action="documents requested").exists())
        notice = ActivityNotice.objects.get(user=self.seller)
        self.assertEqual(notice.url, reverse("my_listings") + f"#listing-{self.flat.publicId}")
        response = self.client.get(reverse("my_listings"))
        self.assertContains(response, "Documents requested. Open the listing to upload them.")
        self.assertContains(response, reverse("seller_listing", args=[self.flat.publicId]))
        self.assertNotContains(response, self.uploadUrl)
        response = self.client.get(reverse("seller_listing", args=[self.flat.publicId]))
        self.assertContains(response, "Documents requested")
        self.assertContains(response, self.documents.message)
        self.assertContains(response, self.uploadUrl)
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")

    def test_seller_submits_pdf_jpg(self):
        secondAdmin = makeStaff("secondadmin@test.com")
        inactiveAdmin = makeStaff("inactiveadmin@test.com")
        inactiveAdmin.is_active = False
        inactiveAdmin.save()
        self.otherAdmin.groups.clear()
        self.upload([pdf(), jpeg(), png()])
        self.documents.refresh_from_db()
        self.assertEqual(self.documents.status, "submitted")
        self.assertIsNotNone(self.documents.submittedAt)
        self.assertEqual(self.documents.files.count(), 3)
        self.assertEqual(
            set(self.documents.files.values_list("contentType", flat=True)),
            {"application/pdf", "image/jpeg", "image/png"},
        )
        staffNotices = ActivityNotice.objects.filter(user__is_staff=True)
        self.assertEqual(
            set(staffNotices.values_list("user_id", flat=True)),
            {self.admin.pk, self.otherAdmin.pk, secondAdmin.pk},
        )
        self.assertTrue(
            all((notice.url == reverse("admin_queue") + f"#queue-{self.flat.pk}" for notice in staffNotices))
        )
        self.assertTrue(
            AuditLog.objects.filter(
                actor=self.seller, action="documents submitted", detail="3 files"
            ).exists()
        )
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_queue"))
        self.assertContains(response, "Documents submitted")
        self.assertNotContains(response, "Accept documents and approve")
        response = self.client.get(reverse("admin_queue_item", args=[self.flat.pk]))
        self.assertContains(response, "Documents submitted")
        self.assertContains(response, "Accept documents and approve")
        self.assertContains(response, reverse("document_file", args=[self.documents.files.first().pk]))
        self.assertContains(
            self.client.get(reverse("admin_overview")), "1 document submission ready to check"
        )
        self.client.force_login(self.seller)
        self.assertContains(self.client.get(reverse("my_listings")), "Documents submitted, being checked")
        response = self.client.get(reverse("seller_listing", args=[self.flat.publicId]))
        self.assertContains(response, "Documents submitted")
        self.assertNotContains(response, "Submit documents</button>")
        count = ActivityNotice.objects.count()
        self.assertEqual(self.upload().status_code, 404)
        self.assertEqual(ActivityNotice.objects.count(), count)

    def test_upload_blocks_extensions_mime(self):
        invalidFiles = [
            SimpleUploadedFile("lease.html", b"%PDF-1.4", content_type="application/pdf"),
            SimpleUploadedFile("lease.pdf", b"%PDF-1.4", content_type="text/html"),
            SimpleUploadedFile("lease.pdf", b"<script>bad</script>", content_type="application/pdf"),
            SimpleUploadedFile("lease.jpg", b"\x89PNG\r\n\x1a\n", content_type="image/jpeg"),
            SimpleUploadedFile("lease.jpg", b"\xff\xd8\xffbroken", content_type="image/jpeg"),
            SimpleUploadedFile("lease.png", b"\x89PNG\r\n\x1a\nbroken", content_type="image/png"),
            SimpleUploadedFile("lease.pdf", b"", content_type="application/pdf"),
        ]
        for invalid in invalidFiles:
            with self.subTest(name=invalid.name, contentType=invalid.content_type):
                response = self.upload([pdf(), invalid])
                self.assertEqual(response.status_code, 302)
                self.assertFalse(DocumentFile.objects.exists())
                self.documents.refresh_from_db()
                self.assertEqual(self.documents.status, "requested")
        self.assertFalse(ActivityNotice.objects.filter(user__is_staff=True).exists())
        self.assertFalse(AuditLog.objects.filter(action="documents submitted").exists())

    def test_upload_requires_one_five(self):
        for files in (
            [],
            [pdf(f"lease{index}.pdf") for index in range(6)],
            [
                SimpleUploadedFile(
                    "large.pdf", b"%PDF-" + b"x" * MAX_DOCUMENT_SIZE, content_type="application/pdf"
                )
            ],
        ):
            with self.subTest(count=len(files)):
                self.upload(files)
                self.assertFalse(DocumentFile.objects.exists())
        exactSize = SimpleUploadedFile(
            "exact.pdf", b"%PDF-" + b"x" * (MAX_DOCUMENT_SIZE - 5), content_type="application/pdf"
        )
        self.upload([exactSize, pdf(), pdf(), pdf(), pdf()])
        self.assertEqual(DocumentFile.objects.count(), 5)
        self.assertEqual(DocumentFile.objects.get(originalName="exact.pdf").size, MAX_DOCUMENT_SIZE)

    def test_other_sellers_staff_guests(self):
        for user in (self.other, self.admin, self.otherAdmin):
            self.client.force_login(user)
            self.assertEqual(self.upload().status_code, 404)
        self.client.logout()
        self.assertEqual(self.upload().status_code, 302)
        self.assertFalse(DocumentFile.objects.exists())
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get(self.uploadUrl).status_code, 405)

    def test_downloads_allow_owner_admin(self):
        self.upload()
        document = DocumentFile.objects.get()
        url = reverse("document_file", args=[document.pk])
        self.assertNotIn("media/", document.file.path)
        with self.assertRaises(ValueError):
            document.file.url
        for user in (self.seller, self.admin, self.otherAdmin):
            self.client.force_login(user)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(b"".join(response.streaming_content).startswith(b"%PDF-"))
            closeResponse(response)
            self.assertEqual(response["Cache-Control"], "private, no-store")
            self.assertEqual(response["X-Content-Type-Options"], "nosniff")
            self.assertEqual(response["Content-Type"], "application/pdf")
            self.assertIn("attachment;", response["Content-Disposition"])
            self.assertTrue(AuditLog.objects.filter(actor=user, action="document downloaded").exists())
        for user in (self.other, makeUser("documentbuyer@test.com")):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 404)
        self.client.logout()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("signin"), response.url)
        request = RequestFactory().get("/media/" + document.file.name)
        with self.assertRaises(Http404):
            media(request, document.file.name)
        document.file.storage.delete(document.file.name)
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_accepting_approves_listing(self):
        photo = ListingPhoto.objects.create(
            listing=self.flat,
            room="kitchen",
            image="room.jpg",
            modelVersion=CLEAN["modelVersion"],
            aiIrrelevant=0.9,
            aiGenerated=0.01,
        )
        self.upload()
        self.review("approved")
        self.documents.refresh_from_db()
        self.flat.refresh_from_db()
        photo.refresh_from_db()
        self.assertEqual((self.documents.status, self.flat.status), ("accepted", "live"))
        self.assertIsNotNone(self.documents.reviewedAt)
        self.assertTrue(photo.staffChecked)
        self.assertTrue(Decision.objects.filter(decision="approved").exists())
        self.assertTrue(ActivityNotice.objects.filter(user=self.seller, kind="listing_approved").exists())
        for action in ("documents accepted", "listing approved"):
            self.assertTrue(AuditLog.objects.filter(actor=self.admin, action=action).exists())
        noticeCount = ActivityNotice.objects.count()
        self.review("approved")
        self.assertEqual(ActivityNotice.objects.count(), noticeCount)
        self.client.force_login(self.seller)
        self.assertEqual(self.upload().status_code, 404)

    def test_rejecting_requires_reason(self):
        self.upload([pdf(), jpeg()])
        oldFiles = list(DocumentFile.objects.all())
        self.review("rejected")
        self.documents.refresh_from_db()
        self.assertEqual(self.documents.status, "submitted")
        self.assertFalse(AuditLog.objects.filter(action="documents rejected").exists())
        self.review("rejected", "Please include the owner name.")
        self.documents.refresh_from_db()
        self.flat.refresh_from_db()
        self.assertEqual((self.documents.status, self.flat.status), ("rejected", "review"))
        self.assertEqual(self.documents.staffReason, "Please include the owner name.")
        self.assertIsNotNone(self.documents.reviewedAt)
        self.assertTrue(
            AuditLog.objects.filter(
                actor=self.admin, action="documents rejected", detail=self.documents.staffReason
            ).exists()
        )
        self.assertTrue(
            ActivityNotice.objects.filter(
                user=self.seller, text__contains=self.documents.staffReason
            ).exists()
        )
        self.client.force_login(self.seller)
        self.assertContains(
            self.client.get(reverse("my_listings")),
            "Upload again. Open the listing to upload them.",
        )
        self.assertContains(
            self.client.get(reverse("seller_listing", args=[self.flat.publicId])),
            self.documents.staffReason,
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.upload([pdf("replacement.pdf")])
        self.documents.refresh_from_db()
        self.assertEqual(self.documents.status, "submitted")
        self.assertEqual(self.documents.staffReason, "")
        self.assertIsNone(self.documents.reviewedAt)
        self.assertEqual(
            list(self.documents.files.values_list("originalName", flat=True)), ["replacement.pdf"]
        )
        for document in oldFiles:
            self.assertFalse(document.file.storage.exists(document.file.name))
        self.assertEqual(ActivityNotice.objects.filter(user=self.admin).count(), 2)

    def test_acceptance_keeps_suspension(self):
        self.upload()
        self.seller.is_active = False
        self.seller.save()
        self.review("approved")
        self.seller.is_active = True
        self.seller.save()
        with patch("adminpanel.moderation.otherOpen", return_value=True):
            self.review("approved")
        with patch("adminpanel.moderation.redoStale", return_value=False):
            self.review("approved")
        self.documents.refresh_from_db()
        self.flat.refresh_from_db()
        self.assertEqual((self.documents.status, self.flat.status), ("submitted", "review"))
        self.assertFalse(AuditLog.objects.filter(action="documents accepted").exists())
        self.assertFalse(ActivityNotice.objects.filter(kind="listing_approved").exists())

    def test_staff_blocks_bypass_review(self):
        self.client.force_login(self.admin)
        self.client.post(
            self.decisionUrl,
            {"revision": self.flat.revision, "status": self.flat.status, "decision": "approved"},
        )
        self.review("approved")
        self.client.force_login(self.seller)
        self.upload()
        self.client.force_login(self.admin)
        self.client.post(
            self.decisionUrl,
            {
                "revision": self.flat.revision,
                "status": self.flat.status,
                "decision": "approved",
                "documentRequest": self.documents.pk + 1,
            },
        )
        self.client.post(
            self.decisionUrl,
            {
                "revision": self.flat.revision,
                "status": self.flat.status,
                "decision": "rejected",
                "documentRequest": self.documents.pk,
                "reason": "x" * 201,
            },
        )
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.post(
                self.decisionUrl, {"decision": "approved", "documentRequest": self.documents.pk}
            ).status_code,
            403,
        )
        self.documents.refresh_from_db()
        self.flat.refresh_from_db()
        self.assertEqual((self.documents.status, self.flat.status), ("submitted", "review"))
        self.assertFalse(Decision.objects.filter(decision="approved").exists())
        self.assertFalse(
            AuditLog.objects.filter(action__in=("documents accepted", "documents rejected")).exists()
        )

    def test_repeated_requests_keep_one(self):
        self.client.force_login(self.admin)
        self.flat.refresh_from_db()
        self.client.post(
            self.decisionUrl,
            {
                "revision": self.flat.revision,
                "status": self.flat.status,
                "decision": "documents",
                "reason": "Send a clearer lease.",
            },
        )
        self.documents.refresh_from_db()
        self.assertEqual(DocumentRequest.objects.count(), 1)
        self.assertEqual(self.documents.message, "Send a clearer lease.")
        self.client.force_login(self.seller)
        self.upload()
        self.assertEqual(DocumentFile.objects.count(), 1)

    def test_invalid_replacement_keeps(self):
        self.upload()
        oldFile = DocumentFile.objects.get()
        self.review("rejected", "Send a clearer copy.")
        self.client.force_login(self.seller)
        self.upload([SimpleUploadedFile("wrong.pdf", b"wrong", content_type="application/pdf")])
        self.documents.refresh_from_db()
        self.assertEqual(self.documents.status, "rejected")
        self.assertEqual(DocumentFile.objects.get().pk, oldFile.pk)
        self.assertTrue(oldFile.file.storage.exists(oldFile.file.name))
