from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.core.files.storage import default_storage
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
import numpy as np

from adminpanel.moderation import photoWhy
from .helpers import CompletedPhotoQueueMixin
from sellerpanel import photocheck
from sellerpanel.models import ListingDraft
from sellerpanel.photocheck import doubtQ, MODEL_VERSION, reviewReasons
from sellerpanel.photos import DRAFTS_KEY, photoType, appealWhy, photoStamp, redoStale
from storefront.models import Listing, ListingPhoto
from storefront.tests.helpers import (
    CLEAN,
    FAIR,
    FLAT,
    ROOMS,
    jpeg,
    makeUser,
    makeListing,
    makeStaff,
)


def mockedScores(label, roomLabel="outdoor", roomConfidence=0.99):
    classifier = MagicMock(classes_=np.array([roomLabel, "other"]))
    classifier.predict_proba.return_value = [[roomConfidence, 1 - roomConfidence]]
    head = {"model": classifier, "relevant": ["kitchen"], "hybrid_confidence": 0.9}
    aiClassifier = MagicMock(classes_=np.array([False, True]))
    aiClassifier.predict_proba.return_value = [[0.99, 0.01]]
    chances = [0.0] * len(photocheck.LABELS)
    chances[next((index for index, row in enumerate(photocheck.LABELS) if row[1] == label))] = 0.99
    with patch.object(photocheck, "heads", return_value=(head, {"model": aiClassifier})):
        result = photocheck.decide(np.zeros((1, 512)), chances)
    return {**result, "modelVersion": photocheck.MODEL_VERSION}


