from base64 import b64decode
from datetime import timedelta
from html.parser import HTMLParser
import io
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlencode

from django.core.cache import caches
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from .helpers import CLEAN, jpeg, makeListing, makeOrder, makeProduct, makeUser
from storefront import lens
from storefront.models import Listing, ListingPhoto, Offer
from storefront.search import typeCounts, costCap, tidySearch


class MaxPriceInputParser(HTMLParser):

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") == "maxPrice":
            self.attrs = attrs


class FilterTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.a = makeListing(self.seller, town="BEDOK", askingPrice=400000, bedrooms=2, floorAreaSqm=70)
        self.b = makeListing(
            makeUser("second@test.com", "seller"),
            town="BEDOK",
            askingPrice=650000,
            hasBalcony=True,
            flatType="5 ROOM",
        )
        self.c = makeListing(
            makeUser("third@test.com", "seller"),
            town="YISHUN",
            askingPrice=500000,
            leaseCommenceYear=timezone.localdate().year - 10,
        )
        makeListing(self.seller, town="YISHUN", status="hidden")

    def found(self, **query):
        response = self.client.get(reverse("listings"), query)
        return {flat.pk for flat in response.context["listings"]}

    def test_flat_type_counts_apply(self):
        response = self.client.get(reverse("listings"), {"town": "BEDOK", "flatType": "5 ROOM"})
        totals = {choice["value"]: choice["total"] for choice in response.context["typeChoices"]}
        self.assertEqual(totals, {"2 ROOM": 0, "3 ROOM": 0, "4 ROOM": 1, "5 ROOM": 1, "EXECUTIVE": 0})
        self.assertEqual(response.context["typeTotal"], 2)
        self.assertEqual(response.context["total"], 1)
        self.assertContains(
            response, '<input type="radio" name="flatType" value="5 ROOM" checked>', html=True
        )
        self.assertContains(response, '<span class="filter-count">2</span>', html=True)
        with self.assertNumQueries(1):
            choices, total = typeCounts({"maxPrice": "500000"}, priceCap=650000)
        self.assertEqual(total, 2)
        self.assertEqual(choices[2], {"value": "4 ROOM", "total": 2})

    def test_more_open_one_active(self):
        closed = self.client.get(reverse("listings"), {"flatType": "4 ROOM"})
        self.assertContains(closed, '<details class="filter-group filter-more" id="more-filters">')
        for name in (
            "flatType",
            "minPrice",
            "maxPrice",
            "bedrooms",
            "bathrooms",
            "minSqm",
            "maxSqm",
            "minLease",
            "balcony",
            "sort",
        ):
            with self.subTest(field=name):
                self.assertContains(closed, f'name="{name}"')
        opened = self.client.get(reverse("listings"), {"balcony": "1"})
        self.assertContains(opened, '<details class="filter-group filter-more" id="more-filters" open>')
        self.assertContains(opened, '<span class="filter-badge">1</span>', html=True, count=2)

    def test_browse_shows_twelve_flats(self):
        for index in range(10):
            makeListing(makeUser(f"page{index}@test.com", "seller"))
        response = self.client.get(reverse("listings"))
        self.assertEqual(len(response.context["listings"]), 12)
        self.assertEqual(response.context["pageObj"].paginator.num_pages, 2)

    def test_browse_card_listing_back(self):
        query = {"town": "BEDOK", "sort": "price_asc", "page": "2"}
        with patch("storefront.views.LISTINGS_PER_PAGE", 1):
            response = self.client.get(reverse("listings"), query)
        returnUrl = reverse("listings") + "?" + urlencode(query)
        flat = response.context["listings"][0]
        detailUrl = reverse("listing_detail", args=[flat.publicId])
        self.assertContains(response, f'href="{detailUrl}?return={quote(returnUrl)}"')
        response = self.client.get(detailUrl, {"return": returnUrl})
        self.assertEqual(response.context["browseReturn"], returnUrl)
        self.assertContains(response, f'class="nav-back no-print" href="{returnUrl.replace('&', '&amp;')}"')
        self.assertContains(
            response, f'class="back" href="{returnUrl.replace('&', '&amp;')}#flat-{flat.publicId}"'
        )

    def test_listing_back_links_reject(self):
        detailUrl = reverse("listing_detail", args=[self.a.publicId])
        for returnUrl in (
            "https://evil.example/listings?town=BEDOK",
            "//evil.example/listings?town=BEDOK",
            "https://testserver/listings?town=BEDOK",
            "/account",
            "/listings/../account",
            "/listings-extra?town=BEDOK",
            "/listings#other",
            "/listings?town=BEDOK#other",
            "/\\evil.example/listings",
            "javascript:alert(1)",
        ):
            with self.subTest(returnUrl=returnUrl):
                response = self.client.get(detailUrl, {"return": returnUrl})
                self.assertEqual(response.context["browseReturn"], reverse("listings"))
                self.assertContains(response, 'class="nav-back no-print" href="/listings"')
                self.assertContains(response, f'class="back" href="/listings#flat-{self.a.publicId}"')

    def test_listing_back_links_default(self):
        response = self.client.get(reverse("listing_detail", args=[self.a.publicId]))
        self.assertEqual(response.context["browseReturn"], reverse("listings"))

    def test_public_listings_by_town(self):
        self.assertEqual(self.found(), {self.a.pk, self.b.pk, self.c.pk})
        self.assertEqual(self.found(town="BEDOK"), {self.a.pk, self.b.pk})
        self.assertEqual(self.found(flatType="5 ROOM"), {self.b.pk})

    def test_keyword_search_matches_block(self):
        flat = makeListing(
            makeUser("search@test.com", "seller"),
            town="ANG MO KIO",
            block="474",
            postalCode="560474",
        )
        for query in ["474", "560474", "ang mo kio", "Ang Mo Kio Ave 10", "474 Ang Mo Kio Ave 10"]:
            with self.subTest(query=query):
                self.assertEqual(self.found(q=query), {flat.pk})
        self.assertEqual(self.found(q="bedok"), {self.a.pk, self.b.pk})

    def test_keyword_search_combines(self):
        response = self.client.get(
            reverse("listings"),
            {"q": "bedok", "town": "BEDOK", "maxPrice": "700000", "sort": "price_desc"},
        )
        self.assertEqual([flat.pk for flat in response.context["listings"]], [self.b.pk, self.a.pk])
        self.assertEqual(self.found(q="bedok", maxPrice="500000"), {self.a.pk})
        self.assertEqual(self.found(q="bedok", town="YISHUN"), set())

    def test_keyword_search_excludes_non(self):
        for status in ["review", "hidden", "sold", "rejected"]:
            makeListing(
                makeUser(status + "@test.com", "seller"),
                block="474",
                postalCode="560474",
                status=status,
                statusBeforeSold="live",
            )
        inactiveSeller = makeUser("inactive@test.com", "seller")
        inactiveSeller.is_active = False
        inactiveSeller.save()
        makeListing(inactiveSeller, block="474", postalCode="560474")
        self.assertEqual(self.found(q="474"), set())

    def test_keyword_chip_removal_keeps(self):
        response = self.client.get(
            reverse("listings"), {"q": " bedok ", "town": "BEDOK", "sort": "price_asc", "page": "2"}
        )
        chips = response.context["filterChips"]
        self.assertEqual(chips[0], {"label": "bedok", "url": "?town=BEDOK&sort=price_asc"})
        self.assertEqual(parse_qs(chips[1]["url"][1:]), {"q": ["bedok"], "sort": ["price_asc"]})
        self.assertIn(("q", "bedok"), response.context["sortFields"])
        self.assertContains(response, 'type="hidden" name="q" value="bedok"')

    def test_keyword_search_keeps_query(self):
        with patch("storefront.views.LISTINGS_PER_PAGE", 1):
            response = self.client.get(reverse("listings"), {"q": "bedok", "sort": "price_asc"})
            self.assertTrue(response.context["pageObj"].has_next())
            self.assertEqual(
                parse_qs(response.context["pageQuery"]), {"town": ["BEDOK"], "sort": ["price_asc"]}
            )
            self.assertContains(response, 'href="?sort=price_asc&amp;town=BEDOK&amp;page=2"')
            response = self.client.get(reverse("listings"), {"q": "bedok", "sort": "price_asc", "page": "2"})
            self.assertEqual([flat.pk for flat in response.context["listings"]], [self.b.pk])

    def test_street_search_includes_matches(self):
        catalogue = {
            str(560000 + index): {"b": str(index), "s": "ANG MO KIO AVE 10", "t": "ANG MO KIO"}
            for index in range(12)
        }
        flat = makeListing(
            makeUser("street@test.com", "seller"),
            block="11",
            postalCode="560011",
            town="ANG MO KIO",
        )
        with patch("storefront.search.blocks", return_value=catalogue):
            self.assertEqual(self.found(q="Ang Mo Kio Ave 10"), {flat.pk})

    def test_keyword_search_trims_caps(self):
        response = self.client.get(reverse("listings"), {"q": "  " + "x" * 80 + "  "})
        self.assertEqual(response.context["searchQuery"], "x" * 60)
        self.assertEqual(parse_qs(response.context["pageQuery"]), {"q": ["x" * 60]})
        response = self.client.get(reverse("listings"), {"q": " \t "})
        self.assertEqual(response.context["filterChips"], [])
        self.assertEqual(response.context["pageQuery"], "")
        self.assertEqual(self.found(q=" \t "), {self.a.pk, self.b.pk, self.c.pk})

    def test_keyword_search_handles_odd(self):
        for query in ["%_", "' OR 1=1 --", "<script>alert(1)</script>", "未知地址", "\x00"]:
            with self.subTest(query=query):
                response = self.client.get(reverse("listings"), {"q": query})
                self.assertContains(response, "No flats match these filters.")
                self.assertContains(
                    response,
                    '<p>Check the spelling or <a href="/#shelf-area-title">pick a town on the home page map</a>.</p>',
                    html=True,
                )
                self.assertContains(
                    response,
                    f'<a class="btn btn-brand" href="{reverse('listings')}">Clear filters</a>',
                    html=True,
                )
                self.assertNotContains(response, "Pick a town")
                empty = response.content.decode().split('class="browse-empty"', 1)[1].split("</div>", 1)[0]
                self.assertEqual(empty.count('class="btn'), 1)
                self.assertContains(response, "Clear all", count=1)
                self.assertEqual(response.context["total"], 0)
        response = self.client.get(reverse("listings"), {"flatType": "2 ROOM"})
        self.assertContains(response, "Try another town, a different flat type or a wider budget.")
        self.assertContains(response, f'href="{reverse('listings')}">Clear filters</a>')
        self.assertNotContains(response, "Check the spelling")
        self.assertNotContains(response, "Pick a town")
        Listing.objects.update(status="hidden")
        response = self.client.get(reverse("listings"))
        self.assertContains(
            response,
            '<a class="btn btn-brand" href="/#shelf-area-title">Pick a town</a>',
            html=True,
        )
        self.assertNotContains(response, "Clear filters")
        response = self.client.get(reverse("listings"), {"q": "<script>"})
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>")

    def test_listings_by_price_area(self):
        self.assertEqual(self.found(maxPrice="500000"), {self.a.pk, self.c.pk})
        self.assertEqual(self.found(minSqm="80"), {self.b.pk, self.c.pk})
        self.assertEqual(self.found(maxSqm="80"), {self.a.pk})
        self.assertEqual(self.found(bedrooms="3"), {self.b.pk, self.c.pk})

    def test_price_cap_one_aggregate(self):
        self.b.askingPrice = 1010001
        self.b.save(update_fields=["askingPrice"])
        with self.assertNumQueries(1):
            self.assertEqual(costCap(), 1020000)
        self.b.askingPrice = 1020000
        self.b.save(update_fields=["askingPrice"])
        with self.assertNumQueries(1):
            self.assertEqual(costCap(), 1020000)

    def test_home_browse_price_inputs(self):
        self.b.askingPrice = 1010001
        self.b.save(update_fields=["askingPrice"])
        for status in ["review", "hidden", "sold", "rejected"]:
            makeListing(makeUser(status + "-cap@test.com", "seller"), status=status, askingPrice=2000000)
        inactive = makeUser("inactive-cap@test.com", "seller")
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])
        makeListing(inactive, askingPrice=2500000)
        for name in ["home", "listings"]:
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                parser = MaxPriceInputParser()
                parser.feed(response.content.decode())
                self.assertEqual(parser.attrs["max"], "1020000")
                self.assertEqual(parser.attrs["min"], "400000")
                self.assertEqual(parser.attrs["step"], "10000")
                self.assertEqual(parser.attrs["type"], "range")
                self.assertEqual(parser.attrs["value"], "1020000")

    def test_max_price_above_cap(self):
        for price in ["650000", "650001", "4000000"]:
            with self.subTest(price=price):
                response = self.client.get(reverse("listings"), {"maxPrice": price})
                self.assertEqual(response.context["maxPrice"], "")
                self.assertEqual(
                    {flat.pk for flat in response.context["listings"]},
                    {self.a.pk, self.b.pk, self.c.pk},
                )
                self.assertNotIn("maxPrice", response.context["searchParams"])
                self.assertNotIn("maxPrice", parse_qs(response.context["pageQuery"]))
                self.assertEqual(response.context["filterChips"], [])

    def test_empty_max_price_adds(self):
        response = self.client.get(reverse("listings"), {"maxPrice": "", "town": "BEDOK"})
        self.assertEqual(response.context["maxPrice"], "")
        self.assertEqual({flat.pk for flat in response.context["listings"]}, self.found(town="BEDOK"))
        self.assertEqual([chip["label"] for chip in response.context["filterChips"]], ["Bedok"])
        response = self.client.get(reverse("home"), {"maxPrice": ""}, follow=True)
        self.assertEqual({flat.pk for flat in response.context["listings"]}, self.found())

    def test_price_inputs_keep_no(self):
        self.seller.is_active = False
        self.seller.save(update_fields=["is_active"])
        for flat in [self.b, self.c]:
            flat.status = "hidden"
            flat.save(update_fields=["status"])
        with self.assertNumQueries(1):
            self.assertIsNone(costCap())
        for name in ["home", "listings"]:
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                parser = MaxPriceInputParser()
                parser.feed(response.content.decode())
                self.assertEqual(parser.attrs["max"], "3000000")
                self.assertEqual(parser.attrs["min"], "0")
                self.assertEqual(parser.attrs["value"], "3000000")
        response = self.client.get(reverse("listings"), {"maxPrice": "700000"})
        self.assertEqual(response.context["maxPrice"], 700000)

    def test_listings_by_balcony_remaining(self):
        self.assertEqual(self.found(balcony="1"), {self.b.pk})
        self.assertEqual(self.found(minLease="80"), {self.c.pk})

    def test_invalid_numeric_ignored(self):
        self.assertEqual(
            self.found(maxPrice="asdf", bedrooms="-1", minSqm="nan"),
            {self.a.pk, self.b.pk, self.c.pk},
        )

    def test_price_sort_unknown_fallback(self):
        response = self.client.get(reverse("listings"), {"sort": "price_asc"})
        self.assertEqual([f.pk for f in response.context["listings"]], [self.a.pk, self.c.pk, self.b.pk])
        response = self.client.get(reverse("listings"), {"sort": "asdf"})
        self.assertEqual(response.context["sort"], "recommended")

    def test_chips_keep_other_selected(self):
        response = self.client.get(reverse("listings"), {"town": "BEDOK", "maxPrice": "600000"})
        labels = [chip["label"] for chip in response.context["filterChips"]]
        self.assertEqual(labels, ["Bedok", "Up to $600,000"])
        self.assertEqual(response.context["filterChips"][0]["url"], "?maxPrice=600000")

    def test_region_exactly_towns(self):
        east = makeListing(makeUser("east@test.com", "seller"), town="TAMPINES")
        makeListing(makeUser("west@test.com", "seller"), town="CLEMENTI", status="hidden")
        self.assertEqual(self.found(region="East"), {self.a.pk, self.b.pk, east.pk})
        self.assertEqual(self.found(region="north"), {self.c.pk})
        self.assertEqual(self.found(region="West"), set())
        self.assertEqual(self.found(region="East", flatType="5 ROOM"), {self.b.pk})

    def test_town_beats_region_unknown(self):
        response = self.client.get(reverse("listings"), {"town": "YISHUN", "region": "East"})
        self.assertEqual({flat.pk for flat in response.context["listings"]}, {self.c.pk})
        self.assertEqual(response.context["region"], "")
        self.assertEqual([chip["label"] for chip in response.context["filterChips"]], ["Yishun"])
        self.assertEqual(response.context["pageQuery"], "town=YISHUN")
        self.assertEqual(tidySearch({"q": "bedok", "region": "North"}), {"town": "BEDOK"})
        self.assertEqual(tidySearch({"region": " NORTH-east "}), {"region": "North-East"})
        response = self.client.get(reverse("listings"), {"region": "Atlantis"})
        self.assertEqual(
            {flat.pk for flat in response.context["listings"]}, {self.a.pk, self.b.pk, self.c.pk}
        )
        self.assertEqual(response.context["filterChips"], [])
        self.assertEqual(response.context["pageQuery"], "")

    def test_region_chip_removal_drops(self):
        response = self.client.get(
            reverse("listings"),
            {"region": "east", "maxPrice": "600000", "sort": "price_asc", "page": "2"},
        )
        chips = {chip["label"]: chip["url"] for chip in response.context["filterChips"]}
        self.assertEqual(list(chips), ["East", "Up to $600,000"])
        self.assertEqual(parse_qs(chips["East"][1:]), {"maxPrice": ["600000"], "sort": ["price_asc"]})
        self.assertEqual(parse_qs(chips["Up to $600,000"][1:]), {"region": ["East"], "sort": ["price_asc"]})
        self.assertContains(response, 'aria-label="Remove East"')

    def test_region_kept_by_sort(self):
        with patch("storefront.views.LISTINGS_PER_PAGE", 1):
            response = self.client.get(reverse("listings"), {"region": "East", "sort": "price_asc"})
            self.assertEqual(
                parse_qs(response.context["pageQuery"]), {"region": ["East"], "sort": ["price_asc"]}
            )
            self.assertIn(("region", "East"), response.context["sortFields"])
            self.assertEqual(response.context["searchParams"], {"region": "East"})
            self.assertContains(response, '<input type="hidden" name="region" value="East">', html=True)
            self.assertContains(response, "page=2")
            response = self.client.get(
                reverse("listings"), {"region": "East", "sort": "price_asc", "page": "2"}
            )
            self.assertEqual([flat.pk for flat in response.context["listings"]], [self.b.pk])

    def test_location_group_names_place(self):
        response = self.client.get(reverse("listings"))
        self.assertNotContains(response, 'class="filter-group filter-where"')
        for gone in ("area-map", "Choose on map", "Choose an area on the map", "townmap"):
            self.assertNotContains(response, gone)
        response = self.client.get(reverse("listings"), {"region": "East", "map": "1", "sort": "newest"})
        self.assertNotContains(response, 'class="filter-group filter-where"')
        self.assertContains(response, '<input type="radio" name="region" value="East" checked>', html=True)
        self.assertNotContains(response, "townmap")
        self.assertEqual(parse_qs(response.context["pageQuery"]), {"region": ["East"], "sort": ["newest"]})
        self.assertNotIn(("map", "1"), response.context["sortFields"])
        response = self.client.get(reverse("listings"), {"town": "bedok", "flatType": "4 ROOM"})
        where = (
            response.content.decode().split('class="filter-group filter-where"', 1)[1].split("</div>", 1)[0]
        )
        self.assertIn("<span>Bedok</span>", where)
        self.assertIn('href="?flatType=4+ROOM"', where)
        self.assertNotContains(response, 'name="region"')

    def test_area_group_lists_five(self):
        response = self.client.get(reverse("listings"), {"flatType": "4 ROOM"})
        self.assertEqual(
            response.context["areaChoices"],
            [
                {"value": "North", "total": 1},
                {"value": "North-East", "total": 0},
                {"value": "East", "total": 1},
                {"value": "West", "total": 0},
                {"value": "Central", "total": 0},
            ],
        )
        self.assertEqual(response.context["areaTotal"], 2)
        form = response.content.decode().split('class="browse-filters"', 1)[1].split("</form>", 1)[0]
        self.assertLess(form.index("<legend>Area</legend>"), form.index("<legend>Flat type</legend>"))
        self.assertIn(
            '<input type="radio" name="region" value="" checked><span>Any area</span><span class="filter-count">2</span>',
            form,
        )
        self.assertEqual(form.count('name="region"'), 6)
        east = self.client.get(reverse("listings"), {"region": "East", "flatType": "4 ROOM"})
        self.assertEqual(east.context["areaTotal"], 2)
        self.assertEqual(self.found(region="East", flatType="4 ROOM"), {self.a.pk})
        self.assertEqual(self.client.get(reverse("listings"), {"town": "BEDOK"}).context["areaChoices"], [])

    def test_results_header_holds_heading(self):
        response = self.client.get(reverse("listings"))
        head = response.content.decode().split('<header class="result-head">', 1)[1].split("</header>", 1)[0]
        self.assertIn("<h1>Flats for sale</h1>", head)
        self.assertIn('<p class="result-count">3 flats</p>', head)
        self.assertEqual(head.count("<select"), 1)
        for gone in (
            "<a ",
            "<details",
            "applied-filters",
            "Search by photo",
            "Save this search",
            "tag-pill",
        ):
            self.assertNotIn(gone, head)
        self.assertEqual(head.count("<button"), 1)
        self.assertIn("<noscript><button", head)
        filtered = self.client.get(reverse("listings"), {"town": "BEDOK"})
        head = filtered.content.decode().split('<header class="result-head">', 1)[1].split("</header>", 1)[0]
        self.assertEqual(head.count("Clear all"), 1)
        self.assertContains(filtered, "Clear all", count=1)
        self.assertNotIn("Search by photo", head)

    def test_tag_meanings_sit_in(self):
        with patch("storefront.views.LISTINGS_PER_PAGE", 1):
            content = self.client.get(reverse("listings")).content.decode()
        self.assertEqual(content.count('<details class="fold tag-legend">'), 1)
        self.assertLess(
            content.index('class="browse-grid"'), content.index('<details class="fold tag-legend">')
        )
        self.assertLess(
            content.index('<details class="fold tag-legend">'),
            content.index('class="browse-pagination"'),
        )
        legend = content.split('<details class="fold tag-legend">', 1)[1].split("</details>", 1)[0]
        self.assertIn("<summary>What do the tags mean?</summary>", legend)
        self.assertIn("The asking price is close to what similar flats nearby sold for.", legend)
        self.assertIn("The seller paid to show this flat near the top.", legend)
        self.assertNotContains(self.client.get(reverse("listings"), {"q": "zzzz"}), "What do the tags mean?")

    def test_top_spot_package_ranks(self):
        product = makeProduct("Top spot", days=None, price=299)
        makeOrder(self.seller, product, listing=self.c)
        response = self.client.get(reverse("listings"))
        flats = list(response.context["listings"])
        self.assertEqual(flats[0].pk, self.c.pk)
        self.assertEqual(flats[0].adTag, "Sponsored")
        self.assertEqual(flats[1].adTag, "")

    def test_expired_package_not_show(self):
        product = makeProduct("Boost 30 days")
        order = makeOrder(self.seller, product, listing=self.a)
        order.createdAt = timezone.now() - timedelta(days=31)
        order.save()
        self.assertEqual(list(self.client.get(reverse("listings")).context["listings"])[0].adTag, "")

    def test_search_tier_determines(self):
        makeOrder(self.seller, makeProduct("Spring promo", days=None, tier=3), listing=self.b)
        makeOrder(self.seller, makeProduct("Top spot", days=None, tier=0), listing=self.c)
        flats = list(self.client.get(reverse("listings")).context["listings"])
        self.assertEqual((flats[0].pk, flats[0].tier), (self.b.pk, "Top spot"))
        self.assertEqual([flat.adTag for flat in flats if flat.pk == self.c.pk], [""])

    def test_longer_running_boost_takes(self):
        makeOrder(
            self.seller,
            makeProduct("Boost 90 days", price=149, days=90),
            "completed",
            listing=self.a,
        )
        makeOrder(self.seller, makeProduct("Boost 30 days"), "completed", listing=self.a)
        makeOrder(self.seller, makeProduct("Boost 30 days"), "completed", listing=self.b)
        flats = list(self.client.get(reverse("listings")).context["listings"])
        self.assertEqual((flats[0].pk, flats[0].tier), (self.a.pk, "Boost 90 days"))

    def test_invalid_categories_ignored_town(self):
        response = self.client.get(reverse("listings"), {"town": "ZZZ", "flatType": "9 ROOM"})
        self.assertEqual(
            {flat.pk for flat in response.context["listings"]}, {self.a.pk, self.b.pk, self.c.pk}
        )
        self.assertEqual(response.context["filterChips"], [])
        self.assertEqual(self.found(town="bedok"), {self.a.pk, self.b.pk})

    def test_agreed_flats_rank_available(self):
        top = makeProduct("Top spot", days=None, price=299)
        makeOrder(self.seller, top, listing=self.a)
        makeOrder(self.seller, top, listing=self.b)
        buyer = makeUser("zxcv@test.com", "buyer")
        Offer.objects.create(
            respondedAt=timezone.now(),
            listing=self.b,
            buyer=buyer,
            amount=640000,
            status="accepted",
        )
        Offer.objects.create(
            respondedAt=timezone.now(),
            listing=self.c,
            buyer=buyer,
            amount=490000,
            status="accepted",
        )
        flats = list(self.client.get(reverse("listings")).context["listings"])
        self.assertEqual([flat.pk for flat in flats], [self.a.pk, self.b.pk, self.c.pk])
        Offer.objects.filter(listing=self.b).delete()
        Offer.objects.create(
            respondedAt=timezone.now(),
            listing=self.a,
            buyer=buyer,
            amount=390000,
            status="accepted",
        )
        flats = list(self.client.get(reverse("listings")).context["listings"])
        self.assertEqual([flat.pk for flat in flats], [self.b.pk, self.a.pk, self.c.pk])
        gallery = list(self.client.get(reverse("home")).context["liveListings"])
        self.assertEqual([flat.pk for flat in gallery], [self.b.pk, self.a.pk, self.c.pk])


