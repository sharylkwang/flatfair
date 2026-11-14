from types import SimpleNamespace
from unittest.mock import patch
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from storefront.models import (
    ListingPhoto,
    Collection,
    Listing,
    Offer,
    SavedListing,
    ViewingBooking,
    ViewingSlot,
    Sale,
)
from storefront.templatetags.estate_tags import FIXED_FEATURES, fixedFeatures, typicalGap, townTone
from storefront.tests.helpers import makeListing, makeUser
from datetime import timedelta
from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.db.models import Exists, OuterRef
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from .helpers import makeListing, makeOrder, makeProduct, makeStaff, makeUser
from storefront.presentation import floorOverview, saleChart
from vision.models import DetectedItem, PhotoAnalysis
from urllib.parse import parse_qs, urlsplit


class FixedFeatureTests(SimpleTestCase):

    def test_attached_friendly_labels_icons(self):
        tags = [
            "sofa",
            "bed",
            "wardrobe",
            "dining table",
            "study desk",
            "TV console",
            "plants",
            "window view",
            "washing machine",
            "unknown",
            "wood floor",
            "kitchen cabinets",
            "air conditioner",
        ]
        self.assertEqual(
            fixedFeatures(tags),
            [
                {"label": "Wood flooring", "icon": "flooring"},
                {"label": "Kitchen cabinets", "icon": "cabinet"},
                {"label": "Air conditioning", "icon": "cooling"},
            ],
        )

    def test_cap_applied_filtering(self):
        tags = [
            "sofa",
            "plants",
            "built-in wardrobe",
            "built-in wardrobes",
            "built-in wardrobe",
            *FIXED_FEATURES,
        ]
        features = fixedFeatures(tags)
        self.assertEqual(len(features), 6)
        self.assertEqual(len({feature["label"] for feature in features}), 6)
        self.assertEqual(features[0]["label"], "Built-in wardrobes")

    def test_missing_movable_tags_produce(self):
        for tags in (None, [], ["sofa", "wardrobe", "window view"]):
            with self.subTest(tags=tags):
                self.assertEqual(fixedFeatures(tags), [])

    def test_allowed_icon(self):
        for label, icon in FIXED_FEATURES.values():
            with self.subTest(label=label):
                content = render_to_string("storefront/flat_icon.html", {"name": icon})
                self.assertIn("<path", content)
                self.assertIn('aria-hidden="true"', content)
                self.assertIn('focusable="false"', content)

    def test_typical_gap_same_rounding(self):
        for asking, estimate, expected in (
            (1018000, 1039000, "$21,000 below our estimate"),
            (560000, 550125, "$10,000 above our estimate"),
            (550000, 550125, "Matches our estimate"),
        ):
            with self.subTest(asking=asking, estimate=estimate):
                self.assertEqual(typicalGap(asking, estimate), expected)


