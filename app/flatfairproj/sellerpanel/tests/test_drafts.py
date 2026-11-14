from datetime import timedelta
from unittest.mock import patch

from django.contrib.messages import get_messages
from django.core.files.storage import default_storage
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .helpers import CompletedPhotoQueueMixin
from sellerpanel.models import ListingDraft
from sellerpanel.photos import DRAFTS_KEY, sweepPics
from storefront import address
from storefront.models import Listing
from storefront.tests.helpers import CLEAN, FAIR, FLAT, ROOMS, jpeg, makeListing, makeUser


class ListingDraftTests(CompletedPhotoQueueMixin, TestCase):

    def setUp(self):
        self.seller = makeUser("draft@test.com", "seller")
        self.client.force_login(self.seller)
        self.token = self.client.get(reverse("sell")).context["draftToken"]
        checker = patch(
            "sellerpanel.photos.checkPics", side_effect=lambda photos: [dict(CLEAN)] * len(photos)
        )
        self.checker = checker.start()
        self.addCleanup(checker.stop)
        price = patch("sellerpanel.views.band", return_value=FAIR)
        self.band = price.start()
        self.addCleanup(price.stop)

    def test_fresh_form_keeps_model(self):
        response = self.client.get(reverse("sell"))
        self.assertEqual(response.context["form"]["flatModel"].value(), "")
        self.assertIsNone(response.context["form"]["leaseCommenceYear"].value())

    def test_discard_requires_opening(self):
        self.saveDraft(description="Keep this draft")
        response = self.client.get(reverse("sell"))
        content = response.content.decode()
        confirmation = content.split('<details class="draft-discard">', 1)[1].split("</details>", 1)[0]
        self.assertInHTML('<summary class="btn btn-danger">Discard draft</summary>', confirmation)
        self.assertInHTML("<p>This removes the saved details and photos for this draft.</p>", confirmation)
        self.assertInHTML(
            '<button class="btn btn-danger" name="action" value="discardDraft" formnovalidate>Discard this draft</button>',
            confirmation,
        )
        self.assertEqual(content.count('value="discardDraft"'), 1)
        self.assertEqual(ListingDraft.objects.get().formData["description"], "Keep this draft")

    def saveDraft(self, **data):
        return self.client.post(
            reverse("sell"), {"draftToken": self.token, "action": "saveDraft", **data}, follow=True
        )

    def checkPhoto(self, room="kitchen"):
        response = self.client.post(
            reverse("photo_check"), {"draftToken": self.token, "room": room, "photo": jpeg()}
        )
        self.assertEqual(response.json()["verdict"], "accept")
        return self.client.session[DRAFTS_KEY][self.token]["photos"][room]

    def newSession(self):
        self.client = Client()
        self.client.force_login(self.seller)
        return self.client.get(reverse("sell"))

    def test_missing_pending_photo_returns(self):
        path = self.checkPhoto()
        default_storage.delete(path)
        response = self.client.post(
            reverse("sell"),
            {
                **FLAT,
                "draftToken": self.token,
                **{f"photo_{room}": jpeg() for room in ROOMS if room != "kitchen"},
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Listing.objects.exists())
        slot = next((slot for slot in response.context["photoSlots"] if slot["key"] == "kitchen"))
        self.assertIn("Please choose the photo again", slot["error"])
        data = self.client.session[DRAFTS_KEY][self.token]
        self.assertNotIn("kitchen", data["photos"])
        self.assertNotIn("kitchen", data["checks"])

    def test_save_keeps_incomplete_invalid(self):
        from sellerpanel.photoqueue import queuePics
        from storefront.models import PhotoCheckJob

        with (
            patch("sellerpanel.forms.ListingForm.is_valid") as validate,
            patch("sellerpanel.photos.queuePics", side_effect=queuePics),
        ):
            response = self.saveDraft(
                postalCode="12",
                askingPrice="10",
                description="Still writing",
                photo_kitchen=jpeg(),
                csrfmiddlewaretoken="private",
                confirm="1",
            )
        savedDraft = ListingDraft.objects.get(seller=self.seller)
        self.assertContains(response, "Your draft is saved.")
        self.assertFalse(response.context["form"].is_bound)
        self.assertEqual(response.context["form"].errors, {})
        self.assertEqual(savedDraft.formData["askingPrice"], "10")
        self.assertEqual(savedDraft.formData["postalCode"], "12")
        self.assertNotIn("csrfmiddlewaretoken", savedDraft.formData)
        self.assertNotIn("confirm", savedDraft.formData)
        self.assertNotIn("photo_kitchen", savedDraft.formData)
        job = PhotoCheckJob.objects.get(owner=self.seller, room="kitchen")
        self.assertEqual(savedDraft.photos, {"kitchen": job.pendingPath})
        self.assertEqual(job.status, "queued")
        self.assertFalse(Listing.objects.exists())
        validate.assert_not_called()
        self.checker.assert_not_called()
        self.band.assert_not_called()
        self.assertContains(response, 'value="saveDraft" formnovalidate')
        self.assertContains(response, 'value="discardDraft" formnovalidate')

    def test_save_updates_sellers_single(self):
        self.saveDraft(description="First version")
        first = ListingDraft.objects.get()
        self.saveDraft(description="Second version")
        savedDraft = ListingDraft.objects.get()
        self.assertEqual(savedDraft.pk, first.pk)
        self.assertEqual(savedDraft.formData["description"], "Second version")
        self.assertGreaterEqual(savedDraft.updatedAt, first.updatedAt)

    def test_new_session_restores_saved(self):
        path = self.checkPhoto()
        self.saveDraft(**FLAT, description="Corner unit")
        response = self.newSession()
        self.assertEqual(response.context["draftToken"], self.token)
        form = response.context["form"]
        for name, value in {**FLAT, "description": "Corner unit"}.items():
            self.assertEqual(form[name].value(), value)
        self.assertFalse(form.is_bound)
        slot = next((slot for slot in response.context["photoSlots"] if slot["key"] == "kitchen"))
        self.assertEqual(slot["url"], reverse("draft_photo", kwargs={"path": path}))
        restored = self.client.session[DRAFTS_KEY][self.token]
        self.assertEqual(restored["checks"]["kitchen"], {**CLEAN, "unchecked": False})
        self.assertEqual(self.checker.call_count, 1)

    def test_saved_photos_survive_expired(self):
        path = self.checkPhoto()
        self.saveDraft(description="Keep this")
        session = self.client.session
        session[DRAFTS_KEY][self.token]["created"] = 0
        session.save()
        response = self.client.get(reverse("sell"))
        self.assertEqual(response.context["draftToken"], self.token)
        self.assertTrue(default_storage.exists(path))
        with patch.object(
            default_storage, "get_modified_time", return_value=timezone.now() - timedelta(days=2)
        ):
            sweepPics()
        self.assertTrue(default_storage.exists(path))
        response = self.newSession()
        self.assertEqual(response.context["draftToken"], self.token)

    def test_cleanup_removes_expired_unsaved(self):
        path = self.checkPhoto()
        with patch.object(
            default_storage, "get_modified_time", return_value=timezone.now() - timedelta(days=2)
        ):
            sweepPics()
        self.assertFalse(default_storage.exists(path))

    def test_replacing_saved_photo_keeps(self):
        oldPath = self.checkPhoto()
        self.saveDraft()
        newPath = self.checkPhoto()
        self.assertNotEqual(oldPath, newPath)
        self.assertTrue(default_storage.exists(oldPath))
        self.assertEqual(ListingDraft.objects.get().photos["kitchen"], oldPath)
        self.saveDraft()
        self.assertFalse(default_storage.exists(oldPath))
        self.assertTrue(default_storage.exists(newPath))
        self.assertEqual(ListingDraft.objects.get().photos["kitchen"], newPath)

    def test_removing_saved_photo_updates(self):
        path = self.checkPhoto("other")
        self.saveDraft()
        self.saveDraft(removePhoto="other")
        self.assertFalse(default_storage.exists(path))
        savedDraft = ListingDraft.objects.get()
        self.assertEqual(savedDraft.photos, {})
        self.assertEqual(savedDraft.checks, {})

    def test_discard_deletes_saved_unsaved(self):
        oldPath = self.checkPhoto()
        self.saveDraft(description="Throw away")
        newPath = self.checkPhoto()
        response = self.client.post(
            reverse("sell"), {"draftToken": self.token, "action": "discardDraft"}, follow=True
        )
        self.assertContains(response, "Your draft has been discarded.")
        self.assertFalse(ListingDraft.objects.exists())
        self.assertFalse(default_storage.exists(oldPath))
        self.assertFalse(default_storage.exists(newPath))
        self.assertNotIn(self.token, self.client.session[DRAFTS_KEY])
        self.assertFalse(response.context["form"]["description"].value())
        self.assertNotEqual(response.context["draftToken"], self.token)
        self.assertTrue(all((not slot["url"] for slot in response.context["photoSlots"])))

    def test_discard_session_loss_deletes(self):
        path = self.checkPhoto()
        self.saveDraft()
        self.newSession()
        self.client.post(reverse("sell"), {"action": "discardDraft"})
        self.assertFalse(ListingDraft.objects.exists())
        self.assertFalse(default_storage.exists(path))
        self.assertNotIn(self.token, self.client.session[DRAFTS_KEY])

    def test_discard_also_clears_unsaved(self):
        path = self.checkPhoto()
        self.client.post(reverse("sell"), {"draftToken": self.token, "action": "discardDraft"})
        self.assertFalse(default_storage.exists(path))
        self.assertNotIn(self.token, self.client.session[DRAFTS_KEY])

    def test_dashboard_shows_saved_date(self):
        self.saveDraft()
        response = self.client.get(reverse("dashboard"))
        saved = timezone.localtime(response.context["savedDraft"].updatedAt)
        self.assertContains(response, "Continue your draft")
        self.assertContains(response, "Saved " + saved.strftime("%-d %b"))
        self.assertContains(response, f'href="{reverse('sell')}">Continue draft</a>')
        self.assertEqual(response.context["savedDraft"].seller, self.seller)
        self.client.force_login(makeUser("other@test.com", "seller"))
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "Continue your draft")
        self.assertIsNone(response.context["savedDraft"])
        response = self.client.get(reverse("sell"))
        self.assertNotEqual(response.context["draftToken"], self.token)
        self.client.post(reverse("sell"), {"action": "discardDraft", "draftToken": self.token})
        self.assertTrue(ListingDraft.objects.filter(seller=self.seller).exists())

    def test_final_submission_restores(self):
        paths = [self.checkPhoto(room) for room in ROOMS]
        self.saveDraft(**FLAT)
        self.newSession()
        response = self.client.post(reverse("sell"), {**FLAT, "draftToken": self.token})
        flat = Listing.objects.get()
        self.assertRedirects(
            response,
            reverse("seller_viewings") + "?first=1#slot-form",
            fetch_redirect_response=False,
        )
        self.assertEqual(flat.status, "live")
        self.assertEqual(flat.photos.count(), len(ROOMS))
        self.assertFalse(ListingDraft.objects.exists())
        self.assertTrue(all((not default_storage.exists(path) for path in paths)))
        self.assertTrue(all((default_storage.exists(photo.image.name) for photo in flat.photos.all())))
        self.assertNotIn(self.token, self.client.session[DRAFTS_KEY])
        self.assertEqual(sum((len(call.args[0]) for call in self.checker.call_args_list)), len(ROOMS))

    def test_invalid_final_submission_keeps(self):
        path = self.checkPhoto()
        self.saveDraft(description="Incomplete")
        response = self.client.post(reverse("sell"), {"draftToken": self.token})
        self.assertIn("askingPrice", response.context["form"].errors)
        response = self.client.post(reverse("sell"), {**FLAT, "draftToken": self.token})
        self.assertTrue(response.context["photosMissing"])
        self.assertFalse(Listing.objects.exists())
        self.assertTrue(ListingDraft.objects.exists())
        self.assertTrue(default_storage.exists(path))

    def test_saved_not_bypass_price(self):
        for room in ROOMS:
            self.checkPhoto(room)
        self.saveDraft(**{**FLAT, "askingPrice": "600000"})
        response = self.client.post(
            reverse("sell"), {**FLAT, "askingPrice": "600000", "draftToken": self.token}
        )
        self.assertTrue(response.context["needsConfirm"])
        self.assertTrue(ListingDraft.objects.exists())
        self.assertFalse(Listing.objects.exists())
        self.client.post(
            reverse("sell"),
            {**FLAT, "askingPrice": "600000", "draftToken": self.token, "confirm": "1"},
        )
        self.assertEqual(Listing.objects.get().status, "review")
        self.assertFalse(ListingDraft.objects.exists())

    def test_open_flat_blocks_saving(self):
        self.saveDraft()
        makeListing(self.seller)
        for data in ({"action": "saveDraft", "description": "Blocked"}, FLAT):
            response = self.client.post(reverse("sell"), {"draftToken": self.token, **data})
            self.assertRedirects(response, reverse("my_listings"), fetch_redirect_response=False)
        self.assertEqual(Listing.objects.count(), 1)
        self.assertTrue(ListingDraft.objects.exists())
        self.client.post(reverse("sell"), {"action": "discardDraft", "draftToken": self.token})
        self.assertFalse(ListingDraft.objects.exists())

    def test_restored_photo_review_result(self):
        for room in ROOMS:
            self.checkPhoto(room)
        session = self.client.session
        session[DRAFTS_KEY][self.token]["checks"]["kitchen"]["irrelevant"] = 0.6
        session.save()
        self.saveDraft(**FLAT)
        self.newSession()
        self.client.post(reverse("sell"), {**FLAT, "draftToken": self.token})
        flat = Listing.objects.get()
        self.assertEqual(flat.status, "review")
        self.assertEqual(flat.photos.get(room="kitchen").aiIrrelevant, 0.6)
        self.assertFalse(ListingDraft.objects.exists())

    def test_suspension_at_account_lock(self):
        self.saveDraft()
        for room in ROOMS:
            self.checkPhoto(room)
        self.seller.is_active = False
        with patch("sellerpanel.listings.lock_user", return_value=self.seller):
            for data in ({"action": "saveDraft"}, FLAT):
                response = self.client.post(reverse("sell"), {"draftToken": self.token, **data})
                self.assertEqual(response.status_code, 403)
        self.assertFalse(Listing.objects.exists())
        self.assertTrue(ListingDraft.objects.exists())