class LensTests(TestCase):

    def setUp(self):
        caches["resources"].clear()
        self.addCleanup(caches["resources"].clear)
        seller = makeUser("asdf@test.com", "seller")
        self.near = makeListing(seller, town="BEDOK")
        self.far = makeListing(makeUser("far@test.com", "seller"), town="TAMPINES")
        self.hidden = makeListing(makeUser("held@test.com", "seller"), status="review")
        self.addPhoto(self.near, "kitchen", [0.0, 1.0], ["kitchen cabinets"])
        self.photo = self.addPhoto(self.near, "living_room", [0.95, 0.31], ["sofa", "wood floor"])
        self.addPhoto(self.far, "living_room", [0.6, 0.8], ["tiled floor"])
        self.addPhoto(self.hidden, "living_room", [1.0, 0.0], ["sofa"])
        self.found = {
            **CLEAN,
            "tags": ["sofa"],
            "embedding": self.vector([1.0, 0.0]),
            "modelVersion": lens.MODEL_VERSION,
        }
        checker = patch("storefront.lens.checkPics", side_effect=lambda photos: [self.found])
        self.checker = checker.start()
        self.addCleanup(checker.stop)

    def addPhoto(self, flat, room, embedding, tags):
        return ListingPhoto.objects.create(
            listing=flat,
            room=room,
            image="a.jpg",
            embedding=self.vector(embedding),
            tags=tags,
            aiIrrelevant=0.01,
            modelVersion=lens.MODEL_VERSION,
        )

    def vector(self, values):
        return values + [0.0] * (lens.EMBEDDING_SIZE - len(values))

    def upload(self, photo=None):
        return self.client.post(reverse("lens"), {"photo": photo or jpeg()})

    def test_photo_search_ranks_public(self):
        response = self.upload()
        matches = response.context["matches"]
        self.assertEqual([match["flat"] for match in matches], [self.near, self.far])
        self.assertEqual(matches[0]["photo"], self.photo)
        self.assertEqual([match["closeness"] for match in matches], ["Very similar", "Not very similar"])
        self.assertEqual(response.context["tags"], ["sofa"])
        self.assertContains(response, "Seen in your photo")
        self.assertContains(response, '<li class="chip">sofa</li>', html=True)

    @patch("storefront.lens.similarFlats", return_value=[])
    def test_uploaded_search_shows_small(self, search):
        output = io.BytesIO()
        Image.effect_noise((1600, 1200), 100).convert("RGB").save(output, "PNG")
        photo = SimpleUploadedFile("room.png", output.getvalue(), content_type="image/png")
        with patch("django.core.files.storage.default_storage.save") as save:
            response = self.upload(photo)
        self.checker.assert_called_once()
        search.assert_called_once_with(self.found["embedding"])
        save.assert_not_called()
        thumbnail = response.context["submittedPhoto"]
        self.assertTrue(thumbnail.startswith("data:image/jpeg;base64,"))
        self.assertLess(len(thumbnail.encode("ascii")), 200 * 1024)
        image = Image.open(io.BytesIO(b64decode(thumbnail.split(",", 1)[1])))
        self.assertEqual(image.size, (320, 240))
        self.assertContains(response, f'src="{thumbnail}"')
        for check_text_1 in (
            'alt="Your submitted room photo"',
            'id="lens-status" role="status" aria-live="polite" aria-atomic="true"',
            "data-lens-search",
        ):
            self.assertContains(response, check_text_1)
        content = response.content.decode()
        self.assertLess(content.index('class="lens-query"'), content.index('class="lens-results"'))

    def test_existing_photo_search_shows(self):
        response = self.client.get(reverse("lens"), {"photo": self.photo.pk})
        self.assertContains(response, f'src="{self.photo.publicUrl}"')
        self.assertContains(response, 'alt="Room photo used for this search"')

    def test_weak_upload_listing_photo(self):
        self.near.photos.update(embedding=self.vector([0.0, 1.0]))
        response = self.upload()
        self.assertEqual(response.context["matches"], [])
        self.assertContains(response, "No similar rooms yet")
        self.far.photos.update(embedding=self.vector([1.0, 0.0]))
        response = self.client.get(reverse("lens"), {"photo": self.photo.pk})
        self.assertEqual(response.context["matches"], [])
        self.assertContains(response, "No similar rooms yet")

    def test_match_at_minimum_useful(self):
        self.near.photos.update(
            embedding=self.vector([lens.MIN_USEFUL_SIMILARITY, (1 - lens.MIN_USEFUL_SIMILARITY**2) ** 0.5])
        )
        matches = lens.similarFlats(self.vector([1.0]))
        self.assertEqual(matches[0]["flat"], self.near)
        self.assertEqual(lens.MIN_USEFUL_SIMILARITY, 0.75)

    def test_photo_search_blocks_unsupported(self):
        out = io.BytesIO()
        Image.new("RGB", (16, 16)).save(out, "GIF")
        gif = SimpleUploadedFile("room.gif", out.getvalue(), content_type="image/gif")
        self.assertContains(self.upload(gif), "This file is not a photo.")
        text = SimpleUploadedFile("room.jpg", b"asdf", content_type="image/jpeg")
        self.assertContains(self.upload(text), "This file is not a photo.")
        with patch.object(lens, "MAX_MB", 0):
            self.assertContains(self.upload(), "This photo is over 0 MB.")
        self.assertEqual(self.checker.call_count, 0)

    def test_photo_search_reports(self):
        found = self.found
        self.found = None
        response = self.upload()
        self.assertEqual(response.context["error"], lens.FAILED)
        self.assertNotIn("matches", response.context)
        self.found = {**found, "irrelevant": 0.99}
        self.assertContains(self.upload(), "This does not look like a room in a flat.")

    def test_photo_search_skips_invalid(self):
        self.far.photos.update(embedding=self.vector([0.9, 0.43589]))
        invalid = [
            None,
            [],
            [1.0, 0.0],
            [[1.0] * lens.EMBEDDING_SIZE],
            self.vector(["bad"]),
            self.vector([0.0]),
        ]
        for embedding in invalid:
            with self.subTest(embedding=embedding):
                ListingPhoto.objects.filter(pk=self.photo.pk).update(embedding=embedding)
                matches = lens.similarFlats(self.vector([1.0, 0.0]))
                self.assertEqual([match["flat"] for match in matches], [self.far, self.near])
                self.assertEqual(matches[1]["photo"].room, "kitchen")
        for value in [float("nan"), float("inf"), -float("inf")]:
            with self.subTest(value=value):
                self.photo.embedding = self.vector([value])
                self.assertIsNone(lens.validEmbedding(self.photo.embedding))
                matches = lens.similarFlats(self.vector([1.0, 0.0]))
                self.assertEqual([match["flat"] for match in matches], [self.far, self.near])

    def test_photo_search_returns_empty(self):
        ListingPhoto.objects.update(embedding=[1.0])
        response = self.upload()
        self.assertEqual(response.context["matches"], [])
        self.assertTrue(response.context["searched"])
        self.assertContains(response, "No similar rooms yet")

    def test_photo_search_score(self):
        self.found["irrelevant"] = None
        response = self.upload()
        self.assertContains(response, lens.FAILED)
        self.assertNotIn("matches", response.context)

    def test_photo_search_works_ai(self):
        self.found["aiGenerated"] = None
        ListingPhoto.objects.filter(pk=self.photo.pk).update(aiIrrelevant=None)
        response = self.upload()
        self.assertEqual([match["flat"] for match in response.context["matches"]], [self.near, self.far])

    def test_photo_search_embeddings(self):
        for embedding in [
            None,
            [],
            [1.0],
            self.vector([float("nan")]),
            self.vector([float("inf")]),
        ]:
            with self.subTest(embedding=embedding):
                self.found["embedding"] = embedding
                response = self.upload()
                self.assertContains(response, lens.FAILED)
                self.assertNotIn("matches", response.context)

    def test_photo_search_excludes_older(self):
        self.near.photos.update(modelVersion=None)
        self.far.photos.update(modelVersion="older")
        self.assertEqual(self.upload().context["matches"], [])

    def test_photo_search_queries(self):
        for version in [None, "older"]:
            with self.subTest(version=version):
                self.found["modelVersion"] = version
                self.assertContains(self.upload(), lens.FAILED)
                self.near.photos.update(modelVersion=version)
                response = self.client.get(reverse("lens"), {"photo": self.photo.pk})
                self.assertContains(response, lens.FAILED)
                self.assertNotIn("matches", response.context)

    def test_existing_photo_search_reports(self):
        for fields in [{"embedding": None}, {"embedding": [1.0]}, {"aiIrrelevant": None}]:
            with self.subTest(fields=fields):
                ListingPhoto.objects.filter(pk=self.photo.pk).update(
                    **{"embedding": self.vector([1.0]), "aiIrrelevant": 0.01, **fields}
                )
                response = self.client.get(reverse("lens"), {"photo": self.photo.pk})
                self.assertContains(response, lens.FAILED)
                self.assertNotIn("matches", response.context)

    def test_existing_photo_search_excludes(self):
        response = self.client.get(reverse("lens"), {"photo": self.photo.pk})
        self.assertEqual([match["flat"] for match in response.context["matches"]], [self.far])
        self.assertEqual(response.context["tags"], ["sofa", "wood floor"])
        self.assertEqual(self.checker.call_count, 0)
        hiddenPhoto = self.hidden.photos.get()
        for value in [hiddenPhoto.pk, "asdf"]:
            response = self.client.get(reverse("lens"), {"photo": value})
            self.assertIn("cannot search with that photo", response.context["error"])

    def test_photo_search_rate_limited(self):
        for number in range(lens.SEARCHES_PER_MINUTE):
            self.assertIn("matches", self.upload().context)
        response = self.upload()
        self.assertEqual(response.context["error"], lens.BUSY)
        self.assertEqual(self.checker.call_count, lens.SEARCHES_PER_MINUTE)

    def test_photo_tags_similar_search(self):
        response = self.client.get(reverse("listing_detail", args=[self.near.publicId]))
        self.assertContains(response, "Comes with the flat")
        self.assertContains(response, "<span>Wood flooring</span>", html=True)
        self.assertContains(response, "<span>Kitchen cabinets</span>", html=True)
        self.assertNotContains(response, '<span class="chip" title="Seen in photos">sofa</span>')
        self.assertContains(response, f"/lens?photo={self.photo.pk}")
        self.assertContains(self.client.get(reverse("listings")), reverse("lens"))
