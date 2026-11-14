from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from adminpanel.access import riskLevel, withRisk
from adminpanel.models import AuditLog, Decision, DocumentRequest
from adminpanel.moderation import photoWhy, heldFlats
from sellerpanel.photocheck import MODEL_VERSION
from storefront.models import ActivityNotice, Listing, ListingPhoto
from storefront.tests.helpers import CLEAN, makeListing, makeStaff, makeUser


class QueueTests(TestCase):

    def setUp(self):
        self.admin = makeStaff("admin@test.com")
        self.seller = makeUser("asdf@test.com", "seller")
        self.flat = makeListing(self.seller, status="review", askingPrice=700000)
        self.url = reverse("admin_decide", args=[self.flat.pk])
        self.client.force_login(self.admin)

    def test_explains_price_photo_flags(self):
        ListingPhoto.objects.create(
            listing=self.flat,
            room="kitchen",
            image="a.jpg",
            modelVersion=CLEAN["modelVersion"],
            aiLabel="selfie",
            aiIrrelevant=0.9,
            aiGenerated=0.01,
        )
        response = self.client.get(reverse("admin_queue"))
        row = response.context["rows"][0]
        self.assertEqual(row["flat"], self.flat)
        self.assertIn("The asking price is higher than similar flats sold for.", row["reasons"])
        self.assertIn("1 photo may not show the flat.", row["reasons"])
        self.assertEqual(row["outsideAmount"], 100000)
        self.assertIn("renovation receipts", row["askFor"])
        self.assertTrue(row["mailto"].startswith("mailto:asdf@test.com?subject="))
        self.assertEqual(row["doubtful"][0].staffReasons, "May not show the flat. It looks like a selfie.")

    def test_unavailable_individual_photo(self):
        for field, reason in [
            ("aiIrrelevant", "The room-photo check could not run."),
            ("aiGenerated", "The AI-image check could not run."),
        ]:
            with self.subTest(field=field):
                scores = {"aiIrrelevant": 0.01, "aiGenerated": 0.01, field: None}
                photo = ListingPhoto.objects.create(
                    listing=self.flat,
                    room="kitchen",
                    image="a.jpg",
                    modelVersion=CLEAN["modelVersion"],
                    **scores,
                )
                response = self.client.get(reverse("admin_queue"))
                row = response.context["rows"][0]
                self.assertIn("1 photo could not be checked automatically.", row["reasons"])
                self.assertEqual(row["doubtful"][0].staffReasons, reason)
                self.assertContains(self.client.get(reverse("admin_queue_item", args=[self.flat.pk])), reason)
                photo.staffChecked = True
                photo.save()
                response = self.client.get(reverse("admin_queue"))
                self.assertTrue(response.context["rows"][0]["manualPhotos"])
                photo.delete()

    def test_approval_publishes_listing(self):
        self.client.post(
            self.url,
            {"revision": self.flat.revision, "status": self.flat.status, "decision": "approved"},
        )
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "live")
        decision = Decision.objects.get()
        self.assertEqual(
            (decision.staff, decision.askingPrice, decision.bandHigh), (self.admin, 700000, 600000)
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="listing approved", target__startswith=f"Listing #{self.flat.pk}"
            ).exists()
        )

    def test_rejection_requires_reason_marks(self):
        response = self.client.post(
            self.url,
            {
                "revision": self.flat.revision,
                "status": self.flat.status,
                "decision": "rejected",
                "reason": "",
            },
        )
        self.assertEqual(response.url, reverse("admin_queue_item", args=[self.flat.pk]))
        self.assertFalse(Decision.objects.exists())
        item = self.client.get(response.url)
        self.assertTrue(item.context["r"]["missingReason"])
        self.assertContains(item, "Add a reason first, then reject or ask for documents.")
        self.assertFalse(self.client.get(response.url).context["r"]["missingReason"])
        self.client.post(
            self.url,
            {
                "revision": self.flat.revision,
                "status": self.flat.status,
                "decision": "rejected",
                "reason": "Fake photos",
            },
        )
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "rejected")

    def test_document_request_keeps_listing(self):
        self.client.post(
            self.url,
            {
                "revision": self.flat.revision,
                "status": self.flat.status,
                "decision": "documents",
                "reason": "Send lease papers",
            },
        )
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")
        row = self.client.get(reverse("admin_queue")).context["rows"][0]
        self.assertEqual(row["lastAsk"].reason, "Send lease papers")

    def test_duplicate_unknown_decisions(self):
        self.client.post(
            self.url,
            {"revision": self.flat.revision, "status": self.flat.status, "decision": "approved"},
        )
        self.client.post(
            self.url,
            {"revision": self.flat.revision, "status": self.flat.status, "decision": "approved"},
        )
        self.client.post(
            self.url,
            {"revision": self.flat.revision, "status": self.flat.status, "decision": "asdf"},
        )
        self.assertEqual(Decision.objects.count(), 1)

    def test_suspended_seller_listing_blocks(self):
        self.seller.is_active = False
        self.seller.save()
        self.client.post(
            self.url,
            {"revision": self.flat.revision, "status": self.flat.status, "decision": "approved"},
        )
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")

    def test_staff_approval_blocks_second(self):
        with patch("adminpanel.moderation.otherOpen", return_value=True):
            response = self.client.post(
                self.url,
                {
                    "revision": self.flat.revision,
                    "status": self.flat.status,
                    "decision": "approved",
                },
                follow=True,
            )
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")
        self.assertContains(response, "This seller already has an open flat.")
        self.assertFalse(Decision.objects.exists())
        self.assertFalse(ActivityNotice.objects.exists())

    def test_blocks_takedown_orders_oldest(self):
        self.client.post(
            self.url,
            {
                "revision": self.flat.revision,
                "status": self.flat.status,
                "decision": "takendown",
                "reason": "asdf",
            },
        )
        self.assertFalse(Decision.objects.exists())
        older = makeListing(makeUser("older@test.com", "seller"), status="review")
        Listing.objects.filter(pk=older.pk).update(reviewSince=timezone.now() - timedelta(days=3))
        rows = self.client.get(reverse("admin_queue"), {"sort": "wait"}).context["rows"]
        self.assertEqual([row["flat"].pk for row in rows], [older.pk, self.flat.pk])

    def test_approval_marks_photos_checked(self):
        photo = ListingPhoto.objects.create(
            listing=self.flat,
            room="kitchen",
            image="a.jpg",
            modelVersion=CLEAN["modelVersion"],
            aiIrrelevant=0.9,
            aiGenerated=0.01,
        )
        self.client.post(
            self.url,
            {"revision": self.flat.revision, "status": self.flat.status, "decision": "approved"},
        )
        photo.refresh_from_db()
        self.assertTrue(photo.staffChecked)
        notice = ActivityNotice.objects.get(user=self.seller)
        self.assertEqual(notice.kind, "listing_approved")
        self.assertIn("is live", notice.text)

    def test_rejection_notification_includes(self):
        self.client.post(
            self.url,
            {
                "revision": self.flat.revision,
                "status": self.flat.status,
                "decision": "rejected",
                "reason": "Fake photos",
            },
        )
        notice = ActivityNotice.objects.get(user=self.seller)
        self.assertEqual(notice.kind, "listing_rejected")
        self.assertTrue(notice.text.endswith(": Fake photos"))

    def test_overlong_reason_not_create(self):
        for decision in ("approved", "rejected", "documents"):
            with self.subTest(decision=decision):
                self.client.post(
                    self.url,
                    {
                        "revision": self.flat.revision,
                        "status": self.flat.status,
                        "decision": decision,
                        "reason": "x" * 201,
                    },
                )
                self.flat.refresh_from_db()
                self.assertEqual(self.flat.status, "review")
                self.assertFalse(Decision.objects.exists())
                self.assertFalse(AuditLog.objects.exists())
                self.assertFalse(ActivityNotice.objects.exists())

    def test_incomplete_price_band_shows(self):
        for field in ("bandLow", "bandHigh"):
            with self.subTest(field=field):
                self.flat.bandLow, self.flat.bandHigh = (500000, 600000)
                setattr(self.flat, field, None)
                self.flat.save()
                response = self.client.get(reverse("admin_queue"))
                self.assertContains(
                    self.client.get(reverse("admin_queue_item", args=[self.flat.pk])),
                    "No price check for this listing yet.",
                )
                self.assertIn(
                    "We could not check the price automatically.",
                    response.context["rows"][0]["reasons"],
                )


