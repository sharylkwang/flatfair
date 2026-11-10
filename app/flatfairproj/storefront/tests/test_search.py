from django.test import TestCase
from django.urls import reverse
from storefront.tests.helpers import makeListing, makeUser
from unittest.mock import patch
import re
from urllib.parse import parse_qs
from storefront.search import tidySearch, findHomes, hintTown, costRange
from .helpers import makeListing, makeUser
from html.parser import HTMLParser
from storefront.models import SavedSearch, SearchAlert
from django.template import Context, Template
from storefront.templatetags.shop_tags import town_map
from storefront.interiors import (
    NEARBY_AREAS,
    REGIONS,
    TOWN_AREAS,
    areaPaths,
    centroids,
    regionName,
    mapBounds,
    townPoint,
)
from storefront.valuation import TOWNS


class BrowsePlaceTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.flat = makeListing(makeUser("place-seller@test.com", "seller"))

    def test_flat_type_links_keep(self):
        page = self.client.get(reverse("listings"), {"town": "YISHUN"}).content.decode()
        self.assertIn("?flatType=5%20ROOM&amp;town=YISHUN", page)
        home = self.client.get(reverse("home")).content.decode()
        self.assertNotIn("&amp;town=", home.split("nav-type", 2)[1][:200])

    def test_back_flat_returns_card(self):
        results = self.client.get(reverse("listings"), {"town": "YISHUN"})
        self.assertContains(results, f'id="flat-{self.flat.publicId}"')
        detail = self.client.get(
            reverse("listing_detail", args=[self.flat.publicId]),
            {"return": "/listings?town=YISHUN"},
        )
        self.assertContains(detail, f'href="/listings?town=YISHUN#flat-{self.flat.publicId}"')


QUERY = {"town": "YISHUN", "flatType": "4 ROOM", "area": "92", "storey": "8", "lease": "1990"}


class EstimateBrowseTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        seller = makeUser("est-seller@test.com", "seller")
        cls.flat = makeListing(seller, askingPrice=600000)

    @patch("storefront.views.band", return_value=(520000, 577400, 640000))
    def test_see_similar_flats_carries(self, band):
        url = self.client.get(reverse("valuation"), QUERY).context["similarUrl"]
        self.assertIn("est=577000", url)
        page = self.client.get(url)
        self.assertContains(page, "Your estimated price: <b>$577,000</b>")
        self.assertContains(page, "$23k above your estimate")

    def test_missing_nothing_extra_shows(self):
        page = self.client.get(reverse("listings"), {"town": "YISHUN", "est": "abc"})
        self.assertNotContains(page, "Your estimated price")
        self.assertNotContains(page, "your estimate")


