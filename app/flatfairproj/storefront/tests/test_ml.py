import ast
import inspect
import io
import tempfile
import textwrap
from unittest.mock import MagicMock
from django.core.management import call_command
from sellerpanel.management.commands import download_photo_model
from vision.management.commands import download_vision_models
from datetime import timedelta
from unittest.mock import patch, Mock
from django.core.cache import caches
from django.test import TestCase, SimpleTestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone
from storefront import lens, valuation
from storefront.models import FlatView, Listing, ListingPhoto, Offer
from storefront.templatetags.estate_tags import fixedFeatures
from .helpers import CLEAN, jpeg, makeListing, makeStaff, makeUser
import hashlib
import json
import pretrained
import numpy
from pathlib import Path
from tempfile import TemporaryDirectory
from vision.pipeline import analyse
from pretrained import PROJECT, TRAINING, warnGone
from decimal import Decimal
from types import SimpleNamespace
from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError, transaction
from sellerpanel import photocheck, recommend
from storefront.tests.helpers import makeListing, makeUser


class LensPageTests(TestCase):

    def setUp(self):
        for name in ("default", "resources"):
            caches[name].clear()
            self.addCleanup(caches[name].clear)
        self.flat = makeListing(makeUser("lens-seller@test.com", "seller"))
        self.url = reverse("lens")
        checker = patch("storefront.lens.checkPics", side_effect=AssertionError("Photo model must not run"))
        self.checker = checker.start()
        self.addCleanup(checker.stop)
        self.addCleanup(self.checker.assert_not_called)

    def photo(self, room="living_room", flat=None, **fields):
        values = {
            "listing": flat or self.flat,
            "room": room,
            "image": f"sample/{room}.jpg",
            "embedding": [1.0] + [0.0] * (lens.EMBEDDING_SIZE - 1),
            "modelVersion": lens.MODEL_VERSION,
            "aiIrrelevant": 0.01,
        }
        values.update(fields)
        return ListingPhoto.objects.create(**values)

    def samples(self):
        kitchen = self.photo("kitchen", tags=["kitchen cabinets", "tiled floor", "sofa"])
        living = self.photo(tags=["wood floor", "ceiling fan", "tiled floor"])
        bedroom = self.photo("master_bedroom", tags=["built-in wardrobe", "built-in wardrobes", "wood floor"])
        bathroom = self.photo("common_toilet", tags=["tiled floor", "water heater"])
        balcony = self.photo("balcony", tags=["tiled floor", "ceiling fan"])
        other = self.photo("other", tags=["tiled floor"])
        self.photo("bedroom_2", tags=["kitchen cabinets"])
        return [living, kitchen, bedroom, bathroom, balcony, other]

    def test_guest_samples_deterministic(self):
        expected = self.samples()
        response = self.client.get(self.url)
        self.assertEqual(response.context["lensSamples"], expected)
        self.assertEqual(self.client.get(self.url).context["lensSamples"], expected)
        self.assertContains(response, "Try a sample")
        self.assertContains(response, 'class="lens-sample"', count=6)
        for photo in expected:
            self.assertContains(response, f'href="{self.url}?photo={photo.pk}#results"')
            self.assertContains(response, f'alt="{photo.get_room_display()} in a flat in Yishun"')
            self.assertContains(response, f"<figcaption>{photo.get_room_display()}")
        for check_text_1 in (
            "Choose a room photo",
            'accept="image/jpeg,image/png"',
            "How it works",
            "We look for similar rooms in flats for sale",
        ):
            self.assertContains(response, check_text_1)

    def test_no_samples_popular_looks(self):
        for withPhoto in (False, True):
            with self.subTest(withPhoto=withPhoto):
                if withPhoto:
                    self.samples()
                    Listing.objects.filter(pk=self.flat.pk).update(status="sold", statusBeforeSold="live")
                response = self.client.get(self.url)
                self.assertEqual(response.context["lensSamples"], [])
                self.assertEqual(response.context["popularLooks"], [])
                self.assertNotContains(response, "Try a sample")
                self.assertNotContains(response, "Popular looks")

    def test_samples_exclude_non_public(self):
        valid = self.photo()
        for room, fields in (
            ("kitchen", {"modelVersion": "old"}),
            ("master_bedroom", {"embedding": None}),
            ("bedroom_2", {"embedding": [1.0]}),
            ("common_toilet", {"aiIrrelevant": None}),
            ("balcony", {"aiIrrelevant": 2}),
            ("other", {"image": ""}),
        ):
            self.photo(room, tags=["bathtub"], **fields)
        for index, status in enumerate(("hidden", "review", "rejected", "sold", "live")):
            seller = makeUser(f"lens-private{index}@test.com", "seller")
            flat = makeListing(seller, status=status)
            if status == "live":
                seller.is_active = False
                seller.save(update_fields=["is_active"])
            self.photo(flat=flat, tags=["bathtub"])
        response = self.client.get(self.url)
        self.assertEqual(response.context["lensSamples"], [valid])
        self.assertEqual(response.context["popularLooks"], [])

    def test_samples_fill_six_some(self):
        first = self.photo()
        expected = [first]
        for index in range(6):
            flat = makeListing(makeUser(f"lens-fill{index}@test.com", "seller"))
            expected.append(self.photo(flat=flat))
        response = self.client.get(self.url)
        self.assertEqual(response.context["lensSamples"], expected[:6])

    def test_popular_looks_count_photos(self):
        self.samples()
        response = self.client.get(self.url)
        looks = response.context["popularLooks"]
        self.assertEqual(
            [look["label"] for look in looks],
            ["Tiled flooring", "Ceiling fan", "Kitchen cabinets", "Wood flooring"],
        )
        for look in looks:
            photo = look["photo"]
            self.assertIn(look["label"], [feature["label"] for feature in fixedFeatures(photo.tags)])
            with patch("storefront.lens.similarFlats", return_value=[]) as search:
                result = self.client.get(self.url, {"photo": photo.pk})
            self.assertTrue(result.context["searched"])
            search.assert_called_once_with(photo.embedding, photo.listing)
            self.assertContains(response, f'href="{self.url}?photo={photo.pk}#results"')

    def test_popular_looks_deduplicate(self):
        self.photo(tags=["built-in wardrobe", "built-in wardrobes", "built-in wardrobe"])
        self.photo("kitchen", tags=["kitchen cabinets"])
        self.photo("other", tags=["kitchen cabinets"])
        response = self.client.get(self.url)
        self.assertEqual(
            [look["label"] for look in response.context["popularLooks"]],
            ["Kitchen cabinets", "Built-in wardrobes"],
        )

    def test_search_keeps_thumbnail_feature(self):
        samples = self.samples()
        with patch(
            "storefront.lens.similarFlats",
            return_value=[{"flat": self.flat, "photo": samples[1], "closeness": "Very similar"}],
        ):
            response = self.client.get(self.url, {"photo": samples[0].pk})
        self.assertContains(response, 'alt="Room photo used for this search"')
        self.assertContains(response, '<li class="chip">wood floor</li>', html=True)
        content = response.content.decode()
        self.assertLess(
            content.index('id="lens-yours-title">Your photo<'),
            content.index('class="lens-results"'),
        )
        self.assertLess(content.index('class="lens-results"'), content.index("Try another sample photo"))
        self.assertNotContains(response, ">Try a sample")
        for check_text_2 in (
            "<figcaption>Living room, Blk 123 Yishun</figcaption>",
            '<h2 id="lens-results-title">Similar rooms</h2>',
            '<p class="result-count">1 flat</p>',
        ):
            self.assertContains(response, check_text_2, html=True)
        self.assertContains(response, "Use another photo")
        self.assertContains(response, "Or pick a look")
        self.assertContains(response, 'aria-current="true"', count=1)
        current = content.split('aria-current="true"', 1)[0].rsplit("<a ", 1)[1]
        self.assertIn(f"?photo={samples[0].pk}#results", current)
        self.assertContains(response, '<span class="lens-current">Current</span>', html=True)

    def test_result_card_shows_matched(self):
        samples = self.samples()
        other = makeListing(makeUser("lens-match@test.com", "seller"), block="45", town="BEDOK")
        cover = self.photo(flat=other)
        toilet = self.photo("common_toilet", flat=other)
        with patch(
            "storefront.lens.similarFlats",
            return_value=[
                {"flat": other, "photo": toilet, "closeness": "Very similar"},
                {"flat": self.flat, "photo": samples[1], "closeness": "Similar"},
            ],
        ):
            response = self.client.get(self.url, {"photo": samples[3].pk})
        self.assertEqual(other.coverPhoto(), cover)
        results = response.content.decode().split('class="lens-results"', 1)[1].split("</section>", 1)[0]
        card = results.split('class="card-wrap"', 2)[1]
        self.assertIn(f'src="{toilet.publicUrl}"', card)
        self.assertNotIn(cover.publicUrl, card)
        self.assertIn('alt="Common toilet in Blk 45, Bedok"', card)
        self.assertIn('<span class="lens-room">Common toilet</span>', card)
        self.assertIn('<span class="lens-match lens-match--very">Very similar</span>', card)
        self.assertIn("$550,000", card)
        number = [photo.pk for photo in other.orderedPhotos()].index(toilet.pk) + 1
        self.assertIn(f'href="{reverse('listing_detail', args=[other.publicId])}#photo-{number}"', card)
        self.assertIn('<span class="lens-match">Similar</span>', results)
        self.assertIn('<span class="lens-room">Kitchen</span>', results)

    def test_nine_results_show_more(self):
        source = self.photo()
        matches = []
        for index in range(11):
            flat = makeListing(makeUser(f"lens-many{index}@test.com", "seller"), block=str(500 + index))
            matches.append({"flat": flat, "photo": self.photo("kitchen", flat=flat), "closeness": "Similar"})
        with patch("storefront.lens.similarFlats", return_value=matches):
            response = self.client.get(self.url, {"photo": source.pk})
        self.assertContains(response, '<p class="result-count">11 flats</p>', html=True)
        first, more = response.content.decode().split('<details class="fold lens-more">', 1)
        self.assertEqual(first.count('class="card-wrap"'), 9)
        self.assertIn("<summary>Show 2 more</summary>", more)
        self.assertEqual(more.split("</details>", 1)[0].count('class="card-wrap"'), 2)

    def test_empty_search_keeps_friendly(self):
        photo = self.photo()
        with patch("storefront.lens.similarFlats", return_value=[]):
            response = self.client.get(self.url, {"photo": photo.pk})
        self.assertContains(response, "<h3>No similar rooms yet</h3>", html=True)
        self.assertContains(response, 'class="empty-icon"', count=1)
        self.assertContains(
            response, f'<a class="btn btn-brand" href="{self.url}">Use another photo</a>', html=True
        )
        self.assertNotContains(response, 'class="result-count"')
        self.assertContains(response, "Try another sample photo")

    def test_upload_keeps_your_photo(self):
        self.photo()
        result = {
            **CLEAN,
            "embedding": [1.0] + [0.0] * (lens.EMBEDDING_SIZE - 1),
            "tags": ["tiled floor"],
        }
        with (
            patch("storefront.lens.checkPics", return_value=[result]),
            patch("storefront.lens.similarFlats", return_value=[]),
        ):
            response = self.client.post(self.url, {"photo": jpeg()})
        self.assertContains(response, '<h2 id="lens-yours-title">Your photo</h2>', html=True)
        self.assertContains(response, "<figcaption>Your upload</figcaption>", html=True)
        self.assertContains(response, 'alt="Your submitted room photo"')
        self.assertContains(response, '<li class="chip">tiled floor</li>', html=True)
        self.assertContains(response, "Try another sample")

    def test_recommendations_render_eligible(self):
        buyer = makeUser("lens-buyer@test.com")
        other = makeListing(makeUser("lens-other@test.com", "seller"))
        candidate = makeListing(makeUser("lens-candidate@test.com", "seller"))
        for flat in (self.flat, other):
            FlatView.objects.create(
                buyer=buyer, listing=flat, lastViewedAt=timezone.now() - timedelta(days=2)
            )
        self.assertNotContains(self.client.get(self.url), "Recommended for you")
        self.client.force_login(buyer)
        response = self.client.get(self.url)
        self.assertContains(response, "Recommended for you")
        self.assertIn(candidate, response.context["recommendedFlats"])
        for user in (
            makeUser("lens-new@test.com"),
            self.flat.seller,
            makeStaff("lens-staff@test.com"),
        ):
            self.client.force_login(user)
            self.assertNotContains(self.client.get(self.url), "Recommended for you")
        buyer.profile.canSell = True
        buyer.profile.save()
        self.client.force_login(buyer)
        session = self.client.session
        session["marketplaceMode"] = "seller"
        session.save()
        self.assertNotContains(self.client.get(self.url), "Recommended for you")