class ListingActionTests(TestCase):

    def setUp(self):
        self.admin = makeStaff("admin@test.com")
        self.seller = makeUser("asdf@test.com", "seller")
        self.flat = makeListing(
            self.seller, town="BEDOK", block="157", postalCode="460157", leaseCommenceYear=1985
        )
        self.url = reverse("admin_listing_action", args=[self.flat.pk])
        self.client.force_login(self.admin)

    def test_filters_cover_price_block(self):
        makeListing(self.seller, askingPrice=700000, status="hidden")
        response = self.client.get(reverse("admin_listings"))
        self.assertEqual(len(response.context["rows"]), 2)
        self.assertEqual(len(self.client.get(reverse("admin_listings"), {"above": "1"}).context["rows"]), 1)
        self.assertEqual(len(self.client.get(reverse("admin_listings"), {"q": "blk 157"}).context["rows"]), 1)
        self.assertEqual(
            len(self.client.get(reverse("admin_listings"), {"town": "BEDOK"}).context["rows"]), 1
        )
        response = self.client.get(reverse("admin_listings"), {"seller": self.seller.pk})
        self.assertEqual(response.context["seller"], self.seller)

    def test_takedown_requires_reason(self):
        self.client.post(self.url, {"action": "takedown", "reason": ""})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "live")
        self.client.post(self.url, {"action": "takedown", "reason": "Duplicate listing"})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "hidden")
        notice = ActivityNotice.objects.get(user=self.seller)
        self.assertEqual(notice.kind, "listing_taken_down")
        self.assertIn("Duplicate listing", notice.text)
        self.assertEqual(Decision.objects.get().get_decision_display(), "Taken down")

    def test_relist_review_update_status(self):
        self.flat.status = "hidden"
        self.flat.save()
        self.client.post(self.url, {"action": "relist"})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "live")
        self.assertEqual(ActivityNotice.objects.get(user=self.seller).kind, "listing_relisted")
        self.client.post(self.url, {"action": "review"})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")
        self.assertTrue(AuditLog.objects.filter(action="listing sent to review").exists())

    def test_staff_relist_blocks_second(self):
        self.flat.status = "hidden"
        self.flat.save()
        makeListing(self.seller, status="review")
        response = self.client.post(self.url, {"action": "relist"}, follow=True)
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "hidden")
        self.assertContains(response, "This seller already has an open flat.")
        self.assertFalse(Decision.objects.exists())
        self.assertFalse(ActivityNotice.objects.exists())

    def test_staff_review_blocks_second(self):
        self.flat.status = "hidden"
        self.flat.save()
        other = makeListing(self.seller)
        for status in ("hidden", "rejected"):
            with self.subTest(status=status):
                self.flat.status = status
                self.flat.save()
                response = self.client.post(self.url, {"action": "review"}, follow=True)
                self.flat.refresh_from_db()
                self.assertEqual(self.flat.status, status)
                self.assertContains(response, "This seller already has an open flat.")
        other.refresh_from_db()
        self.assertEqual(other.status, "live")
        self.assertFalse(AuditLog.objects.exists())

    def test_takedown_keeps_sold_status(self):
        self.flat.status = "sold"
        self.flat.save()
        self.client.post(self.url, {"action": "takedown", "reason": "asdf"})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "sold")
        self.flat.status = "hidden"
        self.flat.save()
        self.seller.is_active = False
        self.seller.save()
        self.client.post(self.url, {"action": "relist"})
        self.assertEqual(Listing.objects.get(pk=self.flat.pk).status, "hidden")

    def test_keeps_return_filters_anchor(self):
        response = self.client.post(self.url, {"action": "review", "returnQuery": "status=live&page=2"})
        self.assertEqual(
            response.url, reverse("admin_listings") + f"?status=live&page=2#listing-{self.flat.pk}"
        )

    def test_edit_revalues_changed_features(self):
        url = reverse("admin_listing_edit", args=[self.flat.pk])
        data = {
            "revision": self.flat.revision,
            "askingPrice": "650000",
            "floorAreaSqm": "92",
            "storey": "8",
            "flatModel": "Model A",
        }
        with patch("adminpanel.moderation.band") as band:
            self.client.post(url, data)
            band.assert_not_called()
            band.return_value = (510000, 560000, 610000)
            self.client.post(url, {**data, "storey": "12", "revision": self.flat.revision + 1})
        self.flat.refresh_from_db()
        self.assertEqual(
            (self.flat.askingPrice, self.flat.storey, self.flat.estimatedPrice),
            (650000, 12, 560000),
        )
        details = list(
            AuditLog.objects.filter(action="listing edited").order_by("pk").values_list("detail", flat=True)
        )
        self.assertEqual(details, ["Changed asking price ($)", "Changed floor level"])
        response = self.client.post(url, {**data, "askingPrice": "10"})
        self.assertIn("askingPrice", response.context["form"].errors)

    def test_staff_seller_flat_address(self):
        url = reverse("admin_listing_edit", args=[self.flat.pk])
        data = {
            "revision": self.flat.revision,
            "askingPrice": "650000",
            "floorAreaSqm": "92",
            "storey": "8",
            "flatModel": "Model A",
        }
        cases = [
            ("flatModel", "Maisonette"),
            ("floorAreaSqm", "150"),
            ("floorAreaSqm", "24"),
            ("floorAreaSqm", "301"),
            ("askingPrice", "49999"),
            ("askingPrice", "3000001"),
            ("storey", "0"),
            ("storey", "51"),
        ]
        for field, value in cases:
            with (
                self.subTest(field=field, value=value),
                patch("adminpanel.moderation.band") as band,
            ):
                response = self.client.post(url, {**data, field: value})
                self.assertIn(field, response.context["form"].errors)
                band.assert_not_called()
        self.flat.refresh_from_db()
        self.assertEqual((self.flat.askingPrice, self.flat.revision), (550000, 0))
        self.assertFalse(AuditLog.objects.exists())

    def test_staff_blocks_inconsistent(self):
        self.flat.block = "999"
        self.flat.save()
        response = self.client.post(
            reverse("admin_listing_edit", args=[self.flat.pk]),
            {
                "revision": self.flat.revision,
                "askingPrice": "650000",
                "floorAreaSqm": "92",
                "storey": "8",
                "flatModel": "Model A",
            },
        )
        self.assertContains(response, "Check the block number or the postal code.")
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.askingPrice, 550000)
        self.assertFalse(AuditLog.objects.exists())

    def test_valuation_failure_leaves(self):
        original = Listing.objects.values().get(pk=self.flat.pk)
        with patch("adminpanel.moderation.band", side_effect=ValueError("unavailable")):
            response = self.client.post(
                reverse("admin_listing_edit", args=[self.flat.pk]),
                {
                    "revision": self.flat.revision,
                    "askingPrice": "650000",
                    "floorAreaSqm": "95",
                    "storey": "12",
                    "flatModel": "Model A",
                },
            )
        self.assertContains(response, "The valuation service is unavailable. Nothing was saved.")
        self.assertEqual(Listing.objects.values().get(pk=self.flat.pk), original)
        self.assertFalse(AuditLog.objects.exists())

    def test_two_staff_tabs_blocks(self):
        url = reverse("admin_listing_edit", args=[self.flat.pk])
        revision = self.client.get(url).context["form"]["revision"].value()
        self.assertContains(self.client.get(url), 'type="hidden" name="revision"')
        data = {
            "revision": revision,
            "askingPrice": "650000",
            "floorAreaSqm": "92",
            "storey": "8",
            "flatModel": "Model A",
        }
        self.client.post(url, data)
        with patch("adminpanel.moderation.band") as band:
            response = self.client.post(url, {**data, "askingPrice": "700000"})
        band.assert_not_called()
        self.assertContains(response, "This listing changed in another tab")
        self.flat.refresh_from_db()
        self.assertEqual((self.flat.askingPrice, self.flat.revision), (650000, revision + 1))
        self.assertEqual(AuditLog.objects.filter(action="listing edited").count(), 1)

    def test_seller_price_change_invalidates(self):
        url = reverse("admin_listing_edit", args=[self.flat.pk])
        revision = self.client.get(url).context["form"]["revision"].value()
        self.flat.askingPrice = 620000
        self.flat.save(update_fields=["askingPrice"])
        response = self.client.post(
            url,
            {
                "revision": revision,
                "askingPrice": "650000",
                "floorAreaSqm": "92",
                "storey": "8",
                "flatModel": "Model A",
            },
        )
        self.assertContains(response, "Reload the page")
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.askingPrice, 620000)

    def test_review_timestamp_tracks_entry(self):
        self.flat.createdAt = timezone.now() - timedelta(days=30)
        self.flat.save()
        self.client.post(self.url, {"action": "review"})
        self.flat.refresh_from_db()
        self.assertGreater(self.flat.reviewSince, timezone.now() - timedelta(minutes=1))
        self.flat.status = "live"
        self.flat.save()
        self.assertIsNone(self.flat.reviewSince)

    def test_edit_form_displays_integer(self):
        response = self.client.get(reverse("admin_listing_edit", args=[self.flat.pk]))
        self.assertContains(response, 'value="92"')
        self.assertNotContains(response, 'value="92.0"')
        self.assertContains(self.client.get(reverse("admin_listings")), "0 views")

    def test_photo_reasons_friendly_scene(self):
        photo = ListingPhoto(
            listing=self.flat,
            room="kitchen",
            aiLabel="other_place",
            aiIrrelevant=0.99,
            aiGenerated=0.01,
            modelVersion=CLEAN["modelVersion"],
        )
        self.assertEqual(
            photoWhy(photo), ["May not show the flat. It looks like an office or shop, not a home."]
        )
        photo.aiLabel = "empty_room"
        self.assertEqual(photoWhy(photo), ["May not show the flat. It looks like an empty room."])