class SlotWarningTests(SimpleTestCase):

    def test_clip_overrides_warn_non(self):
        for label, matchingSlot in [
            ("balcony", "balcony"),
            ("service yard", "service_yard"),
            ("window view", "view"),
        ]:
            result = mockedScores(label)
            self.assertEqual(result["labelSource"], "clip-override")
            self.assertEqual(result["label"], label)
            self.assertEqual(result["roomLabel"], "outdoor")
            self.assertEqual(result["irrelevant"], 0.0)
            for slot, slotLabel in photocheck.ROOM_LABELS.items():
                with self.subTest(label=label, slot=slot):
                    warning = (
                        ""
                        if slot == matchingSlot
                        else f"This looks like a {label}, not the {slotLabel}. Our team will check it."
                    )
                    self.assertEqual(photocheck.slotWarn(result, slot), warning)

    def test_room_head_source_keeps(self):
        result = mockedScores("kitchen", roomLabel="kitchen", roomConfidence=0.96)
        self.assertEqual(result["labelSource"], "room-head")
        self.assertEqual(
            photocheck.slotWarn(result, "master_bedroom"),
            "This looks like a kitchen, not a bedroom. Please check it’s the right photo for Master bedroom.",
        )
        self.assertEqual(photocheck.slotWarn(result, "kitchen"), "")
        self.assertEqual(photocheck.slotWarn(result, "other"), "")

    def test_high_confidence_mismatch(self):
        result = {"roomLabel": "kitchen", "roomConfidence": 0.95}
        self.assertEqual(
            photocheck.slotWarn(result, "master_bedroom"),
            "This looks like a kitchen, not a bedroom. Please check it’s the right photo for Master bedroom.",
        )
        for label, slot in [
            ("bedroom", "master_bedroom"),
            ("bedroom", "bedroom_5"),
            ("bathroom", "master_toilet"),
            ("toilet", "common_toilet_3"),
            ("living_room", "living_room"),
            ("living room", "living_room"),
        ]:
            with self.subTest(label=label, slot=slot):
                self.assertEqual(photocheck.slotWarn({"roomLabel": label, "roomConfidence": 0.99}, slot), "")

    def test_low_confidence_ambiguous_clip(self):
        for label in ["dining_room", "empty_room", "corridor", "unknown", "balcony", "floor_plan"]:
            self.assertEqual(photocheck.slotWarn({"roomLabel": label, "roomConfidence": 0.99}, "kitchen"), "")
        for confidence in [None, 0.59, float("nan"), float("inf"), 1.1]:
            self.assertEqual(
                photocheck.slotWarn({"roomLabel": "kitchen", "roomConfidence": confidence}, "bedroom_2"),
                "",
            )
        self.assertEqual(photocheck.slotWarn({"label": "kitchen", "roomConfidence": 0.99}, "bedroom_2"), "")
        self.assertEqual(photocheck.slotWarn({"roomLabel": "kitchen", "roomConfidence": 0.99}, "other"), "")

    def test_wrong_room_in_toilet(self):
        result = {"roomLabel": "dining_room", "roomConfidence": 0.82, "tags": ["dining table"]}
        self.assertEqual(
            photocheck.slotWarn(result, "master_toilet"),
            "This looks like a dining room, not a toilet. Please check it’s the right photo for Master bedroom toilet.",
        )
        self.assertEqual(
            photocheck.slotWarn({"roomLabel": "bathroom", "roomConfidence": 0.99, "tags": []}, "bedroom_2"),
            "This looks like a toilet, not a bedroom. Please check it’s the right photo for Bedroom 2.",
        )

    def test_open_plan_rooms_matching(self):
        openPlan = {"roomLabel": "kitchen", "roomConfidence": 0.83, "tags": ["kitchen cabinets"]}
        self.assertEqual(photocheck.slotWarn(openPlan, "living_room"), "")
        self.assertEqual(
            photocheck.slotWarn({"roomLabel": "dining_room", "roomConfidence": 0.9, "tags": []}, "kitchen"),
            "",
        )
        withBed = {"roomLabel": "living_room", "roomConfidence": 0.7, "tags": ["bed", "sofa"]}
        self.assertEqual(photocheck.slotWarn(withBed, "master_bedroom"), "")
        withBowl = {"roomLabel": "bedroom", "roomConfidence": 0.7, "tags": ["toilet bowl"]}
        self.assertEqual(photocheck.slotWarn(withBowl, "common_toilet"), "")

    def test_likely_generated_photo_goes(self):
        result = {
            "irrelevant": 0.01,
            "aiGenerated": 0.765,
            "aiReviewAt": 0.494,
            "modelVersion": photocheck.MODEL_VERSION,
        }
        self.assertIn(photocheck.AI_REASON, photocheck.reviewReasons(result))
        self.assertNotIn(photocheck.AI_REASON, photocheck.reviewReasons({**result, "aiGenerated": 0.69}))
        self.assertEqual(photocheck.AI_HOLD_AT, 0.7)

    def test_room_head_confidence_survives(self):
        classifier = MagicMock()
        classifier.classes_ = np.array(["bathroom", "kitchen"])
        classifier.predict_proba.return_value = [[0.04, 0.96]]
        head = {"model": classifier, "relevant": ["bathroom", "kitchen"], "hybrid_confidence": 0.9}
        chances = [0.0] * len(photocheck.LABELS)
        chances[4] = 0.99
        with patch.object(photocheck, "heads", return_value=(head, None)):
            result = photocheck.decide(np.zeros((1, 512)), chances)
        self.assertEqual(result["label"], "balcony")
        self.assertEqual(result["roomLabel"], "kitchen")
        self.assertEqual(result["roomConfidence"], 0.96)
        self.assertEqual(result["labelSource"], "clip-override")
        self.assertTrue(photocheck.slotWarn(result, "master_bedroom"))
        self.assertEqual(photocheck.slotWarn(result, "balcony"), "")

    def test_photo_mismatch_stops_subprocess(self):
        with (
            patch.object(photocheck, "isOn", return_value=True),
            patch.object(photocheck, "verifyModel", return_value="identity mismatch"),
            patch.object(photocheck.subprocess, "run") as process,
        ):
            result = photocheck.checkPics([b"photo"])[0]
        process.assert_not_called()
        self.assertIn("Photo model identity mismatch; staff must review it", photocheck.reviewReasons(result))


