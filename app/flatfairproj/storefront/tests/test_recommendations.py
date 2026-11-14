from datetime import timedelta
from unittest.mock import patch
from weakref import WeakSet

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .helpers import makeListing, makeStaff, makeUser
from sellerpanel.photocheck import EMBEDDING_SIZE, MODEL_VERSION
from storefront import recommend_flats as recs
from storefront.models import (
    Collection,
    FlatView,
    Listing,
    ListingPhoto,
    Offer,
    SavedListing,
    SavedSearch,
    ViewingBooking,
    ViewingSlot,
)
from storefront.presentation import budgetShelf, typeShelf


class FlatRecommendationTests(TestCase):

    def setUp(self):
        self.buyer = makeUser("buyer@test.com")
        self.collection = Collection.objects.create(user=self.buyer, name="Shortlist")
        self.first = self.flat(town="TAMPINES", askingPrice=600000)
        self.second = self.flat(town="TAMPINES", askingPrice=600000)
        self.candidate = self.flat(town="TAMPINES", askingPrice=600000)
        self.older = timezone.now() - timedelta(days=2)

    def flat(self, **fields):
        seller = makeUser(f"seller{Listing.objects.count()}@test.com", "seller")
        return makeListing(fields.pop("seller", seller), **fields)

    def view(self, flat, count=1, when=None):
        return FlatView.objects.create(
            buyer=self.buyer, listing=flat, count=count, lastViewedAt=when or self.older
        )

    def engage(self):
        self.view(self.first)
        self.view(self.second)

    def save(self, flat):
        return SavedListing.objects.create(collection=self.collection, listing=flat)

    def photo(self, flat, values, version=MODEL_VERSION, room="living_room"):
        vector = values + [0.0] * (EMBEDDING_SIZE - len(values))
        return ListingPhoto.objects.create(
            listing=flat, room=room, image="demo/photo.jpg", embedding=vector, modelVersion=version
        )

    def candidates(self, *flats):
        for flat in flats:
            flat.tastePhotos = list(flat.photos.all())
        return flats

    def test_cold_start_requires_two(self):
        self.assertEqual(recs.recommendedFlats(self.buyer), [])
        self.view(self.first, count=9)
        self.assertIsNone(recs.tasteProfile(self.buyer))
        SavedSearch.objects.create(buyer=self.buyer, queryParams={"town": "TAMPINES"})
        self.assertIsNotNone(recs.tasteProfile(self.buyer))
        self.assertIn(self.candidate, recs.recommendedFlats(self.buyer))
        FlatView.objects.all().delete()
        self.save(self.first)
        self.assertIsNone(recs.tasteProfile(self.buyer))

    def test_signal_weights_cap_decay(self):
        self.view(self.first, count=1)
        self.save(self.second)
        extra = Collection.objects.create(user=self.buyer, name="Other")
        SavedListing.objects.create(collection=extra, listing=self.second)
        Offer.objects.create(buyer=self.buyer, listing=self.candidate, amount=550000)
        booked = self.flat()
        slot = ViewingSlot.objects.create(listing=booked, startsAt=timezone.now() + timedelta(days=1))
        ViewingBooking.objects.create(buyer=self.buyer, slot=slot)
        weights = recs.tasteProfile(self.buyer)["weights"]
        self.assertEqual(weights[self.first.pk], 1)
        self.assertEqual(weights[self.second.pk], 3)
        self.assertEqual(weights[self.candidate.pk], 5)
        self.assertEqual(weights[booked.pk], 4)
        FlatView.objects.filter(listing=self.first).update(
            count=99, lastViewedAt=timezone.now() - timedelta(days=31)
        )
        self.assertEqual(recs.tasteProfile(self.buyer)["weights"][self.first.pk], 1.5)

    def test_saved_searches_soft_preferences(self):
        self.view(self.first)
        SavedSearch.objects.create(
            buyer=self.buyer,
            queryParams={
                "town": "BEDOK",
                "flatType": "3 ROOM",
                "maxPrice": "400000",
                "minSqm": "60",
                "minLease": "80",
            },
        )
        profile = recs.tasteProfile(self.buyer)
        self.assertEqual(profile["categories"]["town"]["BEDOK"], recs.SEARCH_WEIGHT)
        self.assertGreater(profile["means"]["lease"], self.first.remainingLease())
        self.assertIn(self.candidate, recs.recommendedFlats(self.buyer))

    def test_excludes_own_saved_offered(self):
        self.engage()
        self.save(self.first)
        Offer.objects.create(buyer=self.buyer, listing=self.second, amount=550000)
        booked = self.flat()
        slot = ViewingSlot.objects.create(listing=booked, startsAt=timezone.now() + timedelta(days=1))
        ViewingBooking.objects.create(buyer=self.buyer, slot=slot, status="cancelled")
        recent = self.flat()
        self.view(recent, when=timezone.now())
        self.flat(seller=self.buyer)
        for status in ("hidden", "review", "sold", "rejected"):
            self.flat(status=status)
        inactive = self.flat()
        inactive.seller.is_active = False
        inactive.seller.save(update_fields=["is_active"])
        self.assertEqual(recs.recommendedFlats(self.buyer), [self.candidate])

    def test_reason_town_type_price(self):
        self.engage()
        profile = recs.tasteProfile(self.buyer)
        twin = self.flat(town="TAMPINES", askingPrice=600000)
        rows = recs.rankFlats(profile, self.candidates(twin, self.candidate))
        self.assertEqual([row[0] for row in rows], [self.candidate, twin])
        self.assertEqual(rows[0][2], "Because you looked at 4-room flats in Tampines around $600k")

    def test_fair_range_boost_and_reason(self):
        self.engage()
        profile = recs.tasteProfile(self.buyer)
        outside = self.flat(town="TAMPINES", askingPrice=600000, bandLow=610000, bandHigh=650000)
        self.candidates(self.candidate, outside)
        self.assertAlmostEqual(
            recs.scoreFlat(self.candidate, profile)[0] - recs.scoreFlat(outside, profile)[0],
            recs.FAIR_WEIGHT,
        )
        with patch.multiple(
            recs,
            TOWN_WEIGHT=0,
            TYPE_WEIGHT=0,
            PRICE_WEIGHT=0,
            SIZE_WEIGHT=0,
            STOREY_WEIGHT=0,
            LEASE_WEIGHT=0,
        ):
            self.assertEqual(recs.scoreFlat(self.candidate, profile)[1], "Priced within our fair range")

    def test_photo_similarity_normalised(self):
        self.engage()
        self.save(self.first)
        self.photo(self.first, [2.0, 0.0])
        self.photo(self.second, [1.0, 0.0])
        self.photo(self.candidate, [5.0, 0.0])
        self.photo(self.candidate, [0.0, 1.0], version="old", room="kitchen")
        profile = recs.tasteProfile(self.buyer)
        self.assertAlmostEqual(float(profile["embedding"][0]), 1)
        self.candidate.town = "BEDOK"
        self.candidate.flatType = "3 ROOM"
        self.candidate.askingPrice = 1200000
        self.candidates(self.candidate)
        score, reason = recs.scoreFlat(self.candidate, profile)
        self.assertEqual(reason, "Rooms look like flats you saved")
        ListingPhoto.objects.update(embedding=[1.0])
        self.candidates(self.candidate)
        self.assertIsNotNone(recs.scoreFlat(self.candidate, profile)[0])

    def test_picks_update_on_save(self):
        self.engage()
        with patch.object(recs, "tasteProfile", wraps=recs.tasteProfile) as build:
            first = recs.recommendedFlats(self.buyer)
            again = recs.recommendedFlats(self.buyer)
            self.assertEqual([flat.pk for flat in first], [flat.pk for flat in again])
            self.assertEqual(build.call_count, 2)
            self.save(self.candidate)
            self.assertNotIn(self.candidate, recs.recommendedFlats(self.buyer))
            self.assertEqual(build.call_count, 3)
            self.assertTrue(first)

    def test_hidden_flats_stay_out(self):
        self.engage()
        self.assertIn(self.candidate, recs.recommendedFlats(self.buyer))
        Listing.objects.filter(pk=self.candidate.pk).update(status="hidden")
        self.assertNotIn(self.candidate, recs.recommendedFlats(self.buyer))

    def test_maximum_six_results(self):
        self.engage()
        for index in range(8):
            self.flat(town="TAMPINES")
        self.assertEqual(len(recs.recommendedFlats(self.buyer)), 6)

    def test_recommended_cards_show_another(self):
        self.engage()
        Offer.objects.create(
            listing=self.candidate,
            buyer=makeUser("otherbuyer@test.com"),
            amount=550000,
            status="accepted",
            respondedAt=timezone.now(),
        )
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("home"))
        recommended = next(
            (flat for flat in response.context["recommendedFlats"] if flat.pk == self.candidate.pk)
        )
        with self.assertNumQueries(0):
            self.assertTrue(recommended.underOffer)
        self.assertTrue(recommended.acceptedCount)
        self.assertContains(response, "Under offer")

    def test_ranking_keeps_top_results(self):
        retained = WeakSet()

        def candidates():
            for pk in range(50, 0, -1):
                flat = Listing(pk=pk)
                retained.add(flat)
                self.assertLessEqual(len(retained), recs.RESULTS + 2)
                yield flat

        with patch.object(recs, "scoreFlat", new=lambda flat, profile: (1, "Similar flat")):
            rows = recs.rankFlats({}, candidates())
        self.assertEqual([flat.pk for flat, score, reason in rows], list(range(1, recs.RESULTS + 1)))

    def test_ui_shows_row_missing(self):
        self.engage()
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("listings"))
        for check_text_1 in (
            "Recommended for you",
            "Why these?",
            "Because you looked at 4-room flats in Tampines around $600k",
        ):
            self.assertContains(response, check_text_1)
        self.assertContains(self.client.get(reverse("home")), "Recommended for you")
        for params in (
            {"town": "TAMPINES"},
            {"flatType": "4 ROOM"},
            {"q": "Tampines"},
            {"minLease": "50"},
        ):
            self.assertNotContains(self.client.get(reverse("listings"), params), "Recommended for you")

    def test_guests_staff_selling_mode(self):
        self.engage()
        self.assertNotContains(self.client.get(reverse("listings")), "Recommended for you")
        self.assertNotContains(self.client.get(reverse("home")), "Recommended for you")
        self.client.force_login(makeStaff("staff@test.com"))
        self.assertNotContains(self.client.get(reverse("listings")), "Recommended for you")
        self.buyer.profile.canSell = True
        self.buyer.profile.save()
        self.client.force_login(self.buyer)
        session = self.client.session
        session["marketplaceMode"] = "seller"
        session.save()
        self.assertNotContains(self.client.get(reverse("listings")), "Recommended for you")
        self.assertNotContains(self.client.get(reverse("home")), "Recommended for you")

    def test_records_buyer_view_but(self):
        self.client.force_login(self.buyer)
        url = reverse("listing_detail", args=[self.candidate.publicId])
        self.client.get(url)
        first = FlatView.objects.get(buyer=self.buyer, listing=self.candidate)
        self.client.get(url)
        last = FlatView.objects.get(buyer=self.buyer, listing=self.candidate)
        self.assertEqual(last.count, 2)
        self.assertEqual(first.firstViewedAt, last.firstViewedAt)
        self.assertGreaterEqual(last.lastViewedAt, first.lastViewedAt)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.viewCount, 1)

    def test_view_recording_excludes_guests(self):
        url = reverse("listing_detail", args=[self.candidate.publicId])
        self.client.get(url)
        for user in (makeStaff("staff@test.com"), self.candidate.seller):
            self.client.force_login(user)
            self.client.get(url)
        self.candidate.seller.profile.canBuy = True
        self.candidate.seller.profile.save()
        self.client.force_login(self.candidate.seller)
        self.client.get(url)
        self.assertFalse(FlatView.objects.exists())
        self.client.force_login(self.buyer)
        Listing.objects.filter(pk=self.candidate.pk).update(status="hidden")
        self.client.get(url)
        self.assertFalse(FlatView.objects.exists())