class HoldTests(TestCase):

    def setUp(self):
        self.admin = makeStaff("admin@test.com")
        self.seller = makeUser("asdf@test.com", "seller")
        self.client.force_login(self.admin)

    def reasons(self):
        return self.client.get(reverse("admin_queue")).context["rows"][0]["reasons"]

    def test_very_low_price_shows(self):
        makeListing(self.seller, status="review", askingPrice=300000)
        self.assertEqual(
            self.reasons(),
            ["The asking price is much lower than similar flats, please check for a typo."],
        )

    def test_unavailable_estimate_shows(self):
        makeListing(self.seller, status="review", estimatedPrice=None)
        self.assertEqual(self.reasons(), ["We could not check the price automatically."])

    def test_unflagged_review_shows_neutral(self):
        makeListing(self.seller, status="review")
        self.assertIn("No price or photo flag.", self.reasons()[0])

    def test_reactivation_clears_staff(self):
        flat = makeListing(self.seller)
        url = reverse("admin_customer_suspend", args=[self.seller.pk])
        self.client.post(url, {"action": "suspend", "reason": "asdf"})
        self.client.post(url, {"action": "reactivate", "reason": "Account checked"})
        flat.refresh_from_db()
        self.assertEqual(flat.status, "review")
        self.assertIn("The seller was suspended and is active again", self.reasons()[0])
        self.seller.profile.refresh_from_db()
        self.assertIsNotNone(self.seller.profile.reactivatedAt)
        self.client.post(
            reverse("admin_decide", args=[flat.pk]),
            {"revision": flat.revision, "status": flat.status, "decision": "approved"},
        )
        flat.status = "review"
        flat.save()
        self.assertNotIn("The seller was suspended and is active again", " ".join(self.reasons()))