QUERY = {"town": "BEDOK", "flatType": "4 ROOM", "area": "92", "storey": "8", "lease": "1990"}


class CarryAnswersTests(TestCase):

    @patch("storefront.views.band", return_value=(548000, 577000, 639000))
    def test_price_result_links_filled(self, band):
        seller = makeUser("carry-seller@test.com", "seller")
        self.client.force_login(seller)
        result = self.client.get(reverse("valuation"), QUERY)
        listUrl = result.context["listUrl"]
        self.assertIn("floorAreaSqm=92", listUrl)
        self.assertIn("storey=8", listUrl)
        page = self.client.get(listUrl)
        self.assertContains(page, "We’ve filled in the details from your price check.")
        form = page.context["form"]
        self.assertEqual(
            (
                form["town"].value(),
                form["flatType"].value(),
                form["floorAreaSqm"].value(),
                form["storey"].value(),
                form["leaseCommenceYear"].value(),
            ),
            ("BEDOK", "4 ROOM", "92", "8", "1990"),
        )

    def test_postal_code_fills_block(self):
        from sellerpanel.listings import oldAnswers

        answers = oldAnswers({"postalCode": "760123", "flatType": "4 ROOM", "town": "BEDOK"})
        self.assertEqual(
            (answers["block"], answers["town"], answers["leaseCommenceYear"]),
            ("123", "YISHUN", "1985"),
        )


