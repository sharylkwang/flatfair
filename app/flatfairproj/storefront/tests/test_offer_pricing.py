from datetime import date, timedelta
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from storefront.models import ActivityNotice, Offer, Sale, Collection, PriceSuggestion, SavedListing
from storefront.offeradvice import advise, offerFloor, coachSlow, suggestion
from storefront.tests.helpers import makeListing, makeUser, makeStaff
from storefront.tests.test_valuation import QUERY
from storefront.valuation import pastCheck


class OfferAdviceTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("advice-seller@test.com", "seller")
        cls.buyer = makeUser("advice-buyer@test.com", "buyer")
        cls.flat = makeListing(
            cls.seller, askingPrice=580000, estimatedPrice=550000, bandLow=500000, bandHigh=600000
        )

    def offer(self, amount, **fields):
        return Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=amount, **fields)

    def test_at_asking_price_marked(self):
        advice = advise(self.offer(580000))
        self.assertEqual((advice["tone"], advice["action"], advice["counter"]), ("good", "accept", None))
        self.assertEqual(advice["headline"], "This offer meets your asking price.")

    def test_inside_fair_range_gets(self):
        advice = advise(self.offer(520000))
        self.assertEqual((advice["tone"], advice["action"], advice["counter"]), ("fair", "counter", 535000))
        self.assertIn("$30,000 (5%) below our estimate", advice["headline"])

    def test_low_gets_counter_at(self):
        advice = advise(self.offer(450000))
        self.assertEqual((advice["tone"], advice["action"], advice["counter"]), ("low", "counter", 550000))
        self.assertIn("below what similar flats sold for", advice["headline"])

    def test_counter_never_goes_above(self):
        self.flat.askingPrice = 540000
        self.flat.save(update_fields=["askingPrice"])
        self.assertEqual(advise(self.offer(450000))["counter"], 540000)

    def test_no_estimate_means_no_advice(self):
        self.flat.estimatedPrice = None
        self.flat.save(update_fields=["estimatedPrice"])
        self.assertIsNone(advise(self.offer(520000)))

    def test_seller_sees_sales_filled(self):
        Sale.objects.create(
            month=date(2026, 9, 1),
            town="YISHUN",
            flatType="4 ROOM",
            block="125",
            street="YISHUN RING RD",
            storeyRange="10 TO 12",
            floorAreaSqm=92,
            flatModel="Model A",
            leaseCommenceYear=1995,
            price=531000,
        )
        offer = self.offer(520000)
        self.client.force_login(self.seller)
        page = self.client.get(reverse("seller_viewings")).content.decode()
        box = page.split(f'id="offer-{offer.pk}"', 1)[1]
        self.assertIn("inside the fair range", box)
        self.assertIn('value="535000" required', box)
        self.assertIn("We’ve put $535,000 in the counter box below.", box)
        self.assertIn("Recent sales nearby", box)
        self.assertIn("$531,000", box)
        response = self.client.post(
            reverse("activity_action"), {"action": "counter", "id": offer.pk, "amount": "535000"}
        )
        self.assertEqual(response.status_code, 302)
        offer.refresh_from_db()
        self.assertEqual((offer.status, int(offer.counterAmount)), ("countered", 535000))


class OfferFloorTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("floor-seller@test.com", "seller")
        cls.buyer = makeUser("floor-buyer@test.com", "buyer")
        cls.flat = makeListing(cls.seller, estimatedPrice=550000, bandLow=500400, bandHigh=600000)

    def sendOffer(self, amount):
        self.client.force_login(self.buyer)
        return self.client.post(
            reverse("activity_action"),
            {"action": "offer", "id": self.flat.publicId, "amount": amount},
            follow=True,
        )

    def test_seller_turns_minimum_on(self):
        self.client.force_login(self.seller)
        page = self.client.get(reverse("seller_listing", args=[self.flat.publicId]))
        self.assertContains(page, "Offer minimum")
        self.assertContains(page, "we’ll decline offers below <b>$500,000</b>")
        response = self.client.post(
            reverse("offer_floor", args=[self.flat.publicId]), {"on": "1"}, follow=True
        )
        self.assertContains(response, "Offer minimum is on. We’ll decline offers below $500,000 for you.")
        self.flat.refresh_from_db()
        self.assertTrue(self.flat.autoDeclineLow)
        self.client.post(reverse("offer_floor", args=[self.flat.publicId]), {"on": "0"})
        self.flat.refresh_from_db()
        self.assertFalse(self.flat.autoDeclineLow)
        self.client.force_login(self.buyer)
        self.assertEqual(
            self.client.post(reverse("offer_floor", args=[self.flat.publicId]), {"on": "1"}).status_code,
            302,
        )
        self.flat.refresh_from_db()
        self.assertFalse(self.flat.autoDeclineLow)

    def test_low_declined_automatically_both(self):
        self.flat.autoDeclineLow = True
        self.flat.save(update_fields=["autoDeclineLow"])
        self.assertEqual(offerFloor(self.flat), 500000)
        response = self.sendOffer("480000")
        self.assertContains(
            response,
            "This seller considers offers from $500,000, so your offer of $480,000 was declined automatically.",
        )
        offer = Offer.objects.get()
        self.assertEqual(offer.status, "declined")
        self.assertIn("declined automatically", offer.reply)
        self.assertTrue(ActivityNotice.objects.filter(user=self.buyer, kind="offer_declined_auto").exists())
        self.assertTrue(ActivityNotice.objects.filter(user=self.seller, kind="offer_declined_auto").exists())
        self.assertFalse(ActivityNotice.objects.filter(kind="offer_new").exists())
        page = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        self.assertContains(page, "This seller considers offers from")
        self.assertContains(page, "Our estimate <b>$550,000</b>")

    def test_at_minimum_go_through(self):
        self.flat.autoDeclineLow = True
        self.flat.save(update_fields=["autoDeclineLow"])
        self.sendOffer("500000")
        self.assertEqual(Offer.objects.get().status, "pending")
        Offer.objects.all().delete()
        self.flat.autoDeclineLow = False
        self.flat.save(update_fields=["autoDeclineLow"])
        self.sendOffer("450000")
        self.assertEqual(Offer.objects.get().status, "pending")


class PriceCoachTests(TestCase):

    def setUp(self):
        self.seller = makeUser("coach-seller@test.com", "seller")
        self.seller.last_login = timezone.now()
        self.seller.save(update_fields=["last_login"])
        self.buyer = makeUser("coach-buyer@test.com", "buyer")
        self.flat = makeListing(
            self.seller, askingPrice=620000, estimatedPrice=561400, bandLow=520000, bandHigh=600000
        )
        self.flat.createdAt = timezone.now() - timedelta(days=20)
        self.flat.save(update_fields=["createdAt"])

    def test_quiet_overpriced_flat_gets(self):
        self.assertEqual(suggestion(self.flat)["price"], 561000)
        self.assertEqual(coachSlow(), 1)
        found = PriceSuggestion.objects.get()
        self.assertEqual((found.price, found.oldPrice, found.status), (561000, 620000, "open"))
        notice = ActivityNotice.objects.get(user=self.seller, kind="price_coach")
        self.assertIn("Our estimate suggests $561,000", notice.text)
        self.assertEqual(coachSlow(), 0)

    def test_interest_new_flats_fair(self):
        Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=550000)
        self.assertIsNone(suggestion(self.flat))
        Offer.objects.all().delete()
        for other in range(2):
            SavedListing.objects.create(
                collection=Collection.objects.create(
                    user=makeUser(f"saver{other}@test.com", "buyer"), name="List"
                ),
                listing=self.flat,
            )
        self.assertIsNone(suggestion(self.flat))
        SavedListing.objects.all().delete()
        self.flat.askingPrice = 561000
        self.assertIsNone(suggestion(self.flat))
        self.flat.askingPrice = 620000
        self.flat.createdAt = timezone.now() - timedelta(days=3)
        self.assertIsNone(suggestion(self.flat))

    def test_seller_lowers_in_one(self):
        coachSlow()
        self.client.force_login(self.seller)
        page = self.client.get(reverse("seller_listing", args=[self.flat.publicId]))
        self.assertContains(page, "Few buyers are showing interest")
        self.assertContains(page, "Lower my price to $561,000")
        self.client.post(
            reverse("update_price", args=[self.flat.publicId]),
            {"askingPrice": "561000", "back": "flat"},
        )
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.askingPrice, 561000)
        page = self.client.get(reverse("seller_listing", args=[self.flat.publicId]))
        self.assertNotContains(page, "Few buyers are showing interest")
        self.assertEqual(PriceSuggestion.objects.get().status, "accepted")

    def test_keeping_stops_suggestions_30(self):
        coachSlow()
        found = PriceSuggestion.objects.get()
        self.client.force_login(self.seller)
        response = self.client.post(reverse("keep_price", args=[found.pk]), follow=True)
        self.assertContains(response, "We’ll keep your price")
        self.assertEqual(PriceSuggestion.objects.get().status, "kept")
        self.assertEqual(coachSlow(), 0)
        self.assertEqual(coachSlow(timezone.now() + timedelta(days=31)), 1)

    def test_seller_keep(self):
        coachSlow()
        found = PriceSuggestion.objects.get()
        self.client.force_login(self.buyer)
        self.assertEqual(self.client.post(reverse("keep_price", args=[found.pk])).status_code, 404)