class QueueReasonGroupTests(TestCase):

    def setUp(self):
        self.staff = makeStaff("groups-admin@test.com")
        self.client.force_login(self.staff)
        self.priced = [self.flat(number, 700000) for number in range(3)]
        self.photo = self.flat(3, 700000, aiGenerated=0.95)
        self.other = self.flat(4, 550000)

    def flat(self, number, price, **scores):
        flat = makeListing(
            makeUser(f"groups-seller{number}@test.com", "seller"),
            status="review",
            askingPrice=price,
        )
        ListingPhoto.objects.create(
            listing=flat,
            room="kitchen",
            image="room.jpg",
            modelVersion=CLEAN["modelVersion"],
            **{"aiIrrelevant": 0.01, "aiGenerated": 0.01, **scores},
        )
        return flat

    def test_tabs_count_filter(self):
        response = self.client.get(reverse("admin_queue"))
        self.assertEqual(
            response.context["reasonCounts"],
            {"all": 5, "price": 3, "photos": 1, "other": 1, "documents": 0},
        )
        for reason, expected in (
            ("price", self.priced),
            ("photos", [self.photo]),
            ("other", [self.other]),
        ):
            with self.subTest(reason=reason):
                response = self.client.get(reverse("admin_queue"), {"reason": reason})
                self.assertEqual(
                    {row["flat"].pk for row in response.context["rows"]},
                    {flat.pk for flat in expected},
                )
        self.assertEqual(
            self.client.get(reverse("admin_queue"), {"reason": "bad"}).context["rows"].paginator.count,
            5,
        )

    def test_documents_ready_tab_lists(self):
        DocumentRequest.objects.create(
            listing=self.priced[0],
            requestedBy=self.staff,
            message="Please upload your HDB letter.",
            status="submitted",
        )
        DocumentRequest.objects.create(
            listing=self.photo,
            requestedBy=self.staff,
            message="Please upload your HDB letter.",
            status="requested",
        )
        response = self.client.get(reverse("admin_queue"), {"reason": "documents"})
        self.assertEqual([row["flat"].pk for row in response.context["rows"]], [self.priced[0].pk])
        self.assertEqual(response.context["reasonCounts"]["documents"], 1)
        self.assertContains(response, "Documents ready <span>1</span>")

    def test_bulk_approval_asks_first(self):
        page = self.client.get(reverse("admin_queue")).content.decode()
        self.assertIn(
            'data-ask="Approve the listings you ticked? They go live for buyers straight away."',
            page,
        )
        self.assertIn(
            'data-ask="Approve all 5 listings on this page? They go live for buyers straight away."',
            page,
        )

    def test_page_size_twenty_five(self):
        self.assertEqual(self.client.get(reverse("admin_queue")).context["rows"].paginator.per_page, 25)
        self.assertEqual(
            self.client.get(reverse("admin_queue"), {"per": "100"}).context["rows"].paginator.per_page,
            100,
        )
        self.assertEqual(
            self.client.get(reverse("admin_queue"), {"per": "5000"}).context["rows"].paginator.per_page,
            25,
        )

    def test_approve_on_page_rows(self):
        page = self.client.get(reverse("admin_queue"), {"reason": "price"})
        self.assertContains(page, "Approve all 3 on this page")
        data = {"scope": "page", "back": "?reason=price"}
        for flat in self.priced:
            data[f"revision-{flat.pk}"] = flat.revision
            data[f"status-{flat.pk}"] = "review"
        response = self.client.post(reverse("admin_bulk_approve"), data)
        self.assertRedirects(response, reverse("admin_queue") + "?reason=price")
        self.assertEqual(Listing.objects.filter(status="live").count(), 3)
        self.assertEqual(
            {self.photo.pk, self.other.pk},
            set(Listing.objects.filter(status="review").values_list("pk", flat=True)),
        )
        self.assertEqual(AuditLog.objects.filter(action="listing approved", actor=self.staff).count(), 3)

    def test_back_link_blocks_leave(self):
        response = self.client.post(reverse("admin_bulk_approve"), {"back": "//evil.example"})
        self.assertRedirects(response, reverse("admin_queue"))