class FlatDetailTests(TestCase):

    def setUp(self):
        self.flat = makeListing(makeUser("flat-detail@test.com", "seller"))
        self.url = reverse("listing_detail", args=[self.flat.publicId])

    def test_feature_pills_hide_movable(self):
        tags = ["sofa", "wardrobe", "window view", "wood floor", "ceiling fan", "kitchen cabinets"]
        photo = ListingPhoto.objects.create(
            listing=self.flat, room="living_room", image="living.jpg", tags=tags
        )
        response = self.client.get(self.url)
        content = response.content.decode()
        section = content.split('class="listing-features"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Comes with the flat", section)
        for label in ("Wood flooring", "Ceiling fan", "Kitchen cabinets"):
            self.assertIn(label, section)
        for tag in ("sofa", "wardrobe", "window view"):
            self.assertNotIn(tag, section)
        self.assertEqual(section.count('<li class="chip">'), 3)
        self.assertEqual(section.count('aria-hidden="true"'), 3)
        photo.refresh_from_db()
        self.assertEqual(photo.tags, tags)
        self.assertEqual(self.flat.photoTags(), tags)
        self.assertContains(response, f"/lens?photo={photo.pk}")

    def test_feature_row_capped_at(self):
        ListingPhoto.objects.create(
            listing=self.flat, room="living_room", image="living.jpg", tags=list(FIXED_FEATURES)
        )
        content = self.client.get(self.url).content.decode()
        section = content.split('class="listing-features"', 1)[1].split("</section>", 1)[0]
        self.assertEqual(section.count('<li class="chip">'), 6)

    def test_no_feature_row_photos(self):
        self.assertNotContains(self.client.get(self.url), "Comes with the flat")
        photo = ListingPhoto.objects.create(
            listing=self.flat, room="living_room", image="living.jpg", tags=[]
        )
        for tags in ([], ["sofa", "bed", "wardrobe", "window view", "plants"]):
            with self.subTest(tags=tags):
                photo.tags = tags
                photo.save(update_fields=["tags"])
                response = self.client.get(self.url)
                self.assertNotContains(response, 'class="listing-features"')
                self.assertNotContains(response, "Comes with the flat")
                self.assertContains(response, f"/lens?photo={photo.pk}")

    def test_icon_facts_keep_lease(self):
        response = self.client.get(self.url)
        content = response.content.decode()
        facts = content.split('<dl class="listing-facts">', 1)[1].split("</dl>", 1)[0]
        self.assertEqual(facts.count("<dt>"), 6)
        self.assertIn(f'aria-label="About {self.flat.remainingLease()} years left of a 99-year lease"', facts)
        self.assertIn(f'max="99" value="{self.flat.remainingLease()}"', facts)
        for value in (self.flat.remainingLease(), self.flat.storey, 92, 3, 2, "No"):
            self.assertIn(f"<strong>{value}</strong>", facts)
        self.assertContains(response, f"{self.flat.flatModel} · Lease from {self.flat.leaseCommenceYear}")
        self.assertContains(response, f"Postal code {self.flat.postalCode}")
        self.flat.hasBalcony = True
        self.flat.save(update_fields=["hasBalcony"])
        self.assertContains(self.client.get(self.url), "<strong>Yes</strong>", html=True)

    def test_full_seller_description(self):
        self.flat.description = "Bright rooms & open space.\n" + "Room for the whole family. " * 25
        self.flat.save(update_fields=["description"])
        response = self.client.get(self.url)
        self.assertContains(response, "storefront/listing_detail.js")
        self.assertContains(response, "data-listing-description")
        content = response.content.decode().split("data-listing-description", 1)[1].split("</details>", 1)[0]
        self.assertIn('<p class="listing-description-preview">', content)
        self.assertIn("<details hidden>", content)
        self.assertIn("Read more", content)
        self.assertIn("Bright rooms &amp; open space.<br>", content)
        self.assertIn("Room for the whole family. " * 25, content)


@patch(
    "storefront.templatetags.estate_tags.modelInfo",
    return_value={"available": True, "version": "v5.3", "month": "2026-07"},
)
@patch("storefront.templatetags.estate_tags.band_coverage", return_value=8)
class FlatPriceTests(SimpleTestCase):

    def test_verdict_bar_one_short(self, coverage, info):
        for asking, verdict, gap in (
            (450000, "This price is on the low side", "$100,000 below our estimate"),
            (550000, "This price is fair", "Matches our estimate"),
            (625000, "This price is on the high side", "$75,000 above our estimate"),
        ):
            with self.subTest(asking=asking):
                content = render_to_string(
                    "storefront/listing_worth.html",
                    {
                        "lo": 500000,
                        "est": 550000,
                        "hi": 600000,
                        "flat": SimpleNamespace(
                            askingPrice=asking, modelVersion="v5.3", town="BEDOK", flatType="4 ROOM"
                        ),
                    },
                )
                visible, disclosure = content.split('<details class="fold glance-fold glance-method">', 1)
                self.assertIn(verdict, visible.split("</h2>", 1)[0])
                self.assertIn(f'<p class="evidence-gap">{gap}</p>', visible)
                self.assertLessEqual(visible.count("<p "), 2)
                self.assertLess(
                    visible.index('class="band fair-band"'), visible.index('class="evidence-gap"')
                )
                self.assertIn(f"Asking price ${asking:,}.", visible)
                self.assertIn("Estimated price about $550,000.", visible)
                self.assertIn("Fair range $500,000–$600,000.", visible)
                self.assertIn("<dt>Low</dt><dd>$500,000</dd>", visible)
                self.assertIn("<dt>Estimate</dt><dd>$550,000</dd>", visible)
                self.assertIn("<dt>High</dt><dd>$600,000</dd>", visible)
                self.assertNotIn("Similar flats may sell", visible)
                for fact in (
                    "How did we get this price?",
                    "about 240,000 HDB resale flats sold since 2017",
                    "about 81 in every 100 sold within our range",
                    "not an official HDB valuation",
                    "using prices up to Jul 2026",
                ):
                    self.assertIn(fact, disclosure)


class FlatThumbTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("thumb-seller@test.com", "seller")
        cls.flat = makeListing(cls.seller, block="321", street="YISHUN RING ROAD")
        cls.other = makeListing(
            makeUser("thumb-other@test.com", "seller"), block="654", street="YISHUN AVENUE 2"
        )
        cls.buyer = makeUser("thumb-buyer@test.com")

    def test_photo_less_browse_shows(self):
        response = self.client.get(reverse("listings"))
        self.assertContains(response, "storefront/towns/yishun")
        self.assertContains(response, "HDB estate, not this flat", count=2)
        self.assertContains(response, "Blk 321")
        self.assertContains(response, "Blk 654")
        self.assertContains(response, "Estate photo", count=2)
        self.assertTemplateUsed(response, "storefront/ui/flat_thumb.html")
        detail = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        self.assertContains(detail, "Estate photo, not this flat")

    def test_shared_includes_render_estate(self):
        self.flat.roomPhotos = []
        for template in (
            "card.html",
            "mini_card.html",
            "saved_card.html",
            "ui/photo_row.html",
            "ui/task_card.html",
        ):
            with self.subTest(template=template):
                html = render_to_string(
                    "storefront/" + template, {"flat": self.flat, "user": AnonymousUser()}
                )
                self.assertIn("Estate photo", html)
                self.assertIn("storefront/towns/yishun", html)
                self.assertIn("not this flat", html)
        photo = ListingPhoto.objects.create(listing=self.flat, room="kitchen", image="own-room.jpg")
        self.flat.roomPhotos = [photo]
        html = render_to_string("storefront/ui/flat_thumb.html", {"flat": self.flat, "size": "small"})
        self.assertIn(photo.publicUrl, html)
        self.assertIn('alt="Kitchen in Blk 321, Yishun"', html)
        self.assertNotIn("Estate photo", html)
        self.assertEqual(townTone(" yishun "), townTone("YISHUN"))
        self.assertIn(townTone("BEDOK"), ("sky", "mint", "periwinkle", "lilac", "good-soft", "warn-soft"))
        self.assertGreater(
            len({townTone(name) for name in ("YISHUN", "BEDOK", "TAMPINES", "BISHAN", "HOUGANG", "PUNGGOL")}),
            1,
        )

    def test_buyer_seller_staff_lists(self):
        collection = Collection.objects.create(user=self.buyer, name="Shortlist")
        SavedListing.objects.create(collection=collection, listing=self.flat)
        Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=550000)
        slot = ViewingSlot.objects.create(listing=self.flat, startsAt=timezone.now() + timedelta(days=1))
        ViewingBooking.objects.create(slot=slot, buyer=self.buyer)
        makeOrder(self.seller, makeProduct("Thumb plan", "addon", 40, None), listing=self.flat)
        staff = makeStaff("thumb-admin@test.com")
        self.other.status = "review"
        self.other.save(update_fields=["status"])
        for user, route in (
            (self.buyer, "saved"),
            (self.buyer, "my_offers"),
            (self.seller, "my_listings"),
            (self.seller, "seller_viewings"),
            (staff, "admin_queue"),
            (staff, "admin_listings"),
        ):
            with self.subTest(route=route):
                self.client.force_login(user)
                response = self.client.get(reverse(route))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "storefront/ui/flat_thumb.html")
                self.assertContains(response, "Estate photo")
                self.assertContains(response, "storefront/towns/yishun")

    def test_photos_first_within_sponsored(self):
        ListingPhoto.objects.create(listing=self.flat, room="kitchen", image="own-room.jpg")
        response = self.client.get(reverse("listings"))
        self.assertEqual([flat.pk for flat in response.context["listings"]], [self.flat.pk, self.other.pk])
        top = makeProduct("Top spot", days=None)
        makeOrder(self.other.seller, top, listing=self.other)
        response = self.client.get(reverse("listings"))
        self.assertEqual([flat.pk for flat in response.context["listings"]], [self.other.pk, self.flat.pk])
        self.assertEqual(response.context["listings"][0].adTag, "Sponsored")
        makeOrder(self.seller, top, listing=self.flat)
        response = self.client.get(reverse("listings"))
        self.assertEqual([flat.pk for flat in response.context["listings"]], [self.flat.pk, self.other.pk])
        response = self.client.get(reverse("listings"), {"sort": "newest"})
        self.assertEqual([flat.pk for flat in response.context["listings"]], [self.other.pk, self.flat.pk])

    def test_browse_stays_query_bound(self):
        self.client.get(reverse("listings"))
        with CaptureQueriesContext(connection) as small:
            self.client.get(reverse("listings"))
        sellers = User.objects.bulk_create(
            [User(username=f"scale-thumb-{index}") for index in range(21998)], batch_size=1000
        )
        Listing.objects.bulk_create(
            [
                Listing(
                    seller=sellers[index],
                    block=str(index),
                    town="YISHUN",
                    flatType="4 ROOM",
                    floorAreaSqm=92,
                    bedrooms=3,
                    bathrooms=2,
                    hasBalcony=False,
                    askingPrice=550000,
                    status="live",
                )
                for index in range(21998)
            ],
            batch_size=1000,
        )
        ListingPhoto.objects.create(listing=self.flat, room="kitchen", image="own-room.jpg")
        with self.assertNumQueries(len(small)):
            response = self.client.get(reverse("listings"))
        self.assertLessEqual(len(small), 8)
        self.assertEqual(response.context["total"], 22000)
        self.assertEqual(len(response.context["listings"]), 12)
        self.assertEqual(response.context["listings"][0].pk, self.flat.pk)
        self.assertTrue(response.context["listings"][0].hasPhotos)
        plan = (
            Listing.objects.annotate(hasPhotos=Exists(ListingPhoto.objects.filter(listing_id=OuterRef("pk"))))
            .order_by("-hasPhotos", "-createdAt", "-pk")[:12]
            .explain()
        )
        self.assertIn("INDEX", plan)
        countSql = [query["sql"] for query in small if "COUNT(" in query["sql"]][0]
        self.assertNotIn("GROUP BY", countSql)


