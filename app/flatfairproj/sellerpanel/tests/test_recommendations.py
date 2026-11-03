from datetime import timedelta
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import joblib
from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pretrained import FEATURE_NAMES, MODEL_VERSION, PACKAGE_TIERS
from sellerpanel.models import PackageRecommendation
from sellerpanel.recommend import makeTraits, loadPackageModel, bestPack, recommendationReason
from sellerpanel.tests.helpers import CARD
from storefront.models import Listing, Order
from storefront.tests.helpers import makeListing, makeOrder, makeProduct, makeStaff, makeUser


def modelArtifact(label="boost90"):
    return {
        "version": MODEL_VERSION,
        "features": FEATURE_NAMES,
        "model": Mock(predict=Mock(return_value=[label])),
        "townTransactions": {"YISHUN": 1800, "BEDOK": 1600},
        "competitionThreshold": 1064,
    }


class PackageFeatureTests(SimpleTestCase):

    def setUp(self):
        self.now = timezone.now()
        self.flat = Listing(
            town=" Yishun ",
            flatType="4 ROOM",
            floorAreaSqm=92,
            storey=8,
            leaseCommenceYear=1995,
            askingPrice=660000,
            estimatedPrice=550000,
            bandLow=500000,
            bandHigh=600000,
            createdAt=self.now - timedelta(days=45),
        )
        self.artifact = modelArtifact()

    def test_feature_values(self):
        features = makeTraits(self.flat, self.artifact, self.now)
        self.assertEqual(list(features), FEATURE_NAMES)
        self.assertEqual(features["town"], "YISHUN")
        self.assertEqual(features["flatType"], "4 ROOM")
        self.assertEqual(features["floorAreaSqm"], 92)
        self.assertEqual(features["storey"], 8)
        self.assertEqual(features["estimatedPrice"], 550000)
        self.assertAlmostEqual(features["askingRatio"], 1.2)
        self.assertAlmostEqual(features["bandPosition"], 1.6)
        self.assertEqual(features["daysListed"], 45)
        self.assertEqual(features["townTransactions"], 1800)
        today = timezone.localtime(self.now)
        self.assertAlmostEqual(features["leaseRemaining"], 99 - (today.year + (today.month - 1) / 12 - 1995))

    def test_future_listing_date_expired(self):
        self.flat.createdAt = self.now + timedelta(days=1)
        self.flat.leaseCommenceYear = 1900
        features = makeTraits(self.flat, self.artifact, self.now)
        self.assertEqual(features["daysListed"], 0)
        self.assertEqual(features["leaseRemaining"], 0)

    def test_missing_reversed_zero_nonfinite(self):
        for field, value in (
            ("estimatedPrice", None),
            ("estimatedPrice", 0),
            ("bandLow", 600000),
            ("bandHigh", 500000),
            ("floorAreaSqm", float("nan")),
        ):
            with self.subTest(field=field, value=value):
                previous = getattr(self.flat, field)
                setattr(self.flat, field, value)
                with self.assertRaises((ValueError, TypeError)):
                    makeTraits(self.flat, self.artifact, self.now)
                setattr(self.flat, field, previous)

    def test_unknown_town_not_silently(self):
        self.flat.town = "UNKNOWN"
        with self.assertRaises(ValueError):
            makeTraits(self.flat, self.artifact, self.now)

    def test_reason_price_resale_activity(self):
        reason = recommendationReason(makeTraits(self.flat, self.artifact, self.now), self.artifact)
        self.assertIn("asking price is above the fair range", reason)
        self.assertIn("Yishun has a lot of resale activity", reason)
        self.assertNotIn("flats for sale", reason)