class PhotoDecideTests(TestCase):

    def setUp(self):
        self.media = TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        setting = override_settings(MEDIA_ROOT=self.media.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.admin = makeStaff("photo-admin@test.com")
        self.seller = makeUser("photo-seller@test.com", "seller")
        self.flat = makeListing(self.seller, status="review")
        self.bad = ListingPhoto.objects.create(
            listing=self.flat,
            room="kitchen",
            image=SimpleUploadedFile("k.jpg", b"x"),
            modelVersion=MODEL_VERSION,
            aiIrrelevant=0.01,
            aiGenerated=0.99,
        )
        self.other = ListingPhoto.objects.create(
            listing=self.flat,
            room="bedroom_2",
            image=SimpleUploadedFile("b.jpg", b"x"),
            modelVersion=MODEL_VERSION,
            aiIrrelevant=0.01,
            aiGenerated=0.99,
        )
        self.client.force_login(self.admin)

    def url(self, photo):
        return reverse("admin_photo_decide", args=[self.flat.pk, photo.pk])

    def test_flagged_own_keep_remove(self):
        page = self.client.get(reverse("admin_queue_item", args=[self.flat.pk]))
        self.assertContains(page, self.url(self.bad))
        self.assertContains(page, self.url(self.other))
        self.assertContains(page, "Looks computer-made")

    def test_removing_one_tells_seller(self):
        response = self.client.post(self.url(self.bad), {"action": "refuse", "reason": "ai"}, follow=True)
        self.assertContains(response, "The kitchen photo is removed and the seller is told why.")
        self.assertFalse(ListingPhoto.objects.filter(pk=self.bad.pk).exists())
        self.assertTrue(ListingPhoto.objects.filter(pk=self.other.pk).exists())
        notice = ActivityNotice.objects.get(user=self.seller, kind="photo_refused")
        self.assertIn("removed the kitchen photo", notice.text)
        self.assertIn("because it looks computer-made", notice.text)
        self.assertTrue(AuditLog.objects.filter(action="photo removed").exists())

    def test_keeping_clears_flag(self):
        self.client.post(self.url(self.other), {"action": "keep"})
        self.other.refresh_from_db()
        self.assertEqual(self.other.staffReviewedBy, self.admin)
        page = self.client.get(reverse("admin_queue_item", args=[self.flat.pk]))
        self.assertContains(page, "1 photo to check")

    def test_removing_needs_a_reason(self):
        response = self.client.post(self.url(self.bad), {"action": "refuse"}, follow=True)
        self.assertContains(response, "Choose why you’re removing this photo.")
        self.assertTrue(ListingPhoto.objects.filter(pk=self.bad.pk).exists())


class RiskTests(TestCase):

    def setUp(self):
        self.admin = makeStaff("risk-admin@test.com")
        self.client.force_login(self.admin)

    def flat(self, email, asking, **fields):
        seller = makeUser(email, "seller")
        return makeListing(
            seller,
            status="review",
            askingPrice=asking,
            estimatedPrice=550000,
            bandLow=500000,
            bandHigh=600000,
            postalCode=fields.pop("postalCode", "760123"),
            **fields,
        )

    def score(self, flat):
        return withRisk(heldFlats()).get(pk=flat.pk).risk

    def test_price_photos_history_add(self):
        price = self.flat("risk-a@test.com", 660000)
        self.assertEqual(self.score(price), 26)
        photos = self.flat("risk-b@test.com", 600000)
        ListingPhoto.objects.create(
            listing=photos,
            room="kitchen",
            image="a.jpg",
            modelVersion=MODEL_VERSION,
            aiIrrelevant=0.01,
            aiGenerated=0.99,
        )
        ListingPhoto.objects.create(
            listing=photos,
            room="bedroom_2",
            image="b.jpg",
            modelVersion=MODEL_VERSION,
            aiIrrelevant=0.01,
            aiGenerated=0.99,
        )
        self.assertEqual(self.score(photos), 40)
        earlier = makeListing(price.seller, status="rejected", postalCode="760125", block="125")
        Decision.objects.create(
            listing=earlier,
            staff=self.admin,
            decision="rejected",
            reason="Fake photos",
            askingPrice=earlier.askingPrice,
            estimatedPrice=550000,
            bandLow=500000,
            bandHigh=600000,
            modelVersion="v5.3",
        )
        self.assertEqual(self.score(price), 36)
        self.assertEqual([riskLevel(5), riskLevel(26), riskLevel(60)], ["low", "medium", "high"])

    def test_queue_puts_riskiest_first(self):
        low = self.flat("risk-c@test.com", 640000)
        high = self.flat("risk-d@test.com", 700000)
        ListingPhoto.objects.create(
            listing=high,
            room="kitchen",
            image="a.jpg",
            modelVersion=MODEL_VERSION,
            aiIrrelevant=0.01,
            aiGenerated=0.99,
        )
        rows = self.client.get(reverse("admin_queue")).context["rows"]
        self.assertEqual([row["flat"].pk for row in rows][:2], [high.pk, low.pk])
        page = self.client.get(reverse("admin_queue_item", args=[high.pk]))
        for check_text_1 in (
            "Risk score 60 of 100 · High",
            "1 photo may be computer-made",
            "Price 17% above the fair range",
        ):
            self.assertContains(page, check_text_1)

    def test_very_low_listings_go(self):
        safe = self.flat("risk-e@test.com", 610000)
        response = self.client.get(reverse("admin_queue"), follow=True)
        safe.refresh_from_db()
        self.assertEqual(safe.status, "live")
        self.assertContains(response, "1 low-risk listing went live automatically.")
        self.assertTrue(
            Decision.objects.filter(
                listing=safe, decision="approved", reason__startswith="Risk score"
            ).exists()
        )
