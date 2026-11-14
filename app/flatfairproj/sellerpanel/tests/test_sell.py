from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
import numpy as np

from .helpers import CompletedPhotoQueueMixin
from sellerpanel import photocheck
from sellerpanel.photos import DRAFTS_KEY, blockNote, niceLabel
from storefront.models import sizeHints, Listing, ListingPhoto
from storefront.tests.helpers import CLEAN, FAIR, FLAT, ROOMS, jpeg, makeListing, makeUser


class SellTests(CompletedPhotoQueueMixin, TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.client.force_login(self.seller)
        self.token = self.client.get(reverse("sell")).context["draftToken"]
        self.result = dict(CLEAN)
        checker = patch(
            "sellerpanel.photos.checkPics", side_effect=lambda photos: [self.result] * len(photos)
        )
        self.checker = checker.start()
        self.addCleanup(checker.stop)
        price = patch("sellerpanel.listings.band", return_value=FAIR)
        self.band = price.start()
        self.addCleanup(price.stop)

    def post(self, rooms=ROOMS, **changes):
        data = {**FLAT, "draftToken": self.token, **changes}
        for room in rooms:
            data[f"photo_{room}"] = jpeg()
        return self.client.post(reverse("sell"), data)

    def test_sections_keep_fields_photos(self):
        response = self.client.get(reverse("sell"))
        html = response.content.decode()
        sections = [
            ("sell-fields", "1 · Address"),
            ("sell-details", "2 · Flat details"),
            ("sell-price", "3 · Price"),
            ("sell-photos", "4 · Photos"),
            ("sell-rules", "5 · Description and rules"),
        ]
        positions = []
        for anchor, heading in sections:
            self.assertContains(response, f'href="#{anchor}"')
            self.assertContains(response, f'id="{anchor}" tabindex="-1"')
            positions.append(html.index(f"<h2>{heading}</h2>"))
        self.assertEqual(positions, sorted(positions))
        for name in response.context["form"].fields:
            with self.subTest(field=name):
                self.assertContains(response, f'name="{name}"', count=1)
        for slot in response.context["photoSlots"]:
            with self.subTest(room=slot["key"]):
                self.assertContains(response, f'name="photo_{slot['key']}"', count=1)
        self.assertContains(
            response,
            '<span class="photo-wait" role="status" aria-live="polite" aria-atomic="true">Checking photo… this can take up to 2 minutes</span>',
            count=len(response.context["photoSlots"]),
            html=True,
        )
        required, optional = html.split('<details class="optional-photos">', 1)
        self.assertIn('name="photo_kitchen"', required)
        self.assertNotIn('name="photo_view"', required)
        self.assertIn('name="photo_view"', optional)
        for action in ["saveDraft", "discardDraft"]:
            self.assertContains(response, f'name="action" value="{action}" formnovalidate')
        for hook in [
            "data-photo-check",
            "data-price-check",
            "data-plan-preview",
            'id="size-hints"',
        ]:
            self.assertContains(response, hook)

    def test_photo_waiting_text_tracks(self):
        with patch("sellerpanel.listings.TIMEOUT", 180):
            response = self.client.get(reverse("sell"))
            self.assertContains(response, "up to 3 minutes")
            flat = makeListing(self.seller)
            response = self.client.get(reverse("edit_listing", args=[flat.publicId]))
            self.assertContains(response, "up to 3 minutes")
        with patch("sellerpanel.listings.TIMEOUT", 60):
            response = self.client.get(reverse("edit_listing", args=[flat.publicId]))
            self.assertContains(response, "up to 1 minute")

    def test_optional_photo_errors_open(self):
        response = self.post(rooms=[], photo_view=SimpleUploadedFile("bad.txt", b"bad"))
        for check_text_1 in (
            '<details class="optional-photos" open>',
            'href="#id_photo_view"',
            'aria-describedby="photo_view_error"',
        ):
            self.assertContains(response, check_text_1)

    def test_price_confirmation_keeps(self):
        response = self.post(askingPrice="600000")
        self.assertTrue(response.context["needsConfirm"])
        for name in response.context["form"].fields:
            self.assertContains(response, f'name="{name}"', count=1)
        for check_text_2 in (
            'name="ownsFlat" required checked',
            'name="confirm" value="1"',
            'href="#id_askingPrice"',
        ):
            self.assertContains(response, check_text_2)
        html = response.content.decode()
        choice = html.index('class="sell-confirm" id="sell-feedback"')
        self.assertLess(html.index('id="sell-price"'), choice)
        self.assertLess(choice, html.index('id="sell-photos"'))

    def test_fair_listing_required_photos(self):
        response = self.post()
        flat = Listing.objects.get()
        self.assertRedirects(
            response,
            reverse("seller_viewings") + "?first=1#slot-form",
            fetch_redirect_response=False,
        )
        page = self.client.get(reverse("seller_viewings"), {"first": "1"})
        self.assertContains(page, "Buyers can only visit at the times you add here.")
        self.assertContains(page, "Your listing is live. Next, add a few viewing times so buyers can visit.")
        self.assertEqual(
            (flat.status, flat.seller, flat.block, flat.estimatedPrice),
            ("live", self.seller, "225A", 400000),
        )
        self.assertEqual(sorted(flat.photos.values_list("room", flat=True)), sorted(ROOMS))
        self.assertEqual(self.checker.call_count, 1)
        self.assertEqual(len(self.checker.call_args[0][0]), 5)

    def test_missing_room_photos_kept(self):
        response = self.post(rooms=ROOMS[:3])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["photosMissing"])
        self.assertFalse(Listing.objects.exists())
        kept = self.client.session[DRAFTS_KEY][self.token]["photos"]
        self.assertEqual(sorted(kept), sorted(ROOMS[:3]))
        self.post(rooms=ROOMS[3:])
        self.assertEqual(Listing.objects.get().photos.count(), 5)

    def test_error_summary_links_field(self):
        response = self.post(rooms=[], askingPrice="", photo_kitchen=SimpleUploadedFile("bad.txt", b"bad"))
        self.assertContains(response, 'id="sell-feedback" role="alert" tabindex="-1"')
        self.assertContains(response, f'action="{reverse('sell')}#sell-feedback"')
        for check_text_3 in ('href="#id_askingPrice"', 'href="#id_photo_kitchen"', 'id="id_photo_kitchen"'):
            self.assertContains(response, check_text_3)
        self.assertNotContains(self.client.get(reverse("sell")), 'id="sell-feedback"')

    def test_kept_draft_photos_show(self):
        response = self.post(rooms=ROOMS[:1])
        slot = (
            response.content.decode()
            .split('data-room="living_room"', 1)[1]
            .split('class="photo-status"', 1)[0]
        )
        self.assertIn('<small class="slot-saved">Photo saved</small>', slot)
        self.assertIn("Change photo", slot)
        self.assertNotIn("Required", slot)
        self.assertNotIn("hidden", slot.split("<img", 1)[1].split(">", 1)[0])

    def assertUnavailableCheckHoldsListing(self, field):
        self.result = {**CLEAN, field: None}
        self.post()
        flat = Listing.objects.get()
        self.assertEqual(flat.status, "review")
        savedField = "aiIrrelevant" if field == "irrelevant" else "aiGenerated"
        self.assertEqual(flat.photos.filter(**{savedField: None}).count(), len(ROOMS))

    def test_unavailable_room_check_holds(self):
        self.assertUnavailableCheckHoldsListing("irrelevant")

    def test_unavailable_ai_check_holds(self):
        self.assertUnavailableCheckHoldsListing("aiGenerated")

    def test_non_image_corrupt_uploads(self):
        data = {
            **FLAT,
            "draftToken": self.token,
            "photo_kitchen": SimpleUploadedFile("a.txt", b"asdf", content_type="text/plain"),
        }
        response = self.client.post(reverse("sell"), data)
        slots = {slot["key"]: slot for slot in response.context["photoSlots"]}
        self.assertIn("not a photo", slots["kitchen"]["error"])
        data["photo_kitchen"] = SimpleUploadedFile("a.jpg", b"asdf", content_type="image/jpeg")
        response = self.client.post(reverse("sell"), data)
        self.assertIn(
            "not a photo", {s["key"]: s for s in response.context["photoSlots"]}["kitchen"]["error"]
        )

    def test_irrelevant_selfie_blocks(self):
        self.result = {"label": "selfie", "irrelevant": 0.99, "aiGenerated": 0.01}
        response = self.post()
        self.assertFalse(Listing.objects.exists())
        error = {slot["key"]: slot for slot in response.context["photoSlots"]}["kitchen"]["error"]
        self.assertIn("This looks like a selfie, not the kitchen.", error)

    def test_doubtful_photo_holds_listing(self):
        self.result = {"label": "kitchen", "irrelevant": photocheck.REVIEW_AT, "aiGenerated": 0.01}
        self.post()
        self.assertEqual(Listing.objects.get().status, "review")
        self.assertEqual(ListingPhoto.objects.filter(aiIrrelevant=photocheck.REVIEW_AT).count(), 5)

    def test_unavailable_photo_check_holds(self):
        self.result = None
        self.post()
        flat = Listing.objects.get()
        self.assertEqual(flat.status, "review")
        self.assertTrue(flat.photos.filter(aiIrrelevant=None, aiLabel="unchecked").exists())

    def test_high_asking_price_requires(self):
        response = self.post(askingPrice="600000")
        self.assertTrue(response.context["needsConfirm"])
        self.assertEqual(response.context["flag"], "above")
        self.assertFalse(Listing.objects.exists())
        self.post(rooms=[], askingPrice="600000", confirm="1")
        self.assertEqual(Listing.objects.get().status, "review")

    def test_repeating_submission_blocks(self):
        self.post()
        response = self.post()
        self.assertRedirects(response, reverse("my_listings"), fetch_redirect_response=False)
        self.assertEqual(Listing.objects.count(), 1)

    def test_invalid_postal_town_lease(self):
        response = self.post(postalCode="000000")
        self.assertIn("postalCode", response.context["form"].errors)
        response = self.post(town="BEDOK")
        self.assertIn("town", response.context["form"].errors)
        response = self.post(leaseCommenceYear=str(timezone.localdate().year - 2))
        self.assertIn("leaseCommenceYear", response.context["form"].errors)
        self.assertFalse(Listing.objects.exists())

    def test_unavailable_price_model_shows(self):
        self.band.side_effect = ValueError(
            "Sorry, the price check isn’t available right now. Please try again later."
        )
        response = self.post()
        self.assertIn(
            "the price check isn’t available right now",
            str(response.context["form"].non_field_errors()),
        )
        self.assertFalse(Listing.objects.exists())

    def test_inactive_seller_redirected_form(self):
        self.seller.is_active = False
        self.seller.save()
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get(reverse("sell")).status_code, 302)

    def test_ownership_confirmation_required(self):
        response = self.post(ownsFlat="")
        self.assertIn("ownsFlat", response.context["form"].errors)
        self.assertFalse(Listing.objects.exists())
        self.post(description="Corner unit, new kitchen.")
        flat = Listing.objects.get()
        self.assertEqual(flat.description, "Corner unit, new kitchen.")
        self.assertContains(
            self.client.get(reverse("listing_detail", args=[flat.publicId])),
            "Corner unit, new kitchen.",
        )

    def test_floor_plan_photo_blocked(self):
        self.result = {"label": "floor_plan", "irrelevant": 0.95, "aiGenerated": 0.01}
        response = self.post()
        self.assertFalse(Listing.objects.exists())
        error = {slot["key"]: slot for slot in response.context["photoSlots"]}["kitchen"]["error"]
        self.assertIn("This looks like a floor plan, not the kitchen.", error)
        self.assertNotIn("kitchen", self.client.session[DRAFTS_KEY][self.token]["photos"])

    def test_seller_create_another_listing(self):
        flat = makeListing(self.seller)
        response = self.client.get(reverse("sell"), follow=True)
        self.assertRedirects(response, reverse("my_listings"))
        self.assertContains(response, "You can sell one flat at a time.")
        self.post()
        self.assertEqual(Listing.objects.count(), 1)
        flat.status = "sold"
        flat.save()
        self.assertEqual(self.client.get(reverse("sell")).status_code, 200)
        self.post()
        self.assertEqual(Listing.objects.count(), 2)

    def test_intent_date_blocks_future(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        response = self.post(intentToSellDate=tomorrow.isoformat())
        self.assertIn("intentToSellDate", response.context["form"].errors)
        response = self.post(intentToSellDate=(timezone.localdate() - timedelta(days=400)).isoformat())
        self.assertIn(
            "Please register again with HDB", response.context["form"].errors["intentToSellDate"][0]
        )
        self.assertFalse(Listing.objects.exists())
        self.post()
        self.assertEqual(Listing.objects.get().intentToSellDate.isoformat(), FLAT["intentToSellDate"])

    def test_photo_labels_formatted_seller(self):
        self.assertEqual(niceLabel("empty_room"), "an empty room")
        self.assertEqual(niceLabel("selfie"), "a selfie")
        self.assertEqual(niceLabel("other_place"), "an office or shop, not a home")

    def test_photo_tags_embeddings_saved(self):
        self.result = {
            **CLEAN,
            "tags": ["sofa", "wood floor"],
            "embedding": [0.6, 0.8],
            "modelVersion": "original-scoring-version",
        }
        self.post()
        photo = Listing.objects.get().photos.get(room="kitchen")
        self.assertEqual((photo.tags, photo.embedding), (["sofa", "wood floor"], [0.6, 0.8]))
        self.assertEqual(photo.modelVersion, "original-scoring-version")

    def test_unversioned_photo_results_not(self):
        self.result.pop("modelVersion")
        self.post()
        self.assertTrue(all((photo.modelVersion is None for photo in Listing.objects.get().photos.all())))

    def test_stale_unversioned_cached_photos(self):
        for version in [None, "previous-model"]:
            with self.subTest(version=version):
                self.post(askingPrice="")
                session = self.client.session
                for result in session[DRAFTS_KEY][self.token]["checks"].values():
                    result["modelVersion"] = version
                session.save()
                self.post(rooms=[])
                flat = Listing.objects.get()
                self.assertEqual(flat.status, "review")
                self.assertTrue(flat.photos.filter(modelVersion=version).exists())
                self.assertContains(self.client.get(reverse("my_listings")), "photo checks are out of date")
                flat.delete()
                self.token = self.client.get(reverse("sell")).context["draftToken"]


class PhotoCheckTests(TestCase):

    def test_review_reasons_cover(self):
        self.assertEqual(photocheck.reviewReasons(None), ["photo check unavailable"])
        self.assertEqual(photocheck.reviewReasons(CLEAN), [])
        self.assertEqual(photocheck.reviewReasons({**CLEAN, "irrelevant": 0.9}), ["may not show the flat"])

    def test_missing_classifier_heads(self):
        with patch.object(photocheck, "heads", return_value=(None, None)):
            result = photocheck.decide(np.zeros((1, 512)), [0.05] * 20)
        self.assertIsNone(result["irrelevant"])
        self.assertEqual(
            photocheck.reviewReasons({**result, "modelVersion": photocheck.MODEL_VERSION}),
            ["The room-photo check could not run", "The AI-image check could not run"],
        )
        self.assertIsNone(result["aiGenerated"])

    def classifierHead(self, ai=False):
        model = MagicMock(
            classes_=np.array([False, True] if ai else ["kitchen", "selfie"]), n_features_in_=512
        )
        model.predict_proba.return_value = np.array([[0.99, 0.01]])
        return {
            "version": photocheck.HEAD_VERSION,
            "model": model,
            "relevant": ["kitchen"],
            "hybrid_confidence": 0.9,
            "thresholds": {"review": 0.5, "block": 0.8},
        }

    def test_missing_unloadable_heads_cached(self):
        for error in [FileNotFoundError("missing"), ValueError("unloadable")]:
            with self.subTest(error=type(error).__name__):
                with patch.object(photocheck, "_heads", None):
                    with patch("joblib.load", side_effect=error) as load:
                        with self.assertLogs("sellerpanel.photocheck", "WARNING"):
                            self.assertEqual(photocheck.heads(), (None, None))
                        self.assertEqual(photocheck.heads(), (None, None))
                        self.assertEqual(load.call_count, 2)

    def test_incompatible_head_artifacts_not(self):
        for ai in [False, True]:
            for problem in ["type", "version", "features", "classes", "thresholds", "model"]:
                with self.subTest(ai=ai, problem=problem):
                    room, generated = (self.classifierHead(), self.classifierHead(True))
                    bad = generated if ai else room
                    if problem == "type":
                        bad = []
                    elif problem == "version":
                        bad["version"] = "old"
                    elif problem == "features":
                        bad["model"].n_features_in_ = 123
                    elif problem == "classes":
                        bad["model"].classes_ = np.array([["wrong"]])
                    elif problem == "thresholds":
                        bad["thresholds"]["review"] = float("nan")
                    else:
                        bad["model"].predict_proba = None
                    artifacts = [room, bad] if ai else [bad, generated]
                    with patch.object(photocheck, "_heads", None):
                        with patch("joblib.load", side_effect=artifacts):
                            with self.assertLogs("sellerpanel.photocheck", "WARNING"):
                                loaded = photocheck.heads()
                    self.assertIsNone(loaded[int(ai)])
                    self.assertIs(loaded[int(not ai)], room if ai else generated)

    def test_valid_classifier_heads_keep(self):
        room, ai = (self.classifierHead(), self.classifierHead(True))
        with patch.object(photocheck, "_heads", None):
            with patch("joblib.load", side_effect=[room, ai]):
                result = photocheck.decide(np.zeros((1, 512)), [0.05] * 20)
        self.assertEqual(
            result,
            {
                "irrelevant": 0.01,
                "label": "kitchen",
                "labelSource": "room-head",
                "aiGenerated": 0.01,
                "roomLabel": "kitchen",
                "roomConfidence": 0.99,
            },
        )
        self.assertEqual(photocheck.reviewReasons({**result, "modelVersion": photocheck.MODEL_VERSION}), [])

    def test_frequency_ai_head_keeps(self):
        room, ai = (self.classifierHead(), self.classifierHead(True))
        ai["frequency"] = True
        ai["model"].n_features_in_ = 515
        with patch.object(photocheck, "_heads", None):
            with patch("joblib.load", side_effect=[room, ai]):
                with patch.object(photocheck, "frequencyFeatures", return_value=[[0.1, 0.2, 0.3]]):
                    result = photocheck.decide(np.zeros((1, 512)), [0.05] * 20)
        self.assertEqual(result["aiGenerated"], 0.01)
        self.assertEqual(ai["model"].predict_proba.call_args.args[0].shape, (1, 515))

    def test_missing_room_head_requires(self):
        with patch.object(photocheck, "heads", return_value=(None, self.classifierHead(True))):
            result = photocheck.decide(np.zeros((1, 512)), [0.05] * 20)
        self.assertEqual(result["aiGenerated"], 0.01)
        self.assertEqual(
            photocheck.reviewReasons({**result, "modelVersion": photocheck.MODEL_VERSION}),
            ["The room-photo check could not run"],
        )

    def test_missing_ai_head_requires(self):
        with patch.object(photocheck, "heads", return_value=(self.classifierHead(), None)):
            result = photocheck.decide(np.zeros((1, 512)), [0.05] * 20)
        self.assertEqual(result["irrelevant"], 0.01)
        self.assertEqual(
            photocheck.reviewReasons({**result, "modelVersion": photocheck.MODEL_VERSION}),
            ["The AI-image check could not run"],
        )

    def test_prediction_failures_invalid(self):
        for ai in [False, True]:
            for output in [
                RuntimeError("failed"),
                [[1.0]],
                [[float("nan"), 0.0]],
                [[1.1, -0.1]],
                [[0.1, 0.1]],
            ]:
                with self.subTest(ai=ai, output=output):
                    room, generated = (self.classifierHead(), self.classifierHead(True))
                    model = (generated if ai else room)["model"]
                    if isinstance(output, Exception):
                        model.predict_proba.side_effect = output
                    else:
                        model.predict_proba.return_value = np.array(output)
                    with patch.object(photocheck, "heads", return_value=(room, generated)):
                        with self.assertLogs("sellerpanel.photocheck", "ERROR"):
                            result = photocheck.decide(np.zeros((1, 512)), [0.05] * 20)
                    self.assertIsNone(result["aiGenerated" if ai else "irrelevant"])
                    self.assertEqual(result["irrelevant" if ai else "aiGenerated"], 0.01)
                    self.assertEqual(
                        len(photocheck.reviewReasons({**result, "modelVersion": photocheck.MODEL_VERSION})),
                        1,
                    )

    def test_blocked_message_formats_non(self):
        self.assertEqual(
            blockNote("other_place", "kitchen"), "This looks like an office or shop, not a home."
        )
        self.assertEqual(blockNote("floor_plan", "bedroom 2"), "This looks like a floor plan, not bedroom 2.")

    def test_disabled_returns_unchecked(self):
        with patch.object(photocheck, "isOn", return_value=False):
            with self.assertLogs("sellerpanel.photocheck", "WARNING"):
                self.assertEqual(photocheck.checkPics([b"a", b"b"]), [None, None])

    def test_trained_floor_plan_prediction(self):
        model = MagicMock(classes_=np.array(["kitchen", "floor_plan", "selfie"]))
        model.predict_proba.return_value = np.array([[0.1, 0.85, 0.05]])
        room = {"model": model, "relevant": ["kitchen", "floor_plan"], "hybrid_confidence": 0.9}
        with patch.object(photocheck, "heads", return_value=(room, None)):
            result = photocheck.decide(np.zeros((1, 512)), [0.05] * 20)
        self.assertEqual(result["label"], "floor_plan")
        self.assertAlmostEqual(result["irrelevant"], 0.9)

    @patch("sellerpanel.photocheck.verifyModel", new=lambda *args, **kwargs: "verified")
    def test_batch_runs_one_child(self):
        done = MagicMock(returncode=0, stdout='[{"label": "kitchen"}, {"label": "toilet"}]\n')
        with patch.object(photocheck, "isOn", return_value=True):
            with patch("subprocess.run", return_value=done) as run:
                results = photocheck.checkPics([b"a", b"b"])
        self.assertEqual(results, [{"label": "kitchen"}, {"label": "toilet"}])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(len(run.call_args[0][0]), 5)

    @patch("sellerpanel.photocheck.verifyModel", new=lambda *args, **kwargs: "verified")
    def test_timeout_child_failure_return(self):
        timeout = photocheck.subprocess.TimeoutExpired("python", photocheck.TIMEOUT)
        crash = MagicMock(returncode=1, stdout="", stderr="killed")
        with patch.object(photocheck, "isOn", return_value=True):
            with self.assertLogs("sellerpanel.photocheck", "ERROR"):
                with patch("subprocess.run", side_effect=timeout):
                    self.assertEqual(photocheck.checkPics([b"a", b"b"]), [None, None])
                with patch("subprocess.run", return_value=crash):
                    self.assertEqual(photocheck.checkPics([b"a"]), [None])
        self.assertEqual(photocheck.reviewReasons(None), ["photo check unavailable"])

    def test_clip_floor_plan_prediction(self):
        chances = [0.0] * 20
        chances[7] = 1.0
        with patch.object(photocheck, "heads", return_value=(None, None)):
            result = photocheck.decide(np.zeros((1, 512)), chances)
        self.assertEqual(result["label"], "floor plan")
        self.assertIsNone(result["irrelevant"])
        self.assertIn("The room-photo check could not run", photocheck.reviewReasons(result))

    def test_confident_clip_balcony(self):
        model = MagicMock(classes_=np.array(["kitchen", "balcony"]))
        model.predict_proba.return_value = np.array([[0.2, 0.8]])
        room = {"model": model, "relevant": ["kitchen"], "hybrid_confidence": 0.9}
        chances = [0.0] * 20
        chances[4] = 0.95
        with patch.object(photocheck, "heads", return_value=(room, None)):
            result = photocheck.decide(np.zeros((1, 512)), chances)
        self.assertEqual((result["label"], result["irrelevant"]), ("balcony", 0.0))

    def test_tag_selection_keeps_high(self):
        chances = [0.0] * len(photocheck.TAGS)
        chances[0] = 0.5
        chances[2] = 0.3
        chances[9] = 0.12
        chances[10] = 0.05
        self.assertEqual(photocheck.pickTags(chances), ["sofa", "bed", "wood floor"])


class TagCommandTests(TestCase):

    def test_processes_untagged_live_listing(self):
        seller = makeUser("asdf@test.com", "seller")
        live = ListingPhoto(listing=makeListing(seller), room="kitchen")
        live.image.save("kitchen.jpg", jpeg())
        hidden = ListingPhoto(
            listing=makeListing(makeUser("held@test.com", "seller"), status="review"),
            room="kitchen",
        )
        hidden.image.save("kitchen.jpg", jpeg())
        found = {
            **CLEAN,
            "tags": ["kitchen cabinets"],
            "embedding": [1.0, 0.0],
            "modelVersion": photocheck.MODEL_VERSION,
        }
        with patch(
            "sellerpanel.management.commands.tag_listing_photos.checkPics", return_value=[found]
        ) as check:
            call_command("tag_listing_photos", stdout=MagicMock())
            call_command("tag_listing_photos", stdout=MagicMock())
        live.refresh_from_db()
        hidden.refresh_from_db()
        self.assertEqual((live.tags, live.embedding), (["kitchen cabinets"], [1.0, 0.0]))
        self.assertIsNone(hidden.embedding)
        self.assertIsNone(hidden.modelVersion)
        self.assertEqual(live.modelVersion, photocheck.MODEL_VERSION)
        self.assertEqual(check.call_count, 1)

    def test_refreshes_older_unversioned(self):
        seller = makeUser("asdf@test.com", "seller")
        flat = makeListing(seller)
        for room, version in [("kitchen", None), ("living_room", "older")]:
            photo = ListingPhoto(listing=flat, room=room, embedding=[0.0, 1.0], modelVersion=version)
            photo.image.save(f"{room}.jpg", jpeg())
        found = {
            **CLEAN,
            "irrelevant": None,
            "tags": ["sofa"],
            "embedding": [1.0, 0.0],
            "modelVersion": photocheck.MODEL_VERSION,
        }
        with patch(
            "sellerpanel.management.commands.tag_listing_photos.checkPics",
            return_value=[found, found],
        ) as check:
            call_command("tag_listing_photos", stdout=MagicMock())
            call_command("tag_listing_photos", stdout=MagicMock())
        self.assertEqual(check.call_count, 1)
        for photo in flat.photos.all():
            self.assertEqual(photo.embedding, [1.0, 0.0])
            self.assertEqual(photo.modelVersion, photocheck.MODEL_VERSION)
            self.assertEqual(photo.aiLabel, CLEAN["label"])
            self.assertIsNone(photo.aiIrrelevant)
            self.assertEqual(photo.aiGenerated, CLEAN["aiGenerated"])


class UploadTests(CompletedPhotoQueueMixin, TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.client.force_login(self.seller)
        self.token = self.client.get(reverse("sell")).context["draftToken"]
        checker = patch(
            "sellerpanel.photos.checkPics", side_effect=lambda photos: [dict(CLEAN)] * len(photos)
        )
        checker.start()
        self.addCleanup(checker.stop)

    def post(self, **files):
        return self.client.post(reverse("sell"), {"draftToken": self.token, **files})

    def kept(self):
        return self.client.session[DRAFTS_KEY][self.token]["photos"]

    def test_removing_draft_photo_deletes(self):
        self.post(photo_kitchen=jpeg())
        path = self.kept()["kitchen"]
        self.assertTrue(default_storage.exists(path))
        self.post(removePhoto="kitchen")
        self.assertEqual(self.kept(), {})
        self.assertFalse(default_storage.exists(path))

    def test_oversized_photo_shows_error(self):
        big = SimpleUploadedFile("big.jpg", b"x" * (10 * 1024 * 1024 + 1), content_type="image/jpeg")
        response = self.post(photo_kitchen=big)
        slots = {slot["key"]: slot for slot in response.context["photoSlots"]}
        self.assertIn("too big", slots["kitchen"]["error"])

    def test_replacing_draft_photo_deletes(self):
        self.post(photo_kitchen=jpeg())
        first = self.kept()["kitchen"]
        self.post(photo_kitchen=jpeg("red"))
        self.assertNotEqual(self.kept()["kitchen"], first)
        self.assertFalse(default_storage.exists(first))

    def test_expired_draft_removed_replaced(self):
        session = self.client.session
        session[DRAFTS_KEY][self.token]["created"] = 0
        session.save()
        token = self.client.get(reverse("sell")).context["draftToken"]
        self.assertNotIn(self.token, self.client.session[DRAFTS_KEY])
        self.assertNotEqual(token, self.token)


class LiveCheckTests(CompletedPhotoQueueMixin, TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.client.force_login(self.seller)
        self.token = self.client.get(reverse("sell")).context["draftToken"]
        self.result = dict(CLEAN)
        checker = patch(
            "sellerpanel.photos.checkPics", side_effect=lambda photos: [self.result] * len(photos)
        )
        self.checker = checker.start()
        self.addCleanup(checker.stop)
        price = patch("sellerpanel.listings.band", return_value=FAIR)
        self.band = price.start()
        self.addCleanup(price.stop)

    def check(self, room="kitchen", photo=None, **data):
        if photo is None:
            photo = jpeg()
        return self.client.post(
            reverse("photo_check"), {"draftToken": self.token, "room": room, "photo": photo, **data}
        )

    def test_accepted_photo_reused_listing(self):
        reply = self.check().json()
        self.assertEqual(reply, {"verdict": "accept", "message": "This looks like a photo of the flat."})
        self.assertIn("kitchen", self.client.session[DRAFTS_KEY][self.token]["photos"])
        data = {**FLAT, "draftToken": self.token}
        for room in ROOMS:
            if room != "kitchen":
                data[f"photo_{room}"] = jpeg()
        self.client.post(reverse("sell"), data)
        flat = Listing.objects.get()
        self.assertEqual((flat.status, flat.photos.count()), ("live", 5))
        self.assertEqual(self.checker.call_count, 2)
        self.assertEqual(len(self.checker.call_args[0][0]), 4)
        self.assertNotIn(self.token, self.client.session[DRAFTS_KEY])

    def test_blocked_photo_not_saved(self):
        self.result = {"label": "selfie", "irrelevant": 0.99, "aiGenerated": 0.01}
        reply = self.check(room="bedroom_2").json()
        self.assertEqual(reply["verdict"], "block")
        self.assertIn("This looks like a selfie, not bedroom 2.", reply["message"])
        self.assertEqual(self.client.session[DRAFTS_KEY][self.token]["photos"], {})

    def test_photo_handles_unavailable_model(self):
        self.result = None
        self.assertEqual(self.check().json()["verdict"], "review")
        text = SimpleUploadedFile("a.txt", b"asdf", content_type="text/plain")
        response = self.check(photo=text)
        self.assertEqual((response.status_code, response.json()["verdict"]), (400, "invalid"))
        self.assertEqual(self.check(room="roof").status_code, 400)
        self.assertEqual(self.check(draftToken="qwerty").status_code, 400)
        self.assertEqual(self.client.get(reverse("photo_check")).status_code, 405)
        self.client.force_login(makeUser("qwer@test.com"))
        self.assertEqual(self.check().status_code, 403)

    def test_concurrent_photo_returns_rate(self):
        cache.add(f"photo-check:{self.seller.pk}", 1, 60)
        self.addCleanup(cache.delete, f"photo-check:{self.seller.pk}")
        self.assertEqual(self.check().status_code, 429)
        self.assertEqual(self.checker.call_count, 0)

    def test_price_returns_range_flags(self):
        fields = {}
        for name in [
            "postalCode",
            "block",
            "flatType",
            "floorAreaSqm",
            "storey",
            "leaseCommenceYear",
        ]:
            fields[name] = FLAT[name]
        reply = self.client.get(reverse("sell_price_check"), {**fields, "askingPrice": "600000"}).json()
        self.assertEqual((reply["lo"], reply["hi"], reply["flag"]), (350000, 450000, "above"))
        self.assertIn("Your price is on the high side", reply["html"])
        self.assertEqual(self.band.call_args[0][:3], ("JURONG EAST", "3 ROOM", "New Generation"))
        reply = self.client.get(reverse("sell_price_check"), {"flatType": "3 ROOM"}).json()
        self.assertIsNone(reply["lo"])
        self.assertIn("Is your price fair?", reply["html"])
        self.band.side_effect = ValueError("asdf")
        reply = self.client.get(reverse("sell_price_check"), fields).json()
        self.assertIn("We can’t compare this flat yet.", reply["html"])

    def test_sell_form_shows_size(self):
        self.assertEqual(sizeHints()["4 ROOM"], {"label": "4-room", "lo": 66, "hi": 125})
        response = self.client.get(reverse("sell"))
        for check_text_4 in ('id="size-hints"', "sellerpanel/listing-photos.js", "data-price-check="):
            self.assertContains(response, check_text_4)