class PriceEvidenceTests(TestCase):

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.reportPath = Path(directory.name) / "price_backtest_eval.json"
        for target, value in (
            ("storefront.valuation.priceBacktestPath", self.reportPath),
            ("storefront.templatetags.estate_tags.modelInfo", {"available": False}),
            ("adminpanel.views.modelInfo", {"available": False}),
            ("storefront.views.band", (500000, 550000, 600000)),
        ):
            replacement = (
                patch(target, value) if isinstance(value, Path) else patch(target, return_value=value)
            )
            replacement.start()
            self.addCleanup(replacement.stop)
        self.report = {
            "pooled": {
                "byTown": {
                    "BUKIT TIMAH": {"count": 108, "servedCoverage": 0.6481481481481481},
                    "BEDOK": {"count": 300, "servedCoverage": 0.9},
                    "BISHAN": {"count": 12, "servedCoverage": 0.9},
                },
                "byFlatType": {
                    "4 ROOM": {"count": 500, "servedCoverage": 0.85},
                    "5 ROOM": {"count": 250, "servedCoverage": 0.75},
                },
                "weakestSubgroups": [
                    {
                        "dimension": "town",
                        "group": "BUKIT TIMAH",
                        "count": 108,
                        "servedCoverage": 0.6481481481481481,
                    },
                    {
                        "dimension": "flat_type",
                        "group": "5 ROOM",
                        "count": 250,
                        "servedCoverage": 0.75,
                    },
                ],
            }
        }
        self.saveReport()

    def saveReport(self):
        self.reportPath.write_text(json.dumps(self.report), encoding="utf-8")

    def priceCheck(self, town="BEDOK", flatType="4 ROOM"):
        return self.client.get(
            reverse("valuation"), {**QUERY, "town": town, "flatType": flatType, "area": "115"}
        )

    def flatPage(self, town="BUKIT TIMAH", flatType="4 ROOM"):
        flat = makeListing(makeUser("seller@test.com", "seller"), town=town, flatType=flatType)
        return self.client.get(reverse("listing_detail", args=[flat.publicId]))

    def test_weak_town_shows_coverage(self):
        note = (
            "In Bukit Timah, 65% of 108 past sales fell inside our range. Treat this range as a rough guide."
        )
        for response in (self.priceCheck("BUKIT TIMAH"), self.flatPage()):
            with self.subTest(path=response.request["PATH_INFO"]):
                self.assertContains(response, note, count=1)
                if response.request["PATH_INFO"] == reverse("valuation"):
                    self.assertContains(response, f'<p class="hint price-uncertainty">{note}</p>', html=True)
                    self.assertNotContains(response, "Similar flats may sell")
                else:
                    content = response.content.decode()
                    visible, disclosure = content.split('<details class="fold glance-fold glance-method">', 1)
                    self.assertIn(note, visible)
                    self.assertNotIn(note, disclosure.split("</details>", 1)[0])
                    self.assertLess(visible.index('class="fair-boundaries"'), visible.index(note))

    def test_good_town_type_show(self):
        for response in (self.priceCheck(), self.flatPage("BEDOK")):
            self.assertNotContains(response, "price-uncertainty")
            self.assertNotContains(response, "Treat this range as a rough guide.")

    def test_small_town_sample_shows(self):
        self.assertContains(
            self.priceCheck("BISHAN"),
            "We could only test this on 12 past sales in this town. Treat this range as a rough guide.",
            count=1,
        )

    def test_weak_flat_type_shown(self):
        self.assertContains(
            self.priceCheck(flatType="5 ROOM"),
            "For 5-room flats, 75% of 250 past sales fell inside our range. Treat this range as a rough guide.",
            count=1,
        )
        for town in ("BUKIT TIMAH", "BISHAN"):
            response = self.priceCheck(town, "5 ROOM")
            self.assertContains(response, "price-uncertainty", count=1)
            self.assertNotContains(response, "For 5-room flats")

    def test_thresholds_thirty_sales_eighty(self):
        self.report["pooled"]["byTown"]["BEDOK"] = {"count": 30, "servedCoverage": 0.8}
        self.saveReport()
        self.assertNotContains(self.priceCheck(), "price-uncertainty")
        self.report["pooled"]["byTown"]["BEDOK"]["count"] = 29
        self.saveReport()
        self.assertContains(self.priceCheck(), "We could only test this on 29 past sales in this town.")

    def test_missing_report_shows_nothing(self):
        self.reportPath.unlink()
        self.assertIsNone(pastCheck())
        for response in (self.priceCheck("BUKIT TIMAH"), self.flatPage()):
            self.assertNotContains(response, "price-uncertainty")
        self.client.force_login(makeStaff("staff@test.com"))
        self.assertNotContains(
            self.client.get(reverse("admin_price_model")),
            "Where the range missed the largest share of sales",
        )

    def test_malformed_reports_show_nothing(self):
        invalid = ["{", "[]", "null", "{}", '{"pooled": []}']
        for summary in (
            None,
            {"count": "108", "servedCoverage": 0.65},
            {"count": 108, "servedCoverage": True},
            {"count": 108, "servedCoverage": float("nan")},
            {"count": -1, "servedCoverage": 0.65},
            {"count": 108, "servedCoverage": 1.1},
        ):
            invalid.append(json.dumps({"pooled": {"byTown": {"BUKIT TIMAH": summary}, "byFlatType": {}}}))
        for content in invalid:
            with self.subTest(content=content):
                self.reportPath.write_text(content, encoding="utf-8")
                self.assertIsNone(pastCheck())
                self.assertNotContains(self.priceCheck("BUKIT TIMAH"), "price-uncertainty")
        self.reportPath.write_bytes(b"\xff")
        self.assertIsNone(pastCheck())

    def test_report_reads_latest(self):
        with patch("storefront.valuation.json.loads", wraps=json.loads) as parse:
            first = pastCheck()
            self.assertEqual(pastCheck(), first)
            self.assertEqual(parse.call_count, 2)
            modified = self.reportPath.stat().st_mtime_ns
            self.report["pooled"]["byTown"]["BUKIT TIMAH"]["count"] = 120
            self.saveReport()
            os.utime(self.reportPath, ns=(modified, modified))
            self.assertEqual(pastCheck()["byTown"]["BUKIT TIMAH"]["count"], 120)
            self.assertEqual(parse.call_count, 3)

    def test_staff_page_lists_weakest(self):
        self.client.force_login(makeStaff("staff@test.com"))
        response = self.client.get(reverse("admin_price_model"))
        self.assertContains(response, "Where the range missed the largest share of sales")
        self.assertContains(response, "Town · Bukit Timah: <b>64.8%</b> of 108 sales", html=True)
        self.assertContains(response, "Flat type · 5-room: <b>75.0%</b> of 250 sales", html=True)
