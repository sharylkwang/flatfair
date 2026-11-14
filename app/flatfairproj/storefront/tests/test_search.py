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