class ListingGlanceTests(TestCase):

    def setUp(self):
        Sale.objects.all().delete()
        self.seller = makeUser("glance@test.com", "seller")
        self.flat = makeListing(
            self.seller,
            town="SENGKANG",
            block="333",
            postalCode="540333",
            flatType="5 ROOM",
            floorAreaSqm=112,
            storey=8,
            askingPrice=820000,
        )
        self.url = reverse("listing_detail", args=[self.flat.publicId])

    def seedSales(self):
        for band, prices in (
            ("01 TO 03", [700000] * 5),
            ("07 TO 09", [770000, 780000, 790000, 800000, 810000]),
            ("13 TO 15", [804000] * 5),
        ):
            for number, price in enumerate(prices):
                Sale.objects.create(
                    month=timezone.localdate().replace(day=1),
                    town=self.flat.town,
                    flatType=self.flat.flatType,
                    block=str(200 + number),
                    street="ANCHORVALE ROAD",
                    storeyRange=band,
                    floorAreaSqm=self.flat.floorAreaSqm,
                    flatModel=self.flat.flatModel,
                    leaseCommenceYear=self.flat.leaseCommenceYear,
                    price=price,
                )

    def fold(self, html, name):
        marker = '<details class="fold glance-fold'
        start = html.index(f"{marker} glance-{name}")
        end = html.find(marker, start + 1)
        row = html[start : end if end != -1 else len(html)]
        self.assertNotIn(" open", row.split(">", 1)[0])
        return row.split("</summary>", 1)

    @patch("vision.templatetags.vision_tags.weights_ok", return_value=True)
    @patch("vision.templatetags.vision_tags.is_stale", return_value=False)
    @patch("storefront.presentation.nextStation", return_value={"name": "Cheng Lim LRT", "metres": 720})
    @patch(
        "storefront.address.nearest_mrt",
        return_value={
            "name": "Farmway LRT",
            "walking": True,
            "minutes": 6,
            "metres": 480,
            "exit": "",
            "badges": [{"code": "SW2", "colour": "#748477", "lineName": "Sengkang LRT", "text": "#FFFFFF"}],
        },
    )
    def test_populated_cards_show_visuals(self, nearest, nextNearest, stale, weights):
        self.seedSales()
        photo = ListingPhoto.objects.create(
            listing=self.flat, room="living_room", image="listings/living.jpg"
        )
        analysis = PhotoAnalysis.objects.create(photo=photo, status="done", timings={"size": [1000, 750]})
        for number in range(22):
            DetectedItem.objects.create(
                analysis=analysis,
                label=("sofa", "lamp", "chair", "table")[number % 4],
                score=0.9,
                box=[10, 10, 100, 100],
                polygon=[[10, 10], [100, 10], [100, 100]],
                crop=f"vision/item-{number}.jpg",
            )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        expected = {
            "sales": (
                ("Recent sales nearby", "Median $790k · 5 sales"),
                (
                    "glance-sale-chart",
                    "glance-asking-line",
                    'role="img"',
                    "Asking price $820,000",
                    "$770,000",
                    "$810,000",
                ),
            ),
            "floors": (
                ("Prices by floor", "High floors +$104k"),
                (
                    "glance-floor-bars",
                    'role="img"',
                    "Low",
                    "$700k",
                    "Mid",
                    "$790k",
                    "High",
                    "$804k",
                    'class="is-current"',
                    "This flat",
                ),
            ),
            "plan": (
                ("Floor plan", "5-room · 112 sqm"),
                ("glance-plan-thumbnail", "Typical floor plan", 'src="/listing/'),
            ),
            "location": (
                ("Location and MRT", "6 min walk · Farmway LRT"),
                (
                    "glance-walk-visual",
                    "6 <small>min walk",
                    "Farmway LRT",
                    "glance-line-dot",
                    "Sengkang LRT",
                    "Cheng Lim LRT",
                    "720 m in a straight line",
                ),
            ),
            "shop": (
                ("Shop the look", "22 items"),
                ('alt="Sofa"', 'alt="Lamp"', 'alt="Chair"', 'alt="Table"'),
            ),
        }
        for name, (hints, visuals) in expected.items():
            with self.subTest(row=name):
                self.assertContains(response, f'<details class="fold glance-fold glance-{name}', count=1)
                summary, body = self.fold(html, name)
                for text in hints:
                    self.assertIn(text, summary)
                self.assertIn('<svg class="ui-icon"', summary)
                self.assertLessEqual(
                    len(summary.split('class="fold-hint')[1].split(">", 1)[1].split("<", 1)[0].split()),
                    6,
                )
                for text in visuals:
                    self.assertIn(text, body)
        for text in (
            "Anchorvale Road",
            "Lease from",
            "$790,000",
            "High floors averaged $104,000",
            "5 sold",
            "data-lightbox",
            "It isn’t part of the sale.",
            "vision-content",
            "Singapore map",
            "Other flats in Sengkang",
        ):
            self.assertIn(text, html)

    @patch("storefront.templatetags.estate_tags.planUrl", return_value="")
    @patch("storefront.address.nearest_mrt", return_value=None)
    @patch("vision.templatetags.vision_tags.weights_ok", return_value=False)
    def test_empty_data_keeps_five(self, weights, nearest, plan):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        for name in ("sales", "floors", "plan", "location", "shop"):
            self.assertContains(response, f'<details class="fold glance-fold glance-{name}', count=1)
        for message in (
            "No recent sales like this to compare yet.",
            "Not enough recent sales to compare floors yet.",
            "We don’t have a floor plan preview for this flat.",
            "We couldn’t check these photos. Try again later.",
            "We don’t have station distances for this flat yet.",
        ):
            self.assertContains(response, message)
        self.assertNotContains(response, "glance-sale-chart")
        self.assertNotContains(response, "glance-floor-bars")

    @patch("vision.templatetags.vision_tags.weights_ok", return_value=True)
    @patch("vision.templatetags.vision_tags.is_stale", return_value=False)
    def test_photo_states_remain_visible(self, stale, weights):
        photo = ListingPhoto.objects.create(
            listing=self.flat, room="living_room", image="listings/living.jpg"
        )
        analysis = PhotoAnalysis.objects.create(photo=photo, status="pending")
        for status, hint, message in (
            ("pending", "Checking photos", "Checking the photos…"),
            ("done", "No items found", "We didn’t spot any furniture in these photos."),
            ("failed", "Couldn’t check photos", "We couldn’t check these photos. Try again later."),
        ):
            with self.subTest(status=status):
                PhotoAnalysis.objects.filter(pk=analysis.pk).update(status=status)
                response = self.client.get(self.url)
                summary, body = self.fold(response.content.decode(), "shop")
                self.assertIn(hint, summary)
                self.assertIn(message, body)

    def test_chart_handles_equal_prices(self):
        sales = [Sale(price=790000) for number in range(5)]
        for asking in (1, 790000, 2000000):
            with self.subTest(asking=asking):
                chart = saleChart(sales, asking)
                self.assertGreaterEqual(chart["askingY"], 30)
                self.assertLessEqual(chart["askingY"], 140)
                self.assertEqual(len(chart["points"]), 5)
                for point in chart["points"]:
                    self.assertGreaterEqual(point["y"], 30)
                    self.assertLessEqual(point["y"], 140)
        self.assertEqual(saleChart([], 790000), {})

    def test_floor_overview_weights_sales(self):
        floors = {
            "rows": [
                {"label": "1–3", "average": 500000, "sales": 5},
                {"label": "4–6", "average": 600000, "sales": 15},
                {"label": "13–15", "average": 700000, "sales": 5},
            ]
        }
        overview = floorOverview(floors, 8)
        self.assertEqual(overview["rows"][0]["average"], 575000)
        self.assertIsNone(overview["rows"][1]["average"])
        self.assertTrue(overview["rows"][1]["mine"])
        self.assertIn("Mid floors (7–12): not enough recent sales, this flat", overview["label"])