class SearchSuggestionTests(TestCase):

    def filterForm(self, response):
        return response.content.decode().split('class="browse-filters"', 1)[1].split("</form>", 1)[0]

    def test_new_town_replaces_previous(self):
        bedok = makeListing(makeUser("next-town@test.com", "seller"), town="BEDOK", askingPrice=500000)
        makeListing(makeUser("previous-town@test.com", "seller"), town="TAMPINES", askingPrice=500000)
        makeListing(makeUser("next-town-high@test.com", "seller"), town="BEDOK", askingPrice=900000)
        makeListing(makeUser("next-town-low@test.com", "seller"), town="BEDOK", askingPrice=400000)
        previous = self.client.get(reverse("listings"), {"q": "tampines"})
        self.assertContains(previous, 'name="q" value="Tampines"')
        self.assertNotIn('type="hidden" name="town"', self.filterForm(previous))
        response = self.client.get(
            reverse("listings"),
            {"q": "bedok", "flatType": "4 ROOM", "minPrice": "450000", "maxPrice": "550000"},
        )
        self.assertEqual({flat.pk for flat in response.context["listings"]}, {bedok.pk})
        self.assertEqual(response.context["town"], "BEDOK")
        self.assertEqual(response.context["searchQuery"], "")
        self.assertEqual(response.context["searchParams"]["flatType"], "4 ROOM")
        self.assertEqual(response.context["searchParams"]["minPrice"], "450000")
        self.assertEqual(response.context["searchParams"]["maxPrice"], "550000")

    def results(self, query):
        response = self.client.get(reverse("search_suggestions"), {"q": query})
        self.assertEqual(response.status_code, 200)
        return response.json()["results"]

    def test_postal_prefix_exact_show(self):
        results = self.results("51056")
        self.assertLessEqual(len(results), 8)
        self.assertTrue(all((item["kind"] == "postal" for item in results)))
        self.assertIn(
            {"kind": "postal", "label": "Blk 560 Pasir Ris St 51 · 510560", "value": "510560"},
            results,
        )
        self.assertEqual(
            self.results("510560"),
            [{"kind": "postal", "label": "Blk 560 Pasir Ris St 51 · 510560", "value": "510560"}],
        )

    def test_label_four_kinds(self):
        cases = (
            ("306A Sengkang", "block", "Blk 306A Anchorvale Link", "Blk 306A Anchorvale Link"),
            ("Pasir Ris St 51", "street", "Pasir Ris St 51", "PASIR RIS ST 51"),
            ("Pasir Ris", "town", "Pasir Ris", "PASIR RIS"),
            ("Whampoa", "town", "Kallang / Whampoa", "KALLANG/WHAMPOA"),
            ("510560", "postal", "Blk 560 Pasir Ris St 51 · 510560", "510560"),
        )
        for query, kind, label, value in cases:
            with self.subTest(kind=kind):
                results = self.results(query)
                self.assertLessEqual(len(results), 8)
                self.assertIn({"kind": kind, "label": label, "value": value}, results)
        self.assertEqual(self.results("Blk 306A Anchorvale"), self.results("306A Anchorvale"))

    def test_misspelt_short_form_towns(self):
        cases = (
            ("chua chu", "Choa Chu Kang", "CHOA CHU KANG"),
            ("chua", "Choa Chu Kang", "CHOA CHU KANG"),
            ("cck", "Choa Chu Kang", "CHOA CHU KANG"),
            ("amk", "Ang Mo Kio", "ANG MO KIO"),
            ("TPY", "Toa Payoh", "TOA PAYOH"),
            ("pungol", "Punggol", "PUNGGOL"),
            ("kalang", "Kallang / Whampoa", "KALLANG/WHAMPOA"),
        )
        for query, label, value in cases:
            with self.subTest(query=query):
                results = self.results(query)
                self.assertEqual(results[0], {"kind": "town", "label": label, "value": value})
                self.assertLessEqual(len(results), 8)
                listings, filters = findHomes({"q": results[0]["value"]})
                self.assertEqual(filters["town"], value)

    def test_town_guess_keeps_grouping(self):
        for query in ("pung", "Pasir Ris", "whampoa", "bukit"):
            with self.subTest(query=query):
                towns = [item["value"] for item in self.results(query) if item["kind"] == "town"]
                self.assertEqual(len(towns), len(set(towns)))
                self.assertTrue(towns)
        kinds = [item["kind"] for item in self.results("pung")]
        self.assertEqual(kinds[0], "town")
        self.assertIn("street", kinds)
        self.assertEqual([item["kind"] for item in self.results("chua")], ["town"])
        with self.assertNumQueries(0):
            self.assertEqual(hintTown("chua chu"), "CHOA CHU KANG")
            self.assertEqual(hintTown("garden"), "")
            self.assertEqual(hintTown("56"), "")
        for query in ("garden", "station"):
            self.assertNotIn("town", [item["kind"] for item in self.results(query)])

    def test_sectors_town_plus_type(self):
        for query, sector in (
            ("nor", "North"),
            ("north e", "North-East"),
            ("north-east", "North-East"),
            ("wes", "West"),
            ("central", "Central"),
        ):
            with self.subTest(query=query):
                self.assertIn({"kind": "area", "label": sector, "value": sector}, self.results(query))
        cases = (
            (
                "tampines 4 room",
                "Tampines · 4-room",
                "Tampines 4-room",
                {"town": "TAMPINES", "flatType": "4 ROOM"},
            ),
            (
                "5rm amk",
                "Ang Mo Kio · 5-room",
                "Ang Mo Kio 5-room",
                {"town": "ANG MO KIO", "flatType": "5 ROOM"},
            ),
            (
                "exec north east",
                "North-East · Executive",
                "North-East Executive",
                {"region": "North-East", "flatType": "EXECUTIVE"},
            ),
            (
                "kallang 3 room",
                "Kallang / Whampoa · 3-room",
                "Kallang/Whampoa 3-room",
                {"town": "KALLANG/WHAMPOA", "flatType": "3 ROOM"},
            ),
            (
                "yew tee 4 room",
                "Yew Tee · 4-room",
                "Yew Tee 4-room",
                {"q": "Yew Tee", "flatType": "4 ROOM"},
            ),
        )
        for query, label, value, params in cases:
            with self.subTest(query=query):
                results = self.results(query)
                self.assertEqual(results[0], {"kind": "search", "label": label, "value": value})
                self.assertEqual(tidySearch({"q": value}), params)
        for query in ("tampines", "4 room", "tampines blk 5 4 room"):
            self.assertNotIn("search", [item["kind"] for item in self.results(query)])

    def test_short_blank_unknown_markup(self):
        for query in ("", " ", "5", "p", "zzzz street", "999999", "<script>", "\x00"):
            with self.subTest(query=query):
                self.assertEqual(self.results(query), [])

    def test_endpoint_allows_get_shared(self):
        self.assertEqual(self.client.post(reverse("search_suggestions")).status_code, 405)
        with patch("storefront.views.addTry", return_value=121) as attempts:
            response = self.client.get(reverse("search_suggestions"), {"q": "Pasir"})
        attempts.assert_called_once_with("search-suggestions:127.0.0.1", 60)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {"results": []})
        self.assertEqual(response["Retry-After"], "60")

    def test_repeated_reuse_parsed_index(self):
        from storefront.address import blocks, hint_index

        self.results("Pasir Ris")
        data, index = blocks(), hint_index()
        misses = hint_index.cache_info().misses
        self.assertTrue(self.results("306A Sengkang"))
        self.assertTrue(self.results("510560"))
        self.assertIs(blocks(), data)
        self.assertIs(hint_index(), index)
        self.assertEqual(hint_index.cache_info().misses, misses)

    def test_both_forms_keep_plain(self):
        for name, boxes in (("home", 2), ("listings", 1)):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertContains(
                    response, 'data-search-suggestions="/api/search-suggestions"', count=boxes
                )
                self.assertContains(response, 'type="search" name="q"', count=boxes)
                self.assertContains(response, "Search flats by town, block, street or postal code", count=1)
                forms = re.findall("<form\\b.*?</form>", response.content.decode(), re.DOTALL)
                searchForms = [form for form in forms if 'type="search" name="q"' in form]
                self.assertEqual(len(searchForms), boxes)
                self.assertIn('class="header-search"', searchForms[0])
                for form in searchForms:
                    self.assertIn('method="get"', form)
                    self.assertNotRegex(form, '<select\\b[^>]*\\bname="town"')

    def test_sidebar_keeps_active_missing(self):
        makeListing(makeUser("sidebar-town@test.com", "seller"), town="BEDOK", street="BEDOK NTH AVE 1")
        plain = self.filterForm(self.client.get(reverse("listings")))
        self.assertNotIn('name="q"', plain)
        typed = self.filterForm(self.client.get(reverse("listings"), {"q": "bedok nth"}))
        self.assertIn('<input type="hidden" name="q" value="bedok nth">', typed)
        self.assertNotIn('type="search"', typed)
        response = self.client.get(reverse("listings"), {"q": "bedok nth", "flatType": "4 ROOM"})
        self.assertEqual([chip["label"] for chip in response.context["filterChips"]], ["bedok nth", "4-room"])

    def test_typed_town_shows_same(self):
        flat = makeListing(makeUser("town@test.com", "seller"), town="TAMPINES")
        makeListing(makeUser("other-town@test.com", "seller"), town="BEDOK")
        typed = self.client.get(reverse("listings"), {"q": "tampines"})
        linked = self.client.get(reverse("listings"), {"town": "TAMPINES"})
        for response in (typed, linked):
            self.assertEqual(response.status_code, 200)
            self.assertEqual({item.pk for item in response.context["listings"]}, {flat.pk})
            self.assertEqual(response.context["town"], "TAMPINES")
            self.assertEqual(response.context["searchQuery"], "")
            self.assertEqual(response.context["filterChips"], [{"label": "Tampines", "url": "?"}])
            self.assertContains(response, 'aria-label="Remove Tampines"')
            self.assertContains(response, 'name="q" value="Tampines"')
            self.assertNotIn('type="hidden" name="town"', self.filterForm(response))
            self.assertContains(response, '<a class="clear-filters" href="/listings">Clear all</a>')
            self.assertIn(("town", "TAMPINES"), response.context["sortFields"])
            self.assertEqual(parse_qs(response.context["pageQuery"]), {"town": ["TAMPINES"]})

    def test_town_persists_type_budget(self):
        flat = makeListing(
            makeUser("town-filter@test.com", "seller"),
            town="TAMPINES",
            flatType="4 ROOM",
            askingPrice=500000,
        )
        makeListing(
            makeUser("town-filter-other@test.com", "seller"),
            town="BEDOK",
            flatType="4 ROOM",
            askingPrice=500000,
        )
        response = self.client.get(reverse("listings"), {"q": "tampines"})
        params = response.context["searchParams"].copy()
        params.update(flatType="4 ROOM", maxPrice="550000")
        response = self.client.get(reverse("listings"), params)
        self.assertEqual({item.pk for item in response.context["listings"]}, {flat.pk})
        self.assertEqual(response.context["town"], "TAMPINES")
        self.assertContains(response, 'name="q" value="Tampines"')
        self.assertNotIn('type="hidden" name="town"', self.filterForm(response))
        chip = next((chip for chip in response.context["filterChips"] if chip["label"] == "Tampines"))
        self.assertNotIn("town", parse_qs(chip["url"][1:]))
        self.assertFalse(self.client.get(reverse("listings")).context["town"])

    def test_typed_kallang_whampoa_variants(self):
        flat = makeListing(makeUser("whampoa@test.com", "seller"), town="KALLANG/WHAMPOA")
        for query in ("kallang/whampoa", "Kallang / Whampoa", " KALLANG/ Whampoa "):
            with self.subTest(query=query):
                response = self.client.get(reverse("listings"), {"q": query})
                self.assertEqual(response.context["town"], "KALLANG/WHAMPOA")
                self.assertEqual(response.context["searchQuery"], "")
                self.assertEqual({item.pk for item in response.context["listings"]}, {flat.pk})

    def test_selected_exactly_their_location(self):
        seller = makeUser("suggestions@test.com", "seller")
        flat = makeListing(
            seller, block="560", street="PASIR RIS ST 51", postalCode="510560", town="PASIR RIS"
        )
        neighbour = makeListing(
            makeUser("suggestion-neighbour@test.com", "seller"),
            block="561",
            street="PASIR RIS ST 51",
            postalCode="510561",
            town="PASIR RIS",
        )
        otherStreet = makeListing(
            makeUser("suggestion-street@test.com", "seller"),
            block="123",
            street="PASIR RIS ST 512",
            postalCode="999991",
            town="PASIR RIS",
        )
        otherBlock = makeListing(
            makeUser("suggestion-block@test.com", "seller"),
            block="1560",
            street="PASIR RIS ST 51",
            postalCode="999992",
            town="YISHUN",
        )
        makeListing(
            makeUser("suggestion-town@test.com", "seller"),
            block="100",
            street="PASIR RIS",
            postalCode="999993",
            town="YISHUN",
        )
        makeListing(
            seller,
            block="560",
            street="PASIR RIS ST 51",
            postalCode="510560",
            town="PASIR RIS",
            status="hidden",
        )
        cases = (
            ("510560", "postal", {flat.pk}),
            ("560 Pasir Ris St 51", "block", {flat.pk}),
            ("Pasir Ris", "town", {flat.pk, neighbour.pk, otherStreet.pk}),
        )
        for query, kind, expected in cases:
            suggestion = next((item for item in self.results(query) if item["kind"] == kind))
            with self.subTest(kind=kind):
                listings, _ = findHomes({"q": suggestion["value"]})
                self.assertEqual({item.pk for item in listings}, expected)
        street = next((item for item in self.results("Pasir Ris St 51") if item["kind"] == "street"))
        listings, _ = findHomes({"q": street["value"]})
        self.assertEqual({item.pk for item in listings}, {flat.pk, neighbour.pk, otherBlock.pk})


class BudgetParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.inputs = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") in ("minPrice", "maxPrice"):
            self.inputs[attrs["name"]] = attrs


class BudgetTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("budget-seller@test.com", "seller")
        cls.low = makeListing(cls.seller, askingPrice=405000)
        cls.middle = makeListing(makeUser("budget-middle@test.com", "seller"), askingPrice=600000)
        cls.high = makeListing(makeUser("budget-high@test.com", "seller"), askingPrice=905000)

    def test_min_max_filter_inclusively(self):
        for params, expected in (
            ({"minPrice": "600000"}, {self.middle.pk, self.high.pk}),
            ({"maxPrice": "600000"}, {self.low.pk, self.middle.pk}),
            ({"minPrice": "600000", "maxPrice": "600000"}, {self.middle.pk}),
            ({"minPrice": "500000", "maxPrice": "900000"}, {self.middle.pk}),
        ):
            with self.subTest(params=params):
                response = self.client.get(reverse("listings"), params)
                self.assertEqual({flat.pk for flat in response.context["listings"]}, expected)

    def test_crossed_handles_swap_missing(self):
        with patch("storefront.views.LISTINGS_PER_PAGE", 1):
            response = self.client.get(
                reverse("listings"),
                {"minPrice": "909000", "maxPrice": "401000", "sort": "price_asc", "page": "2"},
            )
        self.assertEqual(response.context["minPrice"], 401000)
        self.assertEqual(response.context["maxPrice"], 909000)
        self.assertEqual(response.context["pageObj"].number, 2)
        self.assertEqual(response.context["searchParams"], {"minPrice": "401000", "maxPrice": "909000"})
        query = parse_qs(response.context["pageQuery"])
        self.assertEqual(query, {"minPrice": ["401000"], "maxPrice": ["909000"], "sort": ["price_asc"]})
        self.assertContains(response, "minPrice=401000&amp;maxPrice=909000&amp;sort=price_asc&amp;page=1")
        chips = response.context["filterChips"]
        self.assertEqual([chip["label"] for chip in chips], ["From $401,000", "Up to $909,000"])
        self.assertEqual(parse_qs(chips[0]["url"][1:]), {"maxPrice": ["909000"], "sort": ["price_asc"]})
        self.assertEqual(parse_qs(chips[1]["url"][1:]), {"minPrice": ["401000"], "sort": ["price_asc"]})

    def test_both_handles_clamp_bounds(self):
        for params, expected in (
            ({"minPrice": "-10000", "maxPrice": "999999999"}, ("", "")),
            ({"minPrice": "999999999", "maxPrice": "-10000"}, ("", "")),
            ({"minPrice": "999999999"}, (910000, "")),
            ({"maxPrice": "1"}, ("", 400000)),
        ):
            with self.subTest(params=params):
                response = self.client.get(reverse("listings"), params)
                self.assertEqual((response.context["minPrice"], response.context["maxPrice"]), expected)

    def test_invalid_prices_are_ignored(self):
        for price in ("NaN", "inf", "-inf", "bad", ""):
            with self.subTest(price=price):
                response = self.client.get(reverse("listings"), {"minPrice": price, "maxPrice": price})
                self.assertEqual(response.context["total"], 3)
                self.assertEqual(response.context["filterChips"], [])

    def test_bounds_one_aggregate_exclude(self):
        makeListing(makeUser("budget-hidden@test.com", "seller"), askingPrice=10000, status="hidden")
        inactive = makeUser("budget-inactive@test.com", "seller")
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])
        makeListing(inactive, askingPrice=2000000)
        with self.assertNumQueries(1):
            self.assertEqual(costRange(), (400000, 910000))
        for page in ("home", "listings"):
            with self.subTest(page=page):
                response = self.client.get(reverse(page))
                parser = BudgetParser()
                parser.feed(response.content.decode())
                for field, label, value in (
                    ("minPrice", "Lowest price", "400000"),
                    ("maxPrice", "Highest price", "910000"),
                ):
                    self.assertEqual(
                        parser.inputs[field],
                        {
                            "type": "range",
                            "name": field,
                            "min": "400000",
                            "max": "910000",
                            "step": "10000",
                            "value": value,
                            "aria-label": label,
                        },
                    )
                self.assertContains(response, '<span class="budget-end">$400,000</span>', html=True)
                self.assertContains(response, '<span class="budget-end">$910,000</span>', html=True)
                self.assertContains(response, "$400,000 – $910,000")

    def test_same_price_bounds_empty(self):
        self.low.askingPrice = 600000
        self.low.save(update_fields=["askingPrice"])
        self.high.askingPrice = 600000
        self.high.save(update_fields=["askingPrice"])
        self.assertEqual(costRange(), (600000, 600000))
        for page in ("home", "listings"):
            response = self.client.get(reverse(page))
            self.assertContains(response, 'min="600000" max="600000" step="10000" value="600000"', count=2)
        for flat in (self.low, self.middle, self.high):
            flat.status = "hidden"
            flat.save(update_fields=["status"])
        self.assertEqual(costRange(), (None, None))
        for page in ("home", "listings"):
            response = self.client.get(reverse(page))
            self.assertContains(response, 'min="0" max="3000000" step="10000"', count=2)

    def test_min_home_query_redirects(self):
        response = self.client.get(reverse("home"), {"minPrice": "600000"})
        self.assertRedirects(response, reverse("listings") + "?minPrice=600000")

    def test_saved_cleans_swaps_retains(self):
        buyer = makeUser("budget-buyer@test.com")
        self.client.force_login(buyer)
        self.client.post(reverse("save_search"), {"minPrice": "0900000", "maxPrice": "0500000", "page": "2"})
        savedSearch = SavedSearch.objects.get()
        expected = {"minPrice": "500000", "maxPrice": "900000"}
        self.assertEqual(savedSearch.queryParams, expected)
        self.assertEqual(tidySearch(savedSearch.queryParams), expected)
        self.assertEqual(savedSearch.name, "Flats from $500k under $900k")
        self.assertEqual(
            tidySearch({"minPrice": "9999999", "maxPrice": "1"}, 910000, 400000),
            {"minPrice": "400000", "maxPrice": "910000"},
        )
        with self.captureOnCommitCallbacks(execute=True):
            makeListing(makeUser("budget-cheap@test.com", "seller"), askingPrice=450000)
        self.assertFalse(SearchAlert.objects.exists())
        with self.captureOnCommitCallbacks(execute=True):
            flat = makeListing(makeUser("budget-match@test.com", "seller"), askingPrice=550000)
        self.assertEqual(SearchAlert.objects.get().listing, flat)

    def test_saved_allows_zero_high(self):
        buyer = makeUser("budget-wide@test.com")
        savedSearch = SavedSearch.objects.create(
            buyer=buyer, queryParams={"minPrice": 0, "maxPrice": 4000000}
        )
        self.assertEqual(savedSearch.queryParams, {"minPrice": "0", "maxPrice": "4000000"})

    def test_home_price_check_actions(self):
        response = self.client.get(reverse("home"))
        html = response.content.decode()
        priceTab = html.split('id="sell-panel"', 1)[1].split("</form>", 1)[0]
        self.assertIn(f'action="{reverse('valuation')}"', priceTab)
        self.assertIn('class="btn btn-brand" type="submit">Check a price', priceTab)
        self.assertNotIn("List my flat free", priceTab)
        priceCard = html.split('class="tool-card tool-card-price', 1)[1].split("</article>", 1)[0]
        self.assertIn(f'href="{reverse('valuation')}">Price check', priceCard)
        self.assertNotIn("List my flat free", priceCard)
        photoCard = html.split('class="tool-card tool-card-photo', 1)[1].split("</article>", 1)[0]
        self.assertIn("List my flat free", photoCard)
        sellerSection = html.split('class="how-panel how-panel-sell"', 1)[1].split("</div>", 1)[0]
        self.assertIn("List my flat free", sellerSection)