class PackageModelLoadingTests(SimpleTestCase):

    def setUp(self):
        loadPackageModel.cache_clear()
        self.addCleanup(loadPackageModel.cache_clear)

    def test_becomes_available_installed(self):
        source = settings.BASE_DIR / "assets" / "ml" / "package_model.joblib"
        with TemporaryDirectory() as folder, override_settings(BASE_DIR=Path(folder)):
            target = Path(folder) / "assets" / "ml" / "package_model.joblib"
            target.parent.mkdir(parents=True)
            with patch("sellerpanel.recommend.log"):
                self.assertIsNone(loadPackageModel())
            copyfile(source, target)
            self.assertIsNotNone(loadPackageModel())

    def test_replacing_reloads_new_settings(self):
        source = settings.BASE_DIR / "assets" / "ml" / "package_model.joblib"
        with TemporaryDirectory() as folder, override_settings(BASE_DIR=Path(folder)):
            target = Path(folder) / "assets" / "ml" / "package_model.joblib"
            target.parent.mkdir(parents=True)
            copyfile(source, target)
            old = loadPackageModel()["competitionThreshold"]
            updated = joblib.load(source)
            updated["competitionThreshold"] = old * 2
            joblib.dump(updated, target)
            self.assertEqual(loadPackageModel()["competitionThreshold"], old * 2)

    def test_missing_unreadable_returns_none(self):
        for error in (FileNotFoundError("missing"), ValueError("corrupt"), RuntimeError("invalid")):
            with self.subTest(error=error):
                loadPackageModel.cache_clear()
                with patch("joblib.load", side_effect=error) as load:
                    self.assertIsNone(loadPackageModel())
                    self.assertIsNone(loadPackageModel())
                self.assertEqual(load.call_count, 1)

    def test_unknown_version_feature_schema(self):
        for artifact in (
            {"version": "old", "features": FEATURE_NAMES},
            {"version": MODEL_VERSION, "features": FEATURE_NAMES[::-1]},
            {"version": MODEL_VERSION, "features": FEATURE_NAMES, "model": None},
            None,
        ):
            with self.subTest(artifact=artifact):
                loadPackageModel.cache_clear()
                with patch("joblib.load", return_value=artifact):
                    self.assertIsNone(loadPackageModel())

    def test_mocked_joblib_loads_checks(self):
        model = Mock(classes_=list(PACKAGE_TIERS), predict=Mock(return_value=["boost90"]))
        encoded = list(FEATURE_NAMES)
        model.named_steps = {
            "vectorizer": Mock(
                get_feature_names_out=Mock(return_value=Mock(tolist=Mock(return_value=encoded)))
            ),
            "classifier": Mock(n_features_in_=len(encoded)),
        }
        candidate = {
            **modelArtifact(),
            "model": model,
            "encodedFeatures": encoded,
            "syntheticLabels": True,
        }
        with patch("joblib.load", return_value=candidate):
            artifact = loadPackageModel()
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact["version"], MODEL_VERSION)
        self.assertEqual(artifact["features"], FEATURE_NAMES)
        self.assertTrue(artifact["syntheticLabels"])
        flat = Listing(
            town="YISHUN",
            flatType="4 ROOM",
            floorAreaSqm=92,
            storey=8,
            leaseCommenceYear=1995,
            askingPrice=650000,
            estimatedPrice=550000,
            bandLow=500000,
            bandHigh=600000,
            createdAt=timezone.now(),
        )
        prediction = artifact["model"].predict([makeTraits(flat, artifact)])[0]
        self.assertIn(prediction, PACKAGE_TIERS)

    def test_invalid_encoded_schema_class(self):
        model = Mock(classes_=list(PACKAGE_TIERS))
        encoded = list(FEATURE_NAMES)
        model.named_steps = {
            "vectorizer": Mock(
                get_feature_names_out=Mock(return_value=Mock(tolist=Mock(return_value=encoded)))
            ),
            "classifier": Mock(n_features_in_=len(encoded)),
        }
        valid = {**modelArtifact(), "model": model, "encodedFeatures": encoded}
        for changes in (
            {"encodedFeatures": []},
            {"townTransactions": {}},
            {"townTransactions": {"YISHUN": -1}},
            {"competitionThreshold": float("nan")},
            {"model": Mock(classes_=["unknown"])},
        ):
            with self.subTest(changes=changes):
                loadPackageModel.cache_clear()
                with patch("joblib.load", return_value={**valid, **changes}):
                    self.assertIsNone(loadPackageModel())