class SlotWarningFlowTests(CompletedPhotoQueueMixin, TestCase):

    def setUp(self):
        self.seller = makeUser("slot@test.com", "seller")
        self.client.force_login(self.seller)
        self.token = self.client.get(reverse("sell")).context["draftToken"]
        self.result = {**CLEAN, "roomLabel": "kitchen", "roomConfidence": 0.96}
        checker = patch(
            "sellerpanel.photos.checkPics",
            side_effect=lambda photos: [dict(self.result) for photo in photos],
        )
        checker.start()
        self.addCleanup(checker.stop)
        price = patch("sellerpanel.listings.band", return_value=FAIR)
        price.start()
        self.addCleanup(price.stop)

    def test_clip_override_mismatch_warns(self):
        for label in photocheck.TRUST_CLIP_FOR:
            with self.subTest(label=label):
                self.result = mockedScores(label)
                response = self.client.post(
                    reverse("photo_check"),
                    {"draftToken": self.token, "room": "kitchen", "photo": jpeg()},
                )
                warning = f"This looks like a {label}, not the Kitchen. Our team will check it."
                self.assertEqual(response.json(), {"verdict": "review", "message": warning})
                saved = self.client.session[DRAFTS_KEY][self.token]
                self.assertIn("kitchen", saved["photos"])
                self.assertEqual(saved["checks"]["kitchen"]["reviewReasons"], [warning])
                self.assertEqual(photocheck.reviewReasons(saved["checks"]["kitchen"]), [warning])
        self.result = dict(CLEAN)
        data = {**FLAT, "draftToken": self.token}
        for room in ROOMS:
            if room != "kitchen":
                data[f"photo_{room}"] = jpeg()
        self.client.post(reverse("sell"), data)
        listing = Listing.objects.get(seller=self.seller)
        self.assertEqual(listing.status, "review")
        photo = listing.photos.get(room="kitchen")
        self.assertEqual(photo.photoReview, [warning])
        self.assertIn(warning, photoWhy(photo))

    def test_clip_override_matching_allows(self):
        for label, slot in [
            ("balcony", "balcony"),
            ("service yard", "service_yard"),
            ("window view", "view"),
        ]:
            with self.subTest(label=label):
                self.result = mockedScores(label, roomLabel="kitchen", roomConfidence=0.96)
                response = self.client.post(
                    reverse("photo_check"),
                    {"draftToken": self.token, "room": slot, "photo": jpeg()},
                )
                self.assertEqual(response.json()["verdict"], "accept")
                saved = self.client.session[DRAFTS_KEY][self.token]
                self.assertIn(slot, saved["photos"])
                self.assertEqual(photocheck.reviewReasons(saved["checks"][slot]), [])

    def test_keeps_upload_persists_staff(self):
        response = self.client.post(
            reverse("photo_check"),
            {"draftToken": self.token, "room": "master_bedroom", "photo": jpeg()},
        )
        self.assertEqual(response.json()["verdict"], "review")
        warning = (
            "This looks like a kitchen, not a bedroom. Please check it’s the right photo for Master bedroom."
        )
        self.assertEqual(response.json()["message"], warning)
        self.assertIn("master_bedroom", self.client.session[DRAFTS_KEY][self.token]["photos"])
        data = {**FLAT, "draftToken": self.token}
        for room in ROOMS:
            if room != "master_bedroom":
                data[f"photo_{room}"] = jpeg()
        self.client.post(reverse("sell"), data)
        listing = Listing.objects.get(seller=self.seller)
        self.assertEqual(listing.status, "review")
        photo = listing.photos.get(room="master_bedroom")
        self.assertEqual(photo.photoReview, [warning])
        self.assertIn(warning, photoWhy(photo))
        self.assertTrue(ListingPhoto.objects.filter(pk=photo.pk).filter(doubtQ()).exists())
        photo.staffChecked = True
        photo.save(update_fields=["staffChecked"])
        self.assertFalse(ListingPhoto.objects.filter(pk=photo.pk).filter(doubtQ()).exists())

    def test_unavailable_identity_persists(self):
        self.result = {
            "identityStatus": "identity mismatch",
            "modelVersion": photocheck.MODEL_VERSION,
        }
        data = {**FLAT, "draftToken": self.token}
        for room in ROOMS:
            data[f"photo_{room}"] = jpeg()
        self.client.post(reverse("sell"), data)
        photo = ListingPhoto.objects.filter(listing__seller=self.seller).first()
        self.assertIn("Photo model identity mismatch; staff must review it", photoWhy(photo))


def seen(roomLabel, confidence=0.995, tags=None):
    return {
        **CLEAN,
        "labelSource": "room-head",
        "roomLabel": roomLabel,
        "roomConfidence": confidence,
        "tags": tags or ["tiled floor"],
    }