def render(tag):
    return Template("{% load shop_tags %}" + tag).render(Context())


def townsOf(context):
    return {town["town"]: town for group in context["regions"] for town in group["towns"]}


class TownMapDataTests(TestCase):

    def test_sits_in_exactly_one(self):
        grouped = [town for towns in REGIONS.values() for town in towns]
        self.assertEqual(len(grouped), len(set(grouped)))
        self.assertEqual(set(TOWNS) - set(grouped), set())
        self.assertEqual(list(REGIONS), ["North", "North-East", "East", "West", "Central"])

    def test_outline_on(self):
        paths = areaPaths()
        self.assertEqual(len(paths), 55)
        for town in TOWNS:
            for area in TOWN_AREAS.get(town, [town]):
                self.assertIn(area, paths)

    def test_area_missing_own_leads(self):
        owned = [area for town in TOWNS for area in TOWN_AREAS.get(town, [town])]
        nearby = [area for areas in NEARBY_AREAS.values() for area in areas]
        self.assertEqual(set(NEARBY_AREAS) - set(TOWNS), set())
        self.assertEqual(len(owned + nearby), len(set(owned + nearby)))
        self.assertEqual(set(owned + nearby), set(areaPaths()))

    def test_label_point_inside(self):
        for town in TOWNS:
            x, y = townPoint(town)
            self.assertTrue(0 < x < 1000 and 0 < y < 647)
        self.assertEqual(townPoint("TENGAH"), centroids()["TENGAH"])
        self.assertIsNone(townPoint("ATLANTIS"))

    def test_default_view_holds_area(self):
        left, top, width, height = mapBounds()
        self.assertEqual(mapBounds(), [-20.0, -20.0, 1039.9, 686.6])
        for path in areaPaths().values():
            numbers = [float(number) for number in re.findall("-?\\d+\\.?\\d*", path)]
            self.assertTrue(left < min(numbers[0::2]) and max(numbers[0::2]) < left + width)
            self.assertTrue(top < min(numbers[1::2]) and max(numbers[1::2]) < top + height)

    def test_region_names_matched_missing(self):
        self.assertEqual(regionName(" north-EAST "), "North-East")
        self.assertEqual(regionName("Atlantis"), "")
        self.assertEqual(regionName(None), "")


class TownMapLinkTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        makeListing(self.seller, town="TAMPINES")
        makeListing(makeUser("second@test.com", "seller"), town="TAMPINES")
        makeListing(makeUser("third@test.com", "seller"), town="BEDOK")
        makeListing(makeUser("fourth@test.com", "seller"), town="CENTRAL AREA")
        makeListing(makeUser("fifth@test.com", "seller"), town="KALLANG/WHAMPOA")
        makeListing(self.seller, town="PUNGGOL", status="hidden")
        makeListing(makeUser("sixth@test.com", "seller"), town="YISHUN", status="review")

    def test_counts_live_flats_per(self):
        with self.assertNumQueries(1):
            context = town_map("link")
        towns = townsOf(context)
        self.assertEqual(
            {name: town["count"] for name, town in towns.items()},
            {"TAMPINES": 2, "BEDOK": 1, "CENTRAL AREA": 1, "KALLANG/WHAMPOA": 1},
        )
        self.assertEqual(towns["TAMPINES"]["caption"], "Tampines · 2 flats")
        self.assertEqual(towns["BEDOK"]["spoken"], "Bedok, 1 flat")
        self.assertEqual(context["rest"], "Pick an area")
        self.assertTrue(context["allCurrent"])

    def test_flats_rest_plain_outlines(self):
        html = render("{% town_map mode='link' %}")
        self.assertIn('href="/listings?town=TAMPINES"', html)
        self.assertIn('aria-label="Tampines, 2 flats"', html)
        self.assertIn("<title>Tampines · 2 flats</title>", html)
        self.assertIn('href="/listings?town=KALLANG%2FWHAMPOA"', html)
        self.assertNotIn("town=PUNGGOL", html)
        self.assertNotIn("town=YISHUN", html)
        self.assertEqual(html.count("<path "), 55)
        self.assertEqual(html.count('class="townmap-area'), 4)
        self.assertIn('<g class="townmap-rest" aria-hidden="true">', html)
        self.assertEqual(html.count("town-map.js"), 1)
        self.assertIn('data-x="705.3" data-y="250.8"', html)
        self.assertIn('<tspan x="705.3" dy="0.00em">Tampines</tspan>', html)
        self.assertIn(
            '<tspan x="506.4" dy="-0.55em">Central</tspan><tspan x="506.4" dy="1.1em">Area</tspan>',
            html,
        )
        self.assertIn(
            '<tspan x="508.3" dy="-0.55em">Kallang/</tspan><tspan x="508.3" dy="1.1em">Whampoa</tspan>',
            html,
        )
        self.assertEqual(html.count('class="townmap-name"'), 4)
        self.assertTrue(
            html.index('<g class="townmap-names" aria-hidden="true">') > html.rindex('class="townmap-area')
        )
        self.assertIn('viewBox="-20.0 -20.0 1039.9 686.6" style="aspect-ratio: 1039.9 / 686.6"', html)

    def test_caption_sits_between_chips(self):
        html = render("{% town_map mode='link' %}")
        chips = html.index('class="townmap-chips"')
        caption = html.index('<p class="townmap-caption" data-rest="Pick an area">Pick an area</p>')
        stage = html.index('<div class="townmap-stage">')
        self.assertTrue(chips < caption < stage)
        self.assertIn('<span class="townmap-label townmap-hover" aria-hidden="true" hidden>', html)
        self.assertIn('<span class="townmap-label townmap-pin" aria-hidden="true" hidden>', html)
        self.assertNotIn("aria-live", html)

    def test_empty_prompt_leaves_caption(self):
        html = render("{% town_map mode='link' prompt='' %}")
        self.assertIn('<p class="townmap-caption" data-rest=""></p>', html)
        self.assertIn('aria-current="true">All</a>', html)
        html = render("{% town_map mode='link' prompt='' selected='TAMPINES' %}")
        self.assertIn(
            '<p class="townmap-caption" data-rest="Tampines · 2 flats">Tampines · 2 flats</p>', html
        )
        self.assertNotIn('aria-current="true">All</a>', html)
        self.assertEqual(town_map("link", prompt="Choose a town")["rest"], "Choose a town")

    def test_list_argument_drops_small(self):
        self.assertIn('<select name="town">', render("{% town_map mode='link' %}"))
        html = render("{% town_map mode='link' list=False %}")
        self.assertNotIn("<select", html)
        self.assertNotIn("townmap-list", html)
        self.assertIn('href="/listings?town=TAMPINES"', html)

    def test_central_area_kallang_whampoa(self):
        context = town_map("link")
        towns = townsOf(context)
        self.assertEqual(len(towns["CENTRAL AREA"]["paths"]), 11)
        self.assertEqual(len(towns["KALLANG/WHAMPOA"]["paths"]), 2)
        self.assertEqual(len(towns["TAMPINES"]["paths"]), 1)
        drawn = sum((len(town["paths"]) + len(town["near"]) for town in towns.values())) + len(
            context["others"]
        )
        self.assertEqual(drawn, 55)
        self.assertNotIn(areaPaths()["NOVENA"], context["others"])
        self.assertIn(areaPaths()["PUNGGOL"], context["others"])

    def test_keep_other_filters_drop(self):
        query = {
            "flatType": "4 ROOM",
            "maxPrice": "600000",
            "sort": "price_asc",
            "page": "3",
            "map": "1",
            "q": "bedok north",
            "town": "BEDOK",
            "region": "East",
            "junk": "1",
        }
        context = town_map("link", query=query)
        kept = "flatType=4+ROOM&maxPrice=600000&sort=price_asc"
        self.assertEqual(townsOf(context)["TAMPINES"]["href"], "/listings?" + kept + "&town=TAMPINES")
        self.assertEqual(context["regions"][1]["href"], "/listings?" + kept + "&region=North-East")
        self.assertEqual(context["allHref"], "/listings?" + kept)
        self.assertEqual(town_map("link")["allHref"], "/listings")
        html = render("{% town_map mode='link' %}")
        self.assertIn('href="/listings?region=North-East"', html)

    def test_selected_marked_named_in(self):
        context = town_map("link", selected="tampines", region="North")
        self.assertTrue(townsOf(context)["TAMPINES"]["selected"])
        self.assertEqual(context["rest"], "Tampines · 2 flats")
        self.assertFalse(any((group["current"] for group in context["regions"])))
        self.assertFalse(context["allCurrent"])
        html = render("{% town_map mode='link' selected='TAMPINES' %}")
        self.assertIn(
            'class="townmap-area region-east is-selected" href="/listings?town=TAMPINES" aria-current="true"',
            html,
        )
        self.assertIn('<option value="TAMPINES" selected>Tampines (2)</option>', html)
        self.assertIn('data-rest="Tampines · 2 flats"', html)

    def test_selected_region_marks_chip(self):
        context = town_map("link", region="east")
        self.assertEqual([group["name"] for group in context["regions"] if group["current"]], ["East"])
        towns = townsOf(context)
        self.assertTrue(towns["TAMPINES"]["inRegion"] and towns["BEDOK"]["inRegion"])
        self.assertFalse(towns["CENTRAL AREA"]["inRegion"])
        self.assertEqual(context["rest"], "East · 3 flats")
        html = render("{% town_map mode='link' region='East' %}")
        self.assertIn('data-region="east" data-caption="East · 3 flats" aria-current="true">East</a>', html)
        self.assertEqual(html.count("is-region"), 2)
        self.assertEqual(html.count('aria-current="true"'), 1)