QUERY = {"town": "YISHUN", "flatType": "5 ROOM", "area": "150", "storey": "8", "lease": "2000"}


class SimilarFlatsTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("similar@test.com", "seller")

    def link(self):
        response = self.client.get(reverse("valuation"), QUERY)
        self.assertContains(response, 'href="' + response.context["similarUrl"].replace("&", "&amp;") + '"')
        parts = urlsplit(response.context["similarUrl"])
        self.assertEqual(parts.path, reverse("listings"))
        found = {name: values[0] for name, values in parse_qs(parts.query).items()}
        self.assertTrue(found.pop("est").isdigit())
        return found

    def flat(self, **details):
        return makeListing(self.seller, town="YISHUN", flatType="5 ROOM", **details)

    def test_answers_follow_into_browse(self):
        self.flat(floorAreaSqm=148, leaseCommenceYear=2001)
        leaseLeft = 99 - (timezone.localdate().year - 2000)
        expected = {"town": "YISHUN", "flatType": "5 ROOM", "minSqm": "140", "maxSqm": "160"}
        if leaseLeft >= 50:
            expected["minLease"] = str(leaseLeft // 10 * 10 if leaseLeft < 90 else 80)
        self.assertEqual(self.link(), expected)
        page = self.client.get(reverse("listings"), self.link())
        labels = [chip["label"] for chip in page.context["filterChips"]]
        self.assertEqual(labels[:4], ["Yishun", "5-room", "From 140 sqm", "Up to 160 sqm"])
        self.assertEqual(len(page.context["listings"]), 1)

    def test_filters_loosen_until_found(self):
        self.flat(floorAreaSqm=150, leaseCommenceYear=1985)
        self.assertEqual(
            self.link(), {"town": "YISHUN", "flatType": "5 ROOM", "minSqm": "140", "maxSqm": "160"}
        )
        self.seller.listings.all().delete()
        self.flat(floorAreaSqm=110, leaseCommenceYear=1985)
        self.assertEqual(self.link(), {"town": "YISHUN", "flatType": "5 ROOM"})

    def test_town_type_stay_nothing(self):
        self.assertEqual(self.link(), {"town": "YISHUN", "flatType": "5 ROOM"})