WARNING = "HDB records show only 2-room flats sold at Blk 1 Test St, not 4-room. Please check the flat type."
realCheck = address.check


def checkWithWarning(*args, **kwargs):
    record, errors, warnings, fixes = realCheck(*args, **kwargs)
    return (record, errors, {**warnings, "flatType": WARNING}, fixes)


class SellWarningTests(CompletedPhotoQueueMixin, TestCase):

    def setUp(self):
        self.seller = makeUser("warned@test.com", "seller")
        self.client.force_login(self.seller)
        self.result = dict(CLEAN)
        for target, value in (
            ("sellerpanel.listings.flatBand", FAIR),
            ("sellerpanel.photos.checkPics", None),
        ):
            patcher = (
                patch(target, return_value=value)
                if value
                else patch(target, side_effect=lambda photos: [dict(self.result) for photo in photos])
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("storefront.address.check", side_effect=checkWithWarning)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.token = self.client.get(reverse("sell")).context["draftToken"]

    def data(self):
        data = {**FLAT, "draftToken": self.token}
        for room in ROOMS:
            data[f"photo_{room}"] = jpeg()
        return data

    def test_doubtful_answer_shown_on(self):
        response = self.client.post(reverse("sell"), self.data())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Listing.objects.filter(seller=self.seller).exists())
        self.assertContains(response, "Please check this before you publish")
        self.assertContains(response, WARNING)
        self.assertContains(response, 'name="warningsSeen"')

    def test_submitting_again_keeps_answer(self):
        first = self.client.post(reverse("sell"), self.data())
        key = first.context["warningKey"]
        data = {**FLAT, "draftToken": self.token, "warningsSeen": key}
        response = self.client.post(reverse("sell"), data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Listing.objects.filter(seller=self.seller).exists())
        sent = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertNotIn(WARNING, sent)

    def test_stale_confirmation_not_skip(self):
        response = self.client.post(reverse("sell"), {**self.data(), "warningsSeen": "0000000000000000"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Listing.objects.filter(seller=self.seller).exists())