class TownMapPickTests(TestCase):

    def test_mode_offers_missing_query(self):
        with self.assertNumQueries(0):
            context = town_map("pick", field="id_town")
        self.assertEqual(set(townsOf(context)), set(TOWNS))
        self.assertEqual(townsOf(context)["TAMPINES"]["caption"], "Tampines")
        self.assertEqual(context["rest"], "Pick your town on the map")
        self.assertEqual(town_map("pick", selected="BEDOK")["rest"], "Bedok selected")
        self.assertFalse(context["showList"])

    def test_mode_shows_buttons_named(self):
        html = render("{% town_map mode='pick' field='id_town' selected='BEDOK' %}")
        self.assertIn('class="townmap townmap-pick" data-mode="pick" data-field="id_town"', html)
        self.assertIn(
            'href="#" role="button" data-town="TAMPINES" aria-pressed="false" aria-label="Tampines"',
            html,
        )
        self.assertIn('href="#" role="button" data-town="BEDOK" aria-pressed="true"', html)
        self.assertEqual(html.count('role="button"'), len(TOWNS))
        self.assertNotIn("/listings", html)
        self.assertIn('<button class="townmap-chip" type="button" data-region="north-east"', html)
        self.assertIn(
            '<button class="townmap-chip" type="button" data-region="" aria-pressed="true">Whole island</button>',
            html,
        )
        self.assertIn('<p class="townmap-caption" data-rest="Bedok selected">Bedok selected</p>', html)
        self.assertIn('<span class="townmap-status visually-hidden" aria-live="polite"></span>', html)

    def test_mode_never_shows_dropdown(self):
        for tag in (
            "{% town_map mode='pick' field='id_town' %}",
            "{% town_map mode='pick' list=True %}",
        ):
            html = render(tag)
            self.assertNotIn("<select", html)
            self.assertNotIn("townmap-list", html)
            self.assertEqual(html.count('class="townmap-towns"'), 5)
            self.assertEqual(html.count('class="townmap-town"'), len(TOWNS))
            self.assertIn('data-towns="north" hidden>', html)
            self.assertIn(
                '<button class="townmap-town" type="button" data-town="YISHUN" data-label="Yishun" aria-pressed="false">Yishun</button>',
                html,
            )

    def test_mode_carries_prompts_starts(self):
        html = render("{% town_map mode='pick' field='id_town' %}")
        self.assertIn('data-prompt="Pick your town on the map" data-prompt-small="Pick a sector first"', html)
        self.assertIn('data-chosen=" selected" hidden>', html)
        self.assertNotIn(" hidden>\n  <nav", render("{% town_map mode='link' %}"))