class ShelfTests(TestCase):

    def setUp(self):
        self.flats = [
            self.flat("asdf", town="BEDOK", flatType="3 ROOM", askingPrice=405000),
            self.flat("qwer", town="BEDOK", flatType="4 ROOM", askingPrice=500000),
            self.flat("zxcv", town="YISHUN", flatType="4 ROOM", askingPrice=700000),
            self.flat("hjkl", town="BEDOK", flatType="5 ROOM", askingPrice=905000),
        ]
        self.flat("uiop", town="TAMPINES", flatType="2 ROOM", askingPrice=300000, status="hidden")
        self.flat("vbnm", town="TAMPINES", flatType="EXECUTIVE", askingPrice=1200000, status="review")
        self.live = Listing.objects.public().filter(status="live")

    def flat(self, name, **fields):
        return makeListing(makeUser(name + "@test.com", "seller"), **fields)

    def found(self, query):
        response = self.client.get(reverse("listings") + "?" + query)
        return response.context["total"]

    def test_flat_type_counts_match(self):
        with self.assertNumQueries(10):
            shelf = typeShelf(self.live)
        self.assertEqual(
            [(row["flatType"], row["count"], row["lowest"]) for row in shelf],
            [("3 ROOM", 1, 405000), ("4 ROOM", 2, 500000), ("5 ROOM", 1, 905000)],
        )
        self.assertEqual([row["flat"].flatType for row in shelf], ["3 ROOM", "4 ROOM", "5 ROOM"])
        for row in shelf:
            self.assertEqual(self.found("flatType=" + row["flatType"].replace(" ", "+")), row["count"])

    def test_budget_counts_match_search(self):
        with self.assertNumQueries(1):
            shelf = budgetShelf(self.live)
        self.assertEqual(
            shelf,
            [
                {"label": "Under $500k", "count": 1, "query": "maxPrice=499999"},
                {"label": "$500k to $700k", "count": 1, "query": "minPrice=500000&maxPrice=699999"},
                {"label": "$700k to $1m", "count": 2, "query": "minPrice=700000&maxPrice=999999"},
            ],
        )
        for band in shelf:
            self.assertEqual(self.found(band["query"]), band["count"])
        self.assertEqual(sum((band["count"] for band in shelf)), self.live.count())

    def test_budget_edges_go_in(self):
        for index, price in enumerate((499999, 500000, 699999, 700000, 999999, 1000000, 1000001)):
            self.flat(f"edge{index}", askingPrice=price)
        shelf = budgetShelf(self.live)
        self.assertEqual([band["count"] for band in shelf], [2, 3, 4, 2])
        self.assertEqual(sum((band["count"] for band in shelf)), self.live.count())
        self.assertEqual(shelf[3], {"label": "$1m and above", "count": 2, "query": "minPrice=1000000"})
        for band in shelf:
            self.assertEqual(self.found(band["query"]), band["count"])

    def test_budget_counts_price_change(self):
        for prices in ((520000, 640000), (700000, 1000000), (1000000, 1830000), (250000, 260000)):
            Listing.objects.filter(pk=self.flats[0].pk).update(askingPrice=prices[0])
            Listing.objects.filter(pk=self.flats[3].pk).update(askingPrice=prices[1])
            Listing.objects.filter(pk__in=[self.flats[1].pk, self.flats[2].pk]).update(
                askingPrice=sum(prices) // 2
            )
            shelf = budgetShelf(self.live)
            self.assertTrue(shelf)
            for band in shelf:
                with self.subTest(prices=prices, band=band["label"]):
                    self.assertEqual(self.found(band["query"]), band["count"])

    def test_hidden_flats_leave_empty(self):
        self.live.update(status="hidden")
        self.assertEqual((typeShelf(self.live), budgetShelf(self.live)), ([], []))

    def test_home_shelf_links_and_counts(self):
        response = self.client.get(reverse("home"))
        browse = reverse("listings")
        self.assertContains(response, f'href="{browse}?flatType=4%20ROOM"')
        self.assertContains(response, f'href="{browse}?maxPrice=499999"')
        self.assertContains(response, f'href="{browse}?minPrice=500000&amp;maxPrice=699999"')
        self.assertContains(response, f'href="{browse}?town=BEDOK"')
        self.assertNotContains(response, "$1m and above")
        self.assertEqual(len(response.context["liveListings"]), 4)
        self.assertIsNone(response.context["promoFlat"])
        self.assertContains(response, 'class="card-grid" id="home-gallery"')
        self.assertContains(
            response,
            '<h1 id="hero-title">Sell your HDB flat yourself, <span class="hero-accent">can!</span></h1>',
            html=True,
        )
        self.assertNotContains(response, 'class="hero-flat"')
        self.assertContains(response, "$0 agent fee")
        self.assertContains(response, "4 flats in 2 towns")
        self.assertContains(
            response, '<span class="shelf-tile-line">2 flats, from $500,000</span>', html=True
        )
        self.assertContains(response, 'class="shelf-tile"', count=3)
        self.assertContains(response, f'class="fchip" href="{browse}?maxPrice=499999"', count=1)
        self.assertContains(response, '<h2 id="shelf-area-title">Find flats by area</h2>', html=True)
        self.assertContains(response, "Pick your town on the map.")
        self.assertContains(response, 'class="townmap-svg"', count=1)
        for gone in ("Browse by town", "All towns", "Popular towns"):
            self.assertNotContains(response, gone)
        self.assertNotContains(response, "sinming")

    def test_promo_flat_not_shown(self):
        ListingPhoto.objects.create(listing=self.flats[2], room="living_room", image="listings/asdf.jpg")
        response = self.client.get(reverse("home"))
        self.assertEqual(response.context["promoFlat"].pk, self.flats[2].pk)
        shown = [flat.pk for flat in response.context["liveListings"]]
        self.assertEqual(len(shown), 3)
        self.assertContains(response, 'class="btn btn-white shelf-all"', count=1)
        self.assertNotIn(self.flats[2].pk, shown)
        flatUrl = reverse("listing_detail", args=[self.flats[2].publicId])
        self.assertContains(response, f'class="hero-flat" href="{flatUrl}"', count=1)
        self.assertNotContains(response, f'class="card listing-card" href="{flatUrl}"')

    def test_logged_in_home_no(self):
        ListingPhoto.objects.create(listing=self.flats[2], room="living_room", image="listings/asdf.jpg")
        self.client.force_login(makeUser("qwerty@test.com"))
        response = self.client.get(reverse("home"))
        self.assertIsNone(response.context["promoFlat"])
        self.assertIn(self.flats[2].pk, [flat.pk for flat in response.context["liveListings"]])
        self.assertNotContains(response, 'class="home-hero"')
        self.assertContains(response, "<summary>Browse flats and prices</summary>", html=True)

    def test_photo_choice_exclusions(self):
        ListingPhoto.objects.create(listing=self.flats[1], room="living_room", image="listings/asdf.jpg")
        shelf = typeShelf(self.live)
        self.assertEqual(shelf[1]["flat"].pk, self.flats[1].pk)
        self.assertTrue(shelf[1]["flat"].coverPhoto())
        shelf = typeShelf(self.live, [self.flats[1].pk])
        self.assertEqual(shelf[1]["flat"].pk, self.flats[2].pk)
        shelf = typeShelf(self.live, [flat.pk for flat in self.flats])
        self.assertEqual(
            [row["flat"].pk for row in shelf],
            [self.flats[0].pk, self.flats[2].pk, self.flats[3].pk],
        )