@override_settings(STRIPE_SECRET_KEY="")
class PackageRecommendationTests(TestCase):

    def setUp(self):
        self.seller = makeUser("seller@test.com", "seller")
        self.flat = makeListing(self.seller)
        self.boost = makeProduct("Boost 90 days", price=149, days=90)
        self.shortBoost = makeProduct()
        self.artifact = modelArtifact()
        modelPatch = patch("sellerpanel.recommend.loadPackageModel", return_value=self.artifact)
        self.load = modelPatch.start()
        self.addCleanup(modelPatch.stop)
        self.client.force_login(self.seller)

    def impression(self, **fields):
        return PackageRecommendation.objects.create(
            listing=fields.pop("listing", self.flat),
            product=fields.pop("product", self.boost),
            reason="Yishun has high resale activity.",
            modelVersion=MODEL_VERSION,
            **fields,
        )

    def buy(self, product=None, **changes):
        product = product or self.boost
        self.client.post(reverse("cart_add", args=[product.pk]), {"listing": self.flat.publicId})
        token = self.client.get(reverse("cart")).context["checkoutToken"]
        data = {
            "checkoutToken": token,
            "listing": self.flat.publicId,
            "pay": "yes",
            **CARD,
            **changes,
        }
        return self.client.post(reverse("checkout"), data)

    def test_prediction_maps_active_tier(self):
        recommendation = bestPack(self.flat)
        self.assertEqual(recommendation["product"], self.boost)
        self.assertEqual(recommendation["modelVersion"], MODEL_VERSION)
        self.assertIn("Yishun has a lot of resale activity", recommendation["reason"])
        self.assertFalse(PackageRecommendation.objects.exists())

    def test_free_prediction_missing_product(self):
        self.artifact["model"].predict.return_value = ["none"]
        self.assertIsNone(bestPack(self.flat))
        self.artifact["model"].predict.return_value = ["topSpot"]
        self.assertIsNone(bestPack(self.flat))
        self.artifact["model"].predict.return_value = ["boost90"]
        self.boost.isActive = False
        self.boost.save()
        self.assertIsNone(bestPack(self.flat))

    def test_model_failure_missing_band(self):
        self.load.return_value = None
        self.assertIsNone(bestPack(self.flat))
        self.load.return_value = self.artifact
        self.artifact["model"].predict.side_effect = RuntimeError("failed inference")
        self.assertIsNone(bestPack(self.flat))
        self.artifact["model"].predict.side_effect = None
        self.flat.bandLow = None
        self.assertIsNone(bestPack(self.flat))

    def test_closed_listing_active_suppress(self):
        self.assertIsNone(bestPack(None))
        for status in ("sold", "hidden", "rejected"):
            self.flat.status = status
            self.assertIsNone(bestPack(self.flat))
        self.flat.status = "live"
        makeOrder(self.seller, self.shortBoost, "completed", listing=self.flat)
        self.assertIsNone(bestPack(self.flat))

    def test_expired_refunded_not_suppress(self):
        order = makeOrder(self.seller, self.shortBoost, "completed", listing=self.flat)
        Order.objects.filter(pk=order.pk).update(createdAt=timezone.now() - timedelta(days=31))
        self.assertIsNotNone(bestPack(self.flat))
        order.status = "refunded"
        order.save(update_fields=["status"])
        self.assertIsNotNone(bestPack(self.flat))

    def test_dashboard_records_one_across(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Recommended for your flat: Boost 90 days")
        self.assertContains(response, reverse("cart_add", args=[self.boost.pk]))
        self.assertContains(response, f'name="listing" value="{self.flat.publicId}"')
        impression = PackageRecommendation.objects.get()
        self.assertEqual(
            (impression.listing, impression.product, impression.modelVersion),
            (self.flat, self.boost, MODEL_VERSION),
        )
        self.assertFalse(impression.converted)
        self.client.get(reverse("dashboard"))
        self.client.get(reverse("cart"))
        self.client.get(reverse("packages"), {"listing": self.flat.publicId})
        self.assertEqual(PackageRecommendation.objects.count(), 1)

    def test_next_day_records_new(self):
        self.client.get(reverse("dashboard"))
        PackageRecommendation.objects.update(createdAt=timezone.now() - timedelta(days=1))
        self.client.get(reverse("dashboard"))
        self.assertEqual(PackageRecommendation.objects.count(), 2)

    def test_cart_highlights_recommended(self):
        self.client.post(reverse("cart_add", args=[self.boost.pk]))
        response = self.client.get(reverse("cart"))
        self.assertContains(
            response, '<span class="chip is-active">Recommended for your flat</span>', html=True
        )
        self.assertEqual(PackageRecommendation.objects.get().product, self.boost)

    def test_page_highlights_selected(self):
        response = self.client.get(reverse("packages"), {"listing": self.flat.publicId})
        self.assertContains(response, '<p class="package-ribbon">Recommended for your flat</p>', html=True)
        recommendation = PackageRecommendation.objects.get()
        self.assertEqual(recommendation.listing, self.flat)
        card = (
            response.content.decode().split(f'id="package-{self.boost.pk}"', 1)[1].split("</article>", 1)[0]
        )
        self.assertIn(response.context["recommendationReason"], card)
        self.assertContains(response, recommendation.reason)
        self.assertContains(response, "Why this plan?")
        self.assertIn("package-recommended", card)
        self.assertEqual(card.count("Add to cart"), 1)
        self.assertNotContains(response, 'aria-label="Package recommendation"')

    def test_anonymous_other_sellers_blocks(self):
        self.client.logout()
        response = self.client.get(reverse("packages"), {"listing": self.flat.publicId})
        self.assertNotContains(response, "Recommended for your flat")
        self.client.force_login(makeUser("other@test.com", "seller"))
        response = self.client.get(reverse("packages"), {"listing": self.flat.publicId})
        self.assertNotContains(response, "Recommended for your flat")
        self.assertFalse(PackageRecommendation.objects.exists())

    def test_no_model_free_prediction(self):
        for model in (None, modelArtifact("none")):
            self.load.return_value = model
            for page in ("dashboard", "cart", "packages"):
                response = self.client.get(reverse(page))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "Recommended for your flat")
        self.assertFalse(PackageRecommendation.objects.exists())

    def test_dashboard_active_not_record(self):
        makeOrder(self.seller, self.shortBoost, "completed", listing=self.flat)
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "Recommended for your flat")
        self.assertFalse(PackageRecommendation.objects.exists())

    def test_purchase_converts_latest(self):
        earlier = self.impression()
        PackageRecommendation.objects.filter(pk=earlier.pk).update(
            createdAt=timezone.now() - timedelta(days=1)
        )
        response = self.buy()
        self.assertEqual(response.status_code, 302)
        converted = PackageRecommendation.objects.get(converted=True)
        self.assertEqual(converted.orderItem, Order.objects.get().items.get())
        self.assertEqual(converted.product, self.boost)
        earlier.refresh_from_db()
        self.assertFalse(earlier.converted)
        order = Order.objects.get()
        self.client.post(reverse("checkout"), {"checkoutToken": order.checkoutToken, "pay": "yes", **CARD})
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(PackageRecommendation.objects.filter(converted=True).count(), 1)

    def test_buying_different_not_convert(self):
        self.impression()
        self.buy(self.shortBoost)
        self.assertEqual(Order.objects.count(), 1)
        self.assertFalse(PackageRecommendation.objects.filter(converted=True).exists())

    def test_failed_payment_not_convert(self):
        self.impression()
        self.buy(number="4242 4242 4242 4241")
        self.assertFalse(Order.objects.exists())
        self.assertFalse(PackageRecommendation.objects.filter(converted=True).exists())

    def test_missing_model_not_block(self):
        self.load.return_value = None
        self.buy()
        self.assertEqual(Order.objects.count(), 1)
        self.assertFalse(PackageRecommendation.objects.exists())

    def test_stale_future_other_listing(self):
        stale = self.impression()
        future = self.impression()
        otherFlat = makeListing(makeUser("another@test.com", "seller"))
        self.impression(listing=otherFlat)
        PackageRecommendation.objects.filter(pk=stale.pk).update(
            createdAt=timezone.now() - timedelta(days=31)
        )
        PackageRecommendation.objects.filter(pk=future.pk).update(
            createdAt=timezone.now() + timedelta(days=1)
        )
        self.load.return_value = None
        self.buy()
        self.assertEqual(Order.objects.count(), 1)
        self.assertFalse(PackageRecommendation.objects.filter(converted=True).exists())


class PackageRecommendationStaffTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("seller@test.com", "seller")
        cls.flat = makeListing(cls.seller)
        cls.product = makeProduct()

    def test_admin_overview_counts_distinct(self):
        order = makeOrder(self.seller, self.product, listing=self.flat)
        for converted in (False, False, True):
            PackageRecommendation.objects.create(
                listing=self.flat,
                product=self.product,
                reason="Visibility may help.",
                modelVersion=MODEL_VERSION,
                converted=converted,
                orderItem=order.items.get() if converted else None,
            )
        self.flat.status = "sold"
        self.flat.save()
        secondFlat = makeListing(self.seller)
        otherFlat = makeListing(makeUser("other-seller@test.com", "seller"))
        for flat, age in ((secondFlat, 1), (otherFlat, 1), (otherFlat, 31), (otherFlat, -1)):
            exposure = PackageRecommendation.objects.create(
                listing=flat,
                product=self.product,
                reason="Visibility may help.",
                modelVersion=MODEL_VERSION,
            )
            PackageRecommendation.objects.filter(pk=exposure.pk).update(
                createdAt=timezone.now() - timedelta(days=age)
            )
        self.client.force_login(makeStaff("admin@test.com"))
        response = self.client.get(reverse("admin_overview"))
        self.assertEqual(
            response.context["packageRecommendationStats"],
            {"shown": 3, "sellers": 2, "converted": 1, "conversionRate": 33.3},
        )
        self.assertContains(
            response,
            "3 listings shown a suggestion · 2 sellers reached · 1 purchase after a suggestion · 33.3% bought",
        )
        self.assertContains(
            response, "made-up listing examples and package rules, rather than actual sales results"
        )

    def test_empty_stats_zero_conversion(self):
        self.client.force_login(makeStaff("admin@test.com"))
        response = self.client.get(reverse("admin_overview"))
        self.assertEqual(
            response.context["packageRecommendationStats"],
            {"shown": 0, "sellers": 0, "converted": 0, "conversionRate": 0},
        )

    def test_old_future_purchases_excluded(self):
        for age in (31, -1):
            order = makeOrder(self.seller, self.product, listing=self.flat)
            exposure = PackageRecommendation.objects.create(
                listing=self.flat,
                product=self.product,
                reason="Visibility may help.",
                modelVersion=MODEL_VERSION,
                converted=True,
                orderItem=order.items.get(),
            )
            PackageRecommendation.objects.filter(pk=exposure.pk).update(
                createdAt=timezone.now() - timedelta(days=age)
            )
        self.client.force_login(makeStaff("admin@test.com"))
        self.assertEqual(
            self.client.get(reverse("admin_overview")).context["packageRecommendationStats"]["converted"],
            0,
        )

    def test_seller_anonymous_visitors(self):
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get(reverse("admin_overview")).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("admin_overview")).status_code, 302)