class PhotoTypeTests(SimpleTestCase):

    def test_confident_room(self):
        self.assertEqual(photoType(seen("bathroom")), "toilet")
        self.assertEqual(photoType(seen("bedroom", tags=["bed", "wardrobe"])), "bedroom")
        self.assertEqual(photoType(seen("kitchen", tags=["kitchen cabinets"])), "kitchen")
        self.assertEqual(photoType(seen("living_room", tags=["sofa", "TV console"])), "living_room")

    def test_unsure_mixed_no(self):
        self.assertIsNone(photoType(seen("bathroom", confidence=0.97)))
        self.assertIsNone(photoType(seen("bathroom", tags=["toilet bowl", "kitchen cabinets"])))
        self.assertIsNone(photoType(seen("dining_room")))
        self.assertIsNone(photoType(seen("corridor")))
        self.assertIsNone(photoType({**seen("bathroom"), "labelSource": "clip-override"}))
        self.assertIsNone(photoType({**seen("bathroom"), "appeal": True}))
        self.assertIsNone(photoType({**seen("bathroom"), "roomConfidence": float("nan")}))
        self.assertIsNone(photoType(None))


class PhotoSortFlowTests(CompletedPhotoQueueMixin, TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        cache.delete(f"photo-status:{self.seller.pk}")
        self.client.force_login(self.seller)
        self.token = self.client.get(reverse("sell")).context["draftToken"]
        self.result = seen("bathroom")
        checker = patch(
            "sellerpanel.photos.checkPics",
            side_effect=lambda photos: [dict(self.result) for photo in photos],
        )
        checker.start()
        self.addCleanup(checker.stop)
        price = patch("sellerpanel.views.band", return_value=FAIR)
        price.start()
        self.addCleanup(price.stop)

    def upload(self, room, bedrooms="2", bathrooms="1", token=None):
        return self.client.post(
            reverse("photo_check"),
            {
                "draftToken": token or self.token,
                "room": room,
                "photo": jpeg(),
                "bedrooms": bedrooms,
                "bathrooms": bathrooms,
            },
        ).json()

    def saved(self, token=None):
        return self.client.session[DRAFTS_KEY][token or self.token]

    def test_toilet_in_living_room(self):
        reply = self.upload("living_room")
        self.assertEqual(reply["verdict"], "moved")
        self.assertEqual(reply["movedTo"], "common_toilet")
        self.assertEqual(
            reply["message"],
            "Your photo looks like a toilet, so we moved it to Common toilet. Living room still needs its own photo.",
        )
        self.assertEqual(reply["url"], "")
        self.assertEqual(reply["target"]["verdict"], "accept")
        self.assertEqual(reply["target"]["message"], "We moved this photo here from the living room.")
        self.assertEqual(reply["target"]["movedFrom"], "living_room")
        self.assertTrue(reply["target"]["url"])
        saved = self.saved()
        self.assertNotIn("living_room", saved["photos"])
        self.assertIn("common_toilet", saved["photos"])
        self.assertEqual(saved["checks"]["common_toilet"]["movedFrom"], "living_room")
        self.assertEqual(saved["checks"]["common_toilet"]["reviewReasons"], [])

    def test_kitchen_in_living_room(self):
        self.result = seen("kitchen", tags=["kitchen cabinets", "tiled floor"])
        reply = self.upload("living_room")
        self.assertEqual(reply["movedTo"], "kitchen")
        self.assertEqual(
            reply["message"],
            "Your photo looks like the kitchen, so we moved it to Kitchen. Living room still needs its own photo.",
        )

    def test_filled_room_keeps_todays(self):
        self.result = seen("kitchen", confidence=0.9, tags=["kitchen cabinets"])
        self.upload("kitchen")
        self.result = seen("kitchen", tags=["kitchen cabinets"])
        reply = self.upload("master_bedroom")
        self.assertEqual(reply["verdict"], "review")
        self.assertEqual(
            reply["message"],
            "This looks like a kitchen, not a bedroom. Please check it’s the right photo for Master bedroom.",
        )
        self.assertIn("master_bedroom", self.saved()["photos"])

    def test_unsure_photo_is_not_moved(self):
        self.result = seen("bathroom", confidence=0.96)
        reply = self.upload("master_bedroom")
        self.assertEqual(reply["verdict"], "review")
        self.assertIn("master_bedroom", self.saved()["photos"])
        self.assertNotIn("common_toilet", self.saved()["photos"])

    def test_bedroom_goes_first_empty(self):
        self.result = seen("bedroom", confidence=0.9, tags=["bed"])
        self.upload("master_bedroom")
        self.result = seen("bedroom", tags=["bed"])
        reply = self.upload("kitchen", bedrooms="1")
        self.assertEqual(reply["verdict"], "review")
        self.assertIn("kitchen", self.saved()["photos"])
        reply = self.upload("living_room", bedrooms="2")
        self.assertEqual(reply["movedTo"], "bedroom_2")
        self.assertEqual(
            reply["message"],
            "Your photo looks like a bedroom, so we moved it to Bedroom 2. Living room still needs its own photo.",
        )

    def test_page_shows_note_undo(self):
        self.upload("living_room")
        page = self.client.get(reverse("sell"), {"draftToken": self.token})
        for check_text_1 in (
            "Your photo looks like a toilet, so we moved it to Common toilet. Living room still needs its own photo.",
            "We moved this photo here from the living room.",
            'class="photo-slot moved"',
        ):
            self.assertContains(page, check_text_1)
        self.assertContains(
            page,
            'value="living_room" formaction="'
            + reverse("photo_move_back")
            + '" formnovalidate>Move it back to Living room</button>',
        )
        status = self.client.get(reverse("photo_status"), {"draftToken": self.token}).json()["photos"]
        self.assertEqual(status["common_toilet"]["movedFrom"], "living_room")
        self.assertEqual(
            status["common_toilet"]["movedNote"],
            "Your photo looks like a toilet, so we moved it to Common toilet. Living room still needs its own photo.",
        )

    def test_keep_in_living_room(self):
        self.upload("living_room")
        response = self.client.post(
            reverse("photo_move_back"),
            {**FLAT, "draftToken": self.token, "photoBack": "living_room"},
        )
        self.assertRedirects(
            response,
            f"{reverse('sell')}?draftToken={self.token}#sell-photos",
            fetch_redirect_response=False,
        )
        saved = self.saved()
        self.assertIn("living_room", saved["photos"])
        self.assertNotIn("common_toilet", saved["photos"])
        self.assertTrue(saved["checks"]["living_room"]["keptHere"])
        self.assertEqual(saved["formData"]["postalCode"], FLAT["postalCode"])
        page = self.client.get(response.url)
        self.assertContains(
            page,
            "This looks like a toilet, not a living room. Please check it’s the right photo for Living room.",
        )
        self.assertContains(page, "formnovalidate hidden>Move it back to Living room</button>")
        self.assertEqual(page.context["form"].initial["postalCode"], FLAT["postalCode"])

    def test_moved_listed_in_new(self):
        self.upload("living_room")
        self.result = dict(CLEAN)
        data = {**FLAT, "draftToken": self.token}
        for room in ROOMS:
            if room != "common_toilet":
                data[f"photo_{room}"] = jpeg()
        self.client.post(reverse("sell"), data)
        listing = Listing.objects.get(seller=self.seller)
        self.assertEqual(listing.status, "live")
        self.assertEqual(listing.photos.get(room="common_toilet").photoReview, [])

    def test_edit_form_moves_into(self):
        self.result = dict(CLEAN)
        data = {**FLAT, "draftToken": self.token}
        for room in ROOMS:
            data[f"photo_{room}"] = jpeg()
        self.client.post(reverse("sell"), data)
        listing = Listing.objects.get(seller=self.seller)
        page = self.client.get(reverse("edit_listing", args=[listing.publicId]))
        token = page.context["draftToken"]
        self.result = seen("bathroom")
        reply = self.upload("living_room", bathrooms="1", token=token)
        self.assertEqual(reply["verdict"], "review")
        self.assertIn("living_room", self.saved(token)["photos"])
        reply = self.upload("living_room", bathrooms="2", token=token)
        self.assertEqual(reply["movedTo"], "master_toilet")
        self.assertEqual(
            reply["message"],
            "Your photo looks like a toilet, so we moved it to Master bedroom toilet. Living room still needs its own photo.",
        )
        self.assertEqual(reply["url"], listing.photos.get(room="living_room").publicUrl)
        response = self.client.post(
            reverse("photo_move_back"), {"draftToken": token, "photoBack": "living_room"}
        )
        self.assertRedirects(
            response,
            f"{reverse('edit_listing', args=[listing.publicId])}?draftToken={token}#sell-photos",
            fetch_redirect_response=False,
        )
        self.assertIn("living_room", self.saved(token)["photos"])

    def test_kept_missing_warning_says(self):
        self.result = seen("kitchen", tags=["kitchen cabinets"])
        self.upload("living_room")
        self.client.post(reverse("photo_move_back"), {"draftToken": self.token, "photoBack": "living_room"})
        page = self.client.get(reverse("sell"), {"draftToken": self.token})
        self.assertContains(page, "No problem, we’ll keep this photo here.")
        self.assertEqual(self.saved()["checks"]["living_room"]["reviewReasons"], [])

    def test_appealed_never_moved(self):
        self.result = {**seen("bathroom"), "irrelevant": 0.99, "blockAt": 0.6}
        reply = self.upload("living_room")
        self.assertEqual(reply["verdict"], "block")
        reply = self.client.post(
            reverse("photo_check"), {"draftToken": self.token, "room": "living_room", "appeal": "1"}
        ).json()
        self.assertEqual(reply["verdict"], "review")
        self.assertIn("living_room", self.saved()["photos"])
        self.assertNotIn("common_toilet", self.saved()["photos"])

    def test_new_living_room_not(self):
        self.upload("living_room")
        self.result = seen("living_room", tags=["sofa", "TV console"])
        reply = self.upload("living_room")
        self.assertEqual(reply["verdict"], "accept")
        cache.delete(f"photo-status:{self.seller.pk}")
        photos = self.client.get(reverse("photo_status"), {"draftToken": self.token}).json()["photos"]
        self.assertNotIn("movedFrom", photos.get("common_toilet", {}))
        self.assertNotEqual(photos.get("living_room", {}).get("verdict"), "moved")


class BlockMessageTests(SimpleTestCase):

    def test_food_reads_naturally(self):
        from sellerpanel.photos import blockNote

        self.assertEqual(
            blockNote("food", "living room"),
            "This looks like food or a drink, not the living room.",
        )
        self.assertEqual(blockNote("selfie", "kitchen"), "This looks like a selfie, not the kitchen.")


class PhotoAppealTests(CompletedPhotoQueueMixin, TestCase):

    def setUp(self):
        self.seller = makeUser("appeal@test.com", "seller")
        self.client.force_login(self.seller)
        self.token = self.client.get(reverse("sell")).context["draftToken"]
        self.result = {
            **CLEAN,
            "label": "outdoor",
            "irrelevant": 0.99,
            "tags": ["window"],
            "embedding": [1.0, 0.0],
        }
        checker = patch(
            "sellerpanel.photos.checkPics",
            side_effect=lambda photos: [dict(self.result) for photo in photos],
        )
        self.checker = checker.start()
        self.addCleanup(checker.stop)
        price = patch("sellerpanel.listings.band", return_value=FAIR)
        price.start()
        self.addCleanup(price.stop)

    def saved(self):
        return self.client.session[DRAFTS_KEY][self.token]

    def check(self, room="kitchen"):
        return self.client.post(
            reverse("photo_check"), {"draftToken": self.token, "room": room, "photo": jpeg()}
        )

    def appeal(self, room="kitchen"):
        return self.client.post(
            reverse("photo_check"), {"draftToken": self.token, "room": room, "appeal": "1"}
        )

    def submit(self, **changes):
        data = {**FLAT, "draftToken": self.token, **changes}
        for room in ROOMS:
            if room not in self.saved()["photos"] and room not in self.saved().get("refused", {}):
                data[f"photo_{room}"] = jpeg()
        self.result = dict(CLEAN)
        return self.client.post(reverse("sell"), data)

    def test_refusal_requires_replacement(self):
        response = self.client.post(
            reverse("sell"), {**FLAT, "draftToken": self.token, "photo_kitchen": jpeg()}
        )
        slot = next((slot for slot in response.context["photoSlots"] if slot["key"] == "kitchen"))
        self.assertEqual(
            slot["error"],
            "This looks like an outdoor scene, not the kitchen. Please choose a photo of that room instead.",
        )
        self.assertTrue(slot["appealable"])
        self.assertContains(
            response,
            '<input type="checkbox" name="appealPhoto" value="kitchen"> This is a photo of the room. Ask our team to check it',
        )
        self.assertEqual(self.saved()["photos"], {})
        self.assertEqual(self.saved()["checks"], {})
        self.assertFalse(Listing.objects.exists())
        self.submit()
        self.assertFalse(Listing.objects.exists())

    def test_missing_temporary_asks(self):
        self.check()
        default_storage.delete(self.saved()["refused"]["kitchen"]["path"])
        calls = self.checker.call_count
        response = self.appeal()
        self.assertEqual(response.status_code, 400)
        self.assertIn("Please choose the photo again", response.json()["message"])
        self.assertFalse(response.json()["appealable"])
        self.assertNotIn("kitchen", self.saved().get("refused", {}))
        self.assertNotIn("kitchen", self.saved()["photos"])
        self.assertEqual(self.checker.call_count, calls)

    def test_keeps_original_result_forces(self):
        self.assertEqual(self.check().json()["verdict"], "block")
        refusedPath = self.saved()["refused"]["kitchen"]["path"]
        calls = self.checker.call_count
        response = self.appeal()
        self.assertEqual(response.json()["verdict"], "review")
        self.assertEqual(self.checker.call_count, calls)
        result = self.saved()["checks"]["kitchen"]
        for key, value in self.result.items():
            self.assertEqual(result[key], value)
        self.assertTrue(result["appeal"])
        self.assertIn(appealWhy("kitchen"), result["reviewReasons"])
        self.assertFalse(default_storage.exists(refusedPath))
        self.assertEqual(self.client.get(response.json()["url"]).status_code, 200)
        self.submit()
        listing = Listing.objects.get(seller=self.seller)
        self.assertEqual(listing.status, "review")
        photo = listing.photos.get(room="kitchen")
        self.assertFalse(photo.staffChecked)
        self.assertIn(appealWhy("kitchen"), photo.photoReview)
        self.assertEqual(photo.aiIrrelevant, 0.99)
        self.client.force_login(makeStaff("appealstaff@test.com"))
        queue = self.client.get(reverse("admin_queue"))
        self.assertContains(
            self.client.get(reverse("admin_queue_item", args=[queue.context["rows"][0]["flat"].pk])),
            appealWhy("kitchen"),
        )
        self.assertIn(photo.pk, [item.pk for item in queue.context["rows"][0]["doubtful"]])
        self.client.post(
            reverse("admin_decide", args=[listing.pk]),
            {"revision": listing.revision, "status": "review", "decision": "approved"},
        )
        listing.refresh_from_db()
        photo.refresh_from_db()
        self.assertEqual(listing.status, "live")
        self.assertTrue(photo.staffChecked)
        self.assertIn(appealWhy("kitchen"), photo.photoReview)

    def test_form_checkbox_refused_upload(self):
        response = self.client.post(
            reverse("sell"), {**FLAT, "draftToken": self.token, "photo_kitchen": jpeg()}
        )
        self.assertEqual(response.status_code, 200)
        response = self.submit(appealPhoto="kitchen")
        self.assertEqual(response.status_code, 302)
        listing = Listing.objects.get(seller=self.seller)
        self.assertEqual(listing.status, "review")
        photo = listing.photos.get(room="kitchen")
        self.assertEqual(photo.photoReview, [appealWhy("kitchen")])
        self.assertEqual(photo.aiIrrelevant, 0.99)

    def test_fourth_endpoint_refused_clear(self):
        for room in ROOMS[:3]:
            self.check(room)
            self.assertEqual(self.appeal(room).json()["verdict"], "review")
        self.check(ROOMS[3])
        response = self.appeal(ROOMS[3])
        self.assertEqual(response.status_code, 400)
        self.assertIn("up to 3 refused photos per listing", response.json()["message"])
        self.assertEqual(sum((item.get("appeal", False) for item in self.saved()["checks"].values())), 3)
        self.assertNotIn(ROOMS[3], self.saved()["photos"])

    def test_fourth_form_refused_clear(self):
        self.client.post(
            reverse("sell"),
            {**FLAT, "draftToken": self.token, **{f"photo_{room}": jpeg() for room in ROOMS[:4]}},
        )
        response = self.submit(appealPhoto=ROOMS[:4])
        self.assertContains(response, "up to 3 refused photos per listing")
        self.assertEqual(sum((item.get("appeal", False) for item in self.saved()["checks"].values())), 3)
        self.assertFalse(Listing.objects.exists())

    def test_replacement_frees_clears(self):
        for room in ROOMS[:3]:
            self.check(room)
            self.appeal(room)
        self.check(ROOMS[3])
        path = self.saved()["refused"][ROOMS[3]]["path"]
        self.result = dict(CLEAN)
        self.check(ROOMS[0])
        self.assertFalse(self.saved()["checks"][ROOMS[0]].get("appeal"))
        self.assertEqual(self.appeal(ROOMS[3]).json()["verdict"], "review")
        self.assertFalse(default_storage.exists(path))

    def test_blocks_invent_refused_result(self):
        response = self.appeal()
        self.assertEqual(response.status_code, 400)
        self.assertIn("Please choose a refused room photo", response.json()["message"])
        self.assertEqual(self.saved()["photos"], {})
        self.checker.assert_not_called()

    def test_appealed_draft_persists(self):
        self.check()
        self.client.post(
            reverse("sell"),
            {**FLAT, "draftToken": self.token, "action": "saveDraft", "appealPhoto": "kitchen"},
        )
        saved = ListingDraft.objects.get(seller=self.seller)
        self.assertTrue(saved.checks["kitchen"]["appeal"])
        self.assertEqual(saved.checks["kitchen"]["irrelevant"], 0.99)
        self.assertIn(appealWhy("kitchen"), saved.checks["kitchen"]["reviewReasons"])
        self.assertTrue(default_storage.exists(saved.photos["kitchen"]))

    def test_ai_identity_mismatch_flags(self):
        self.result.update(aiGenerated=0.99, identityStatus="identity mismatch")
        self.check()
        self.appeal()
        with patch("sellerpanel.photocheck.AI_REVIEW_AT", 0.5):
            reasons = reviewReasons(self.saved()["checks"]["kitchen"])
        self.assertIn("may be AI-generated", reasons)
        self.assertIn("Photo model identity mismatch; staff must review it", reasons)
        self.assertIn(appealWhy("kitchen"), reasons)

    def test_limit_includes_existing_editing(self):
        listing = makeListing(self.seller, status="review")
        for room in ROOMS[:3]:
            ListingPhoto.objects.create(
                listing=listing,
                room=room,
                image="room.jpg",
                modelVersion=MODEL_VERSION,
                aiIrrelevant=0.99,
                aiGenerated=0.01,
                photoReview=[appealWhy(room)],
            )
        self.token = self.client.get(reverse("edit_listing", args=[listing.publicId])).context["draftToken"]
        self.check(ROOMS[3])
        self.assertIn("up to 3", self.appeal(ROOMS[3]).json()["message"])
        self.check(ROOMS[0])
        self.assertEqual(self.appeal(ROOMS[0]).json()["verdict"], "review")

    def test_edit_blocks_moved_another(self):
        self.check(ROOMS[3])
        self.appeal(ROOMS[3])
        listing = makeListing(self.seller, status="review")
        for room in ROOMS[:3]:
            ListingPhoto.objects.create(
                listing=listing,
                room=room,
                image="room.jpg",
                modelVersion=MODEL_VERSION,
                aiIrrelevant=0.99,
                aiGenerated=0.01,
                photoReview=[appealWhy(room)],
            )
        response = self.client.post(
            reverse("edit_listing", args=[listing.publicId]),
            {**FLAT, "draftToken": self.token, "revision": listing.revision},
        )
        self.assertContains(response, "up to 3 refused photos per listing")
        self.assertEqual(listing.photos.count(), 3)

    def test_refreshing_stale_checks_keeps(self):
        listing = makeListing(self.seller, status="review")
        photo = ListingPhoto.objects.create(
            listing=listing,
            room="kitchen",
            image="room.jpg",
            modelVersion="old",
            photoReview=[appealWhy("kitchen")],
        )
        self.assertTrue(redoStale(listing, (photoStamp(listing), [(photo, dict(CLEAN))])))
        photo.refresh_from_db()
        self.assertEqual(photo.photoReview, [appealWhy("kitchen")])
        self.assertFalse(photo.staffChecked)

    def test_discard_deletes_refused_upload(self):
        self.check()
        path = self.saved()["refused"]["kitchen"]["path"]
        self.client.post(reverse("sell"), {"draftToken": self.token, "action": "discardDraft"})
        self.assertFalse(default_storage.exists(path))
        self.assertNotIn(self.token, self.client.session[DRAFTS_KEY])