class PretrainedIdentityTests(SimpleTestCase):

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        (self.folder / "config.json").write_bytes(b"config-v1")
        (self.folder / "model.safetensors").write_bytes(b"weights-v1")
        self.entry = {
            "revision": "a" * 40,
            "status": "installed",
            "files": [
                {
                    "path": file.name,
                    "sizeBytes": file.stat().st_size,
                    "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                }
                for file in self.folder.iterdir()
            ],
        }

    def verify(self, revision=None):
        return pretrained.verifyModel(pretrained.CLIP_NAME, self.folder, revision or "a" * 40, self.entry)

    def test_cached_hashes_matching_files(self):
        self.assertEqual(self.verify(), "verified")
        before = pretrained.saved_hash.cache_info()
        self.assertEqual(self.verify(), "verified")
        after = pretrained.saved_hash.cache_info()
        self.assertEqual(before.misses, after.misses)
        self.assertGreater(after.hits, before.hits)

    def test_changed_config_same_size(self):
        for name in ["config.json", "model.safetensors"]:
            with self.subTest(file=name):
                self.assertEqual(self.verify(), "verified")
                file = self.folder / name
                original = file.read_bytes()
                file.write_bytes(b"x" * len(original))
                self.assertEqual(self.verify(), "identity mismatch")
                file.write_bytes(original)

    def test_unexpected_config_mismatch(self):
        (self.folder / "preprocessor_config.json").write_bytes(b"unexpected")
        self.assertEqual(self.verify(), "identity mismatch")

    def test_missing_file_folder_revision(self):
        self.assertEqual(self.verify("b" * 40), "identity mismatch")
        (self.folder / "model.safetensors").unlink()
        self.assertEqual(self.verify(), "identity mismatch")
        self.assertEqual(
            pretrained.verifyModel(pretrained.CLIP_NAME, self.folder / "absent", "a" * 40, self.entry),
            "not installed",
        )

    def test_unrecorded_installation_not(self):
        self.entry["status"] = "not installed"
        self.assertEqual(self.verify(), "identity mismatch")

    def test_vision_mismatch_stops_reading(self):
        with patch("pretrained.tagErrors", return_value=["CLIP: identity mismatch"]):
            with self.assertRaisesRegex(ValueError, "identity mismatch; staff review required"):
                analyse("absent-photo.jpg")

    def test_manifest_records_pinned(self):
        manifest = json.loads(pretrained.MANIFEST.read_text())
        if (pretrained.ML / "clip").is_dir():
            self.assertEqual(
                manifest["clip"]["photoModelVersion"],
                pretrained.picVersion(
                    pretrained.ML / "clip",
                    pretrained.CLIP_NAME,
                    pretrained.REVISIONS[pretrained.CLIP_NAME],
                ),
            )
        self.assertEqual(len(manifest["pretrained"]), len(pretrained.PINNED_REVISIONS))
        for entry in manifest["pretrained"]:
            with self.subTest(model=entry["modelId"]):
                self.assertEqual(entry["revision"], pretrained.PINNED_REVISIONS[entry["modelId"]])
                self.assertRegex(entry["revision"], "^[a-f0-9]{40}$")
                self.assertIn(entry["status"], ["installed", "not installed"])
                if entry["status"] == "installed":
                    if (pretrained.ML / entry["localPath"]).is_dir():
                        self.assertEqual(pretrained.verifyModel(entry["modelId"]), "verified")
                    self.assertIn("config.json", {file["path"] for file in entry["files"]})
                    self.assertIn("model.safetensors", {file["path"] for file in entry["files"]})
                for file in entry["files"]:
                    self.assertRegex(file["sha256"], "^[a-f0-9]{64}$")
                    self.assertGreater(file["sizeBytes"], 0)


class ModelManifestTests(SimpleTestCase):

    def test_covers_artifacts_matches_hashes(self):
        manifest = json.loads((TRAINING / "model_manifest.json").read_text())
        entries = manifest["artifacts"]
        actual = {str(path.relative_to(PROJECT)) for path in (PROJECT / "assets" / "ml").rglob("*.joblib")}
        self.assertEqual({entry["path"] for entry in entries}, actual)
        self.assertEqual(len(entries), len(actual))
        for entry in entries:
            with self.subTest(artifact=entry["path"]):
                path = PROJECT / entry["path"]
                with path.open("rb") as stream:
                    self.assertEqual(hashlib.file_digest(stream, "sha256").hexdigest(), entry["sha256"])
                self.assertEqual(path.stat().st_size, entry["sizeBytes"])
                self.assertTrue(entry["version"])
                self.assertTrue((TRAINING / entry["trainingScript"]).is_file())
                for metrics in entry["metricsFiles"]:
                    self.assertTrue((TRAINING / metrics).is_file())


class MlLoggingTests(SimpleTestCase):

    def setUp(self):
        warnGone.cache_clear()
        recommend.loadPackageModel.cache_clear()
        self.addCleanup(warnGone.cache_clear)
        self.addCleanup(recommend.loadPackageModel.cache_clear)

    def test_price_unavailable_warns_once(self):
        for error in (FileNotFoundError("missing"), ValueError("schema")):
            warnGone.cache_clear()
            with (
                patch.object(valuation, "_CACHE", None),
                patch.object(valuation, "_CACHE_SIGNATURE", None),
            ):
                with (
                    patch.object(valuation, "ARTIFACT") as artifact,
                    patch.object(valuation.joblib, "load", side_effect=error),
                ):
                    artifact.stat.return_value = SimpleNamespace(st_mtime_ns=1, st_size=1)
                    with patch.object(valuation, "log") as log:
                        self.assertFalse(valuation.modelInfo()["available"])
                        self.assertFalse(valuation.modelInfo()["available"])
                        log.warning.assert_called_once()
                        log.exception.assert_not_called()

    def test_price_schema_mismatch_warns(self):
        for candidate, error in (({"version": "old"}, None), (None, RuntimeError("bug"))):
            with (
                patch.object(valuation, "_CACHE", None),
                patch.object(valuation, "ARTIFACT") as artifact,
            ):
                artifact.stat.return_value = SimpleNamespace(st_mtime_ns=1, st_size=1)
                with patch.object(valuation.joblib, "load", return_value=candidate, side_effect=error):
                    with patch.object(valuation, "log") as log:
                        self.assertFalse(valuation.modelInfo()["available"])
                        if error:
                            log.exception.assert_called_once()
                            log.warning.assert_not_called()
                        else:
                            log.warning.assert_called_once()
                            log.exception.assert_not_called()

    def test_price_prediction_bug_keeps(self):
        with (
            patch.object(valuation, "makeRow"),
            patch.object(
                valuation,
                "_load",
                return_value={"models": {"median": Mock(predict=Mock(side_effect=RuntimeError("bug")))}},
            ),
        ):
            with patch.object(valuation, "log") as log:
                with self.assertRaisesMessage(
                    ValueError, "Sorry, we couldn’t work out a price for this flat."
                ):
                    valuation.band("BEDOK", "4 ROOM", "Model A", 92, 8, 1990)
                log.exception.assert_called_once()

    def test_price_feature_serving_bug(self):
        with (
            patch.object(valuation, "makeRow", side_effect=RuntimeError("bug")),
            patch.object(valuation, "log") as log,
        ):
            with self.assertRaisesMessage(ValueError, "Sorry, we couldn’t work out a price for this flat."):
                valuation.band("BEDOK", "4 ROOM", "Model A", 92, 8, 1990)
            log.exception.assert_called_once()

    def test_package_unavailable_warns_once(self):
        for error in (FileNotFoundError("missing"), RuntimeError("bug")):
            recommend.loadPackageModel.cache_clear()
            with patch("joblib.load", side_effect=error), patch.object(recommend, "log") as log:
                self.assertIsNone(recommend.loadPackageModel())
                self.assertIsNone(recommend.loadPackageModel())
                if isinstance(error, FileNotFoundError):
                    log.warning.assert_called_once()
                    log.exception.assert_not_called()
                else:
                    log.exception.assert_called_once()
                    log.warning.assert_not_called()

    def test_package_schema_serving_failures(self):
        with (
            patch("joblib.load", return_value={"version": "old"}),
            patch.object(recommend, "log") as log,
        ):
            self.assertIsNone(recommend.loadPackageModel())
            log.warning.assert_called_once()
            log.exception.assert_not_called()
        flat = SimpleNamespace(status="live")
        with (
            patch.object(recommend, "loadPackageModel", side_effect=RuntimeError("bug")),
            patch.object(recommend, "log") as log,
        ):
            self.assertIsNone(recommend.bestPack(flat))
            log.exception.assert_called_once()

    def test_heads_unavailable_schema_warn(self):
        for error, candidate in (
            (FileNotFoundError("missing"), None),
            (None, {}),
            (RuntimeError("bug"), None),
        ):
            warnGone.cache_clear()
            with (
                patch.object(photocheck, "_heads", None),
                patch("joblib.load", side_effect=error, return_value=candidate),
            ):
                with patch.object(photocheck, "log") as log:
                    self.assertEqual(photocheck.heads(), (None, None))
                    self.assertEqual(photocheck.heads(), (None, None))
                    if isinstance(error, RuntimeError):
                        self.assertEqual(log.exception.call_count, 2)
                        log.warning.assert_not_called()
                    else:
                        self.assertEqual(log.warning.call_count, 2)
                        log.exception.assert_not_called()

    def test_photo_identity_mismatch_warns(self):
        with (
            patch.object(photocheck, "isOn", return_value=True),
            patch.object(photocheck, "verifyModel", return_value="identity mismatch"),
        ):
            with (
                patch.object(photocheck, "log") as log,
                patch.object(photocheck.subprocess, "run") as run,
            ):
                for index in range(2):
                    self.assertEqual(
                        photocheck.checkPics([b"photo"])[0]["identityStatus"], "identity mismatch"
                    )
                log.warning.assert_called_once()
                log.exception.assert_not_called()
                run.assert_not_called()

    def test_photo_serving_bug_falls(self):
        with (
            patch.object(photocheck, "isOn", side_effect=RuntimeError("bug")),
            patch.object(photocheck, "log") as log,
        ):
            self.assertEqual(photocheck.checkPics([b"photo"]), [None])
            log.exception.assert_called_once()

    def test_lens_invalid_artifact_warns(self):
        photo = SimpleNamespace(modelVersion="old", embedding=None)
        with (
            patch.object(lens.Listing.objects, "public"),
            patch.object(lens.ListingPhoto.objects, "filter") as photos,
        ):
            photos.return_value.select_related.return_value.first.return_value = photo
            with patch.object(lens, "log") as log:
                for index in range(2):
                    context = {}
                    lens.fromPic(RequestFactory().get("/lens", {"photo": 1}), context)
                    self.assertEqual(context["error"], lens.FAILED)
                log.warning.assert_called_once()
                log.exception.assert_not_called()

    def test_lens_programming_error_logs(self):
        with (
            patch.object(lens, "embeddingMatrix", side_effect=RuntimeError("bug")),
            patch.object(lens, "log") as log,
        ):
            self.assertEqual(lens.similarFlats([1.0] + [0.0] * (lens.EMBEDDING_SIZE - 1)), [])
            log.exception.assert_called_once()


class OfferConstraintTests(TestCase):

    def setUp(self):
        self.flat = makeListing(None)
        self.buyer = makeUser("constraint@test.com")
        self.offer = Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=500000)

    def test_direct_updates_reject_invalid(self):
        invalid = [
            {"amount": 0},
            {"amount": -1},
            {"amount": Decimal("500000.50")},
            {"amount": 3000001},
            {"counterAmount": 525000},
            {"status": "countered", "counterAmount": None, "respondedAt": timezone.now()},
            {"status": "countered", "counterAmount": 500000, "respondedAt": timezone.now()},
            {
                "status": "countered",
                "counterAmount": Decimal("525000.50"),
                "respondedAt": timezone.now(),
            },
            {"status": "countered", "counterAmount": 3000001, "respondedAt": timezone.now()},
        ]
        invalid.extend(
            (
                {"status": status, "respondedAt": None}
                for status in ("declined", "withdrawn", "accepted", "countered")
            )
        )
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Offer.objects.filter(pk=self.offer.pk).update(**values)

    def test_closed_retain_counter_history(self):
        for status in ("countered", "declined", "withdrawn", "accepted"):
            Offer.objects.filter(pk=self.offer.pk).update(
                status=status, counterAmount=525000, respondedAt=timezone.now()
            )
        Offer.objects.filter(pk=self.offer.pk).update(status="accepted", amount=525000, counterAmount=None)
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.amount, 525000)
        self.assertIsNone(self.offer.counterAmount)

    def test_direct_create_blocks_invalid(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Offer.objects.create(
                listing=self.flat,
                buyer=makeUser("otherconstraint@test.com"),
                amount=500000,
                status="declined",
            )


class PhotoMatrixTests(TestCase):

    def setUp(self):
        caches["resources"].clear()
        self.addCleanup(caches["resources"].clear)
        self.flats = [makeListing(makeUser(f"matrix{index}@test.com", "seller")) for index in range(4)]
        self.photos = [
            self.photo(self.flats[index], values)
            for index, values in enumerate(([1, 0], [0.8, 0.6], [0.6, 0.8], [0, 1]))
        ]
        self.photo(self.flats[0], [0, 1])

    def vector(self, values):
        return values + [0.0] * (lens.EMBEDDING_SIZE - len(values))

    def photo(self, flat, values):
        room = "kitchen" if flat.photos.exists() else "living_room"
        return ListingPhoto.objects.create(
            listing=flat,
            room=room,
            image="test.jpg",
            embedding=self.vector(values),
            modelVersion=lens.MODEL_VERSION,
            aiIrrelevant=0.01,
        )

    def test_vectorized_results_match_scalar(self):
        query = lens.validEmbedding(self.vector([1, 0]))
        best = {}
        for photo in ListingPhoto.objects.order_by("pk"):
            score = float(lens.validEmbedding(photo.embedding) @ query)
            if photo.listing_id not in best or score > best[photo.listing_id][1]:
                best[photo.listing_id] = (photo.pk, score)
        expected = sorted(best.values(), key=lambda match: match[1], reverse=True)
        actual = lens.similarFlats(query)
        self.assertEqual([match["photo"].pk for match in actual], [pk for pk, score in expected])
        numpy.testing.assert_allclose(
            [match["score"] for match in actual], [score for pk, score in expected], atol=1e-14
        )

    def test_cached_invalidated_by_create(self):
        ids, matrix = lens.embeddingMatrix()
        with patch.object(lens, "validEmbedding", side_effect=AssertionError("matrix rebuilt")):
            self.assertEqual(lens.embeddingMatrix()[0], ids)
        photo = self.photo(self.flats[1], [1, 0])
        self.assertIn(photo.pk, lens.embeddingMatrix()[0])
        photo.embedding = self.vector([0, 1])
        photo.save(update_fields=["embedding"])
        ids, matrix = lens.embeddingMatrix()
        self.assertEqual(matrix[ids.index(photo.pk), 0], 0)
        ListingPhoto.objects.filter(pk=photo.pk).update(embedding=self.vector([1, 0]))
        ids, matrix = lens.embeddingMatrix()
        self.assertEqual(matrix[ids.index(photo.pk), 0], 1)
        pk = photo.pk
        photo.delete()
        self.assertNotIn(pk, lens.embeddingMatrix()[0])

    def test_equal_scores_keep_scalar(self):
        ListingPhoto.objects.filter(pk=self.photos[0].pk).update(embedding=self.vector([0, 1]))
        self.flats[0].photos.filter(room="kitchen").update(embedding=self.vector([1, 0]))
        ListingPhoto.objects.filter(pk=self.photos[1].pk).update(embedding=self.vector([1, 0]))
        matches = lens.similarFlats(self.vector([1, 0]))
        self.assertEqual([match["flat"].pk for match in matches[:2]], [self.flats[0].pk, self.flats[1].pk])
        self.assertEqual(matches[0]["photo"].room, "kitchen")

    def test_cached_respects_new_listing(self):
        lens.embeddingMatrix()
        self.flats[0].status = "review"
        self.flats[0].save(update_fields=["status"])
        self.assertNotIn(
            self.flats[0].pk, [match["flat"].pk for match in lens.similarFlats(self.vector([1, 0]))]
        )

    def test_get_search_throttles_similarity(self):
        request = RequestFactory().get("/lens", {"photo": self.photos[0].pk}, REMOTE_ADDR="192.0.2.44")
        request.user = AnonymousUser()
        request.session = {}
        with patch.object(lens, "similarFlats", return_value=[]) as search:
            for index in range(lens.SEARCHES_PER_MINUTE):
                context = {}
                lens.fromPic(request, context)
                self.assertTrue(context["searched"])
            context = {}
            lens.fromPic(request, context)
            self.assertEqual(context["error"], lens.BUSY)
            self.assertEqual(search.call_count, lens.SEARCHES_PER_MINUTE)

    def test_get_search_shares_hourly(self):
        with patch.object(lens, "similarFlats", return_value=[]) as search:
            for index in range(lens.SEARCHES_PER_HOUR + 1):
                request = RequestFactory().get(
                    "/lens", {"photo": self.photos[0].pk}, REMOTE_ADDR="192.0.2.45"
                )
                request.user = AnonymousUser()
                request.session = {}
                context = {}
                lens.fromPic(request, context)
            self.assertEqual(context["error"], lens.BUSY)
            self.assertEqual(search.call_count, lens.SEARCHES_PER_HOUR)


class PhotoVersionTests(SimpleTestCase):

    def test_model_changes_head_clip(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(photocheck, "ML", root / "clip"):
                missing = photocheck.modelVersion()
                (root / "photo_head.joblib").write_bytes(b"room-v1")
                room = photocheck.modelVersion()
                (root / "aigen_head.joblib").write_bytes(b"ai-v1")
                both = photocheck.modelVersion()
                (root / "photo_head.joblib").write_bytes(b"room-v2")
                changed = photocheck.modelVersion()
                self.assertEqual(changed, photocheck.modelVersion())
                with patch.object(photocheck, "CLIP_REVISION", "another-revision"):
                    revision = photocheck.modelVersion()
                with patch.object(photocheck, "CLIP_NAME", "another-checkpoint"):
                    checkpoint = photocheck.modelVersion()
        self.assertEqual(len({missing, room, both, changed, revision, checkpoint}), 6)
        self.assertEqual(len(changed), 64)

    def test_scoring_stamps_embeddings_in(self):
        torch = MagicMock()
        transformers = MagicMock()
        features = MagicMock()
        features.__getitem__.return_value.__getitem__.return_value.tolist.return_value = [1.0] + [0.0] * 511
        scores = MagicMock()
        scores.softmax.return_value.tolist.return_value = [[0.05] * 20] * 2
        features.__matmul__.return_value = scores
        features.__rmatmul__.return_value = scores
        with (
            patch.dict("sys.modules", {"torch": torch, "transformers": transformers}),
            patch("PIL.Image.open"),
            patch.object(photocheck, "unit", return_value=features),
            patch.object(photocheck, "textFeatures"),
            patch.object(photocheck, "tagFeatures"),
            patch.object(photocheck, "decide", return_value={"irrelevant": None, "aiGenerated": None}),
            patch.object(photocheck, "pickTags", return_value=["sofa"]),
            patch.object(photocheck, "isOn", return_value=True),
            patch.object(photocheck, "heads", return_value=(None, None)),
            patch.object(photocheck, "verifyModel", return_value="verified"),
        ):
            results = photocheck.scoreFiles(["photo.jpg", "second.jpg"])
            result = results[0]
        self.assertEqual(len(results), 2)
        transformers.CLIPModel.from_pretrained.return_value.eval.return_value.vision_model.assert_called_once()
        self.assertEqual(result["modelVersion"], photocheck.MODEL_VERSION)
        self.assertEqual(result["embedding"], [1.0] + [0.0] * 511)
        self.assertIsNone(result["irrelevant"])
        transformers.CLIPModel.from_pretrained.assert_called_once_with(photocheck.ML, local_files_only=True)


class DownloadRevisionTests(SimpleTestCase):

    def test_explicitly_pass_configured(self):
        for module in [download_photo_model, download_vision_models]:
            with self.subTest(command=module.__name__):
                tree = ast.parse(textwrap.dedent(inspect.getsource(module.Command.handle)))
                calls = [
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and (node.func.id == "snapshot_download")
                ]
                self.assertEqual(len(calls), 1)
                self.assertIn("revision", [keyword.arg for keyword in calls[0].keywords])
        self.assertEqual(download_photo_model.REVISION, photocheck.CLIP_REVISION)
        self.assertEqual(
            set(download_vision_models.REVISIONS),
            {repo for repo, directory in download_vision_models.MODELS},
        )
        self.assertTrue(all(download_vision_models.REVISIONS.values()))

    def test_photo_verifies_saved_model(self):
        for training in (Path("app/training"), Path("ml/training")):
            with self.subTest(training=training):
                output = io.StringIO()
                hub = MagicMock()
                transformers = MagicMock()
                with (
                    patch.dict(
                        "sys.modules",
                        {
                            "torch": MagicMock(),
                            "transformers": transformers,
                            "huggingface_hub": hub,
                        },
                    ),
                    patch.object(download_photo_model, "clip_ready", side_effect=[False, True]),
                    patch.object(
                        download_photo_model, "verifyModel", return_value="identity mismatch"
                    ) as verify,
                    patch.object(download_photo_model, "TRAINING", training),
                ):
                    call_command("download_photo_model", stdout=output)
                hub.snapshot_download.assert_called_once()
                transformers.CLIPModel.from_pretrained.return_value.save_pretrained.assert_called_once_with(
                    download_photo_model.ML
                )
                verify.assert_called_once_with(
                    download_photo_model.NAME,
                    download_photo_model.ML,
                    download_photo_model.REVISION,
                )
                self.assertIn("CLIP: identity mismatch", output.getvalue())
                self.assertIn(f"run python {training / 'write_manifest.py'}", output.getvalue())

    def test_already_installed_photo_model(self):
        output = io.StringIO()
        with (
            patch.object(download_photo_model, "clip_ready", return_value=True),
            patch.object(download_photo_model, "verifyModel", return_value="verified") as verify,
            patch.dict("sys.modules", {"huggingface_hub": MagicMock()}) as modules,
        ):
            call_command("download_photo_model", stdout=output)
            modules["huggingface_hub"].snapshot_download.assert_not_called()
        verify.assert_called_once()
        self.assertIn("CLIP: verified", output.getvalue())
        self.assertNotIn("write_manifest", output.getvalue())

    def test_vision_verifies_new_existing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            hub = MagicMock()
            for existing in (False, True):
                if existing:
                    for repo, directory in download_vision_models.MODELS:
                        destination = root / directory
                        destination.mkdir(parents=True)
                        for name in (
                            "model.safetensors",
                            "config.json",
                            "preprocessor_config.json",
                        ):
                            (destination / name).touch()
                output = io.StringIO()
                hub.reset_mock()
                with (
                    patch.dict("sys.modules", {"huggingface_hub": hub}),
                    patch.object(download_vision_models, "ML", root),
                    patch.object(
                        download_vision_models,
                        "verifyModel",
                        side_effect=["verified", "identity mismatch", "verified", "not installed"],
                    ) as verify,
                    patch.object(download_vision_models, "TRAINING", Path("ml/training")),
                ):
                    call_command("download_vision_models", stdout=output)
                self.assertEqual(hub.snapshot_download.call_count, 0 if existing else 4)
                self.assertEqual(verify.call_count, 4)
                for (repo, directory), call in zip(download_vision_models.MODELS, verify.call_args_list):
                    self.assertEqual(
                        call.args, (repo, root / directory, download_vision_models.REVISIONS[repo])
                    )
                self.assertEqual(output.getvalue().count(": verified"), 2)
                self.assertIn("identity mismatch", output.getvalue())
                self.assertIn("not installed", output.getvalue())
                self.assertIn("run python ml/training/write_manifest.py", output.getvalue())
                self.assertIn("run python manage.py download_vision_models", output.getvalue())
