from storefront.models import Listing
from storefront.templatetags.estate_tags import flat_label
from urllib.parse import parse_qs
from storefront.search import roadWords
from storefront.search import tidySearch
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, SimpleTestCase
from django.urls import reverse

from adminpanel.forms import ListingDetailsForm, ProductForm, StaffForm, TicketForm
from .helpers import FLAT
from sellerpanel.forms import FLOOR_ERROR, PRICE_ERROR, SIZE_ERROR, ListingForm, ReviewForm
from sellerpanel.stripepay import mightSave, savings
from storefront.address import get_postal, nearest_mrt, nearest_station, blocks, street_names
from storefront.forms import RegisterForm, ValuationForm
from storefront.interiors import mapDots
from storefront.models import Sale
from storefront.offeradvice import floorCosts, lastSales, usualSales
from storefront.search import searchData, readSearch, findHomes
from storefront.tests.helpers import makeListing, makeUser
from storefront.valuation import TOWNS


class ListingFormTests(TestCase):

    def form(self, **changes):
        form = ListingForm({**FLAT, **changes})
        form.is_valid()
        return form

    def test_cleans_address_balcony(self):
        form = self.form()
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["block"], "225A")
        self.assertIs(form.cleaned_data["hasBalcony"], False)
        self.assertEqual(form.address["label"], "Blk 225A Jurong East St 21")

    def test_fills_missing_details_address(self):
        form = self.form(flatModel="", town="", block="")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["flatModel"], "New Generation")
        self.assertEqual(form.cleaned_data["town"], "JURONG EAST")
        self.assertEqual(form.cleaned_data["block"], "225A")

    def test_blocks_invalid_address_lease(self):
        self.assertIn("block", self.form(block="12-34").errors)
        self.assertIn("postalCode", self.form(postalCode="60122").errors)
        self.assertIn("leaseCommenceYear", self.form(leaseCommenceYear="1990").errors)
        self.assertIn("askingPrice", self.form(askingPrice="1000").errors)

    def test_blocks_address_size_model(self):
        form = self.form(floorAreaSqm="120")
        self.assertIn(
            "3-room flats at Blk 225A Jurong East St 21 are about 67 sqm",
            form.errors["floorAreaSqm"][0],
        )
        form = self.form(flatType="2 ROOM", flatModel="Maisonette", floorAreaSqm="45")
        self.assertIn("flatModel", form.errors)

    def test_unusual_storey_produces_warning(self):
        form = self.form(storey="20")
        self.assertTrue(form.is_valid())
        self.assertIn("storey", form.warnings)
        self.assertIn("the 20th floor looks too high", form.fields["storey"].warning)

    def test_postal_allows_spaces_sets(self):
        form = ListingForm({**FLAT, "postalCode": "601 225"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIn('maxlength="7"', str(form["postalCode"]))

    def test_review_enforces_rating_comment(self):
        self.assertTrue(ReviewForm({"rating": "3", "comment": "ok"}).is_valid())
        self.assertFalse(ReviewForm({"rating": "0"}).is_valid())
        self.assertFalse(ReviewForm({"rating": "5", "comment": "x" * 501}).is_valid())

    def test_specific_field_error_messages(self):
        form = self.form(floorAreaSqm="abc", storey="x", town="ZZZ", bedrooms="9")
        self.assertEqual(form.errors["floorAreaSqm"], [SIZE_ERROR])
        self.assertEqual(form.errors["storey"], [FLOOR_ERROR])
        self.assertEqual(form.errors["town"], ["Choose a town from the list."])
        self.assertEqual(form.errors["bedrooms"], ["Choose how many bedrooms your flat has."])


class OtherFormTests(TestCase):

    def test_register_cleans_email_marks(self):
        form = RegisterForm(
            {
                "fullName": "asdf",
                "email": "ASDF@test.com ",
                "password": "asdf",
                "confirmPassword": "asdf",
                "role": "buyer",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(form.cleaned_data["email"], "asdf@test.com")
        self.assertIn('aria-invalid="true"', str(form["password"]))

    def test_valuation_derives_town_model(self):
        form = ValuationForm(
            {
                "postal": "460157",
                "flatType": "4 ROOM",
                "floorAreaSqm": "92",
                "storey": "8",
                "leaseCommenceYear": "1985",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["town"], "BEDOK")
        self.assertEqual(form.cleaned_data["flatModel"], "Simplified")
        self.assertEqual(form.cleaned_data["area"], 92)

    def test_valuation_blocks_future_lease(self):
        form = ValuationForm(
            {
                "town": "BEDOK",
                "flatType": "4 ROOM",
                "area": "92",
                "storey": "8",
                "lease": str(date.today().year + 1),
            }
        )
        self.assertIn("lease", form.errors)
        form = ValuationForm({"flatType": "4 ROOM", "area": "92", "storey": "8", "lease": "1990"})
        self.assertIn("town", form.errors)

    def test_valuation_register_explain(self):
        errors = ValuationForm({"town": "ZZZ", "area": "abc", "storey": "x", "lease": "y"}).errors
        self.assertEqual(errors["town"], ["Choose a town from the list."])
        self.assertEqual(errors["flatType"], ["Choose a flat type, like 4-room."])
        self.assertEqual(errors["area"], ["Enter your flat’s size in sqm, like 92."])
        self.assertEqual(errors["storey"], ["Enter your floor level, from 1 to 50."])
        self.assertEqual(errors["lease"], ["Enter the year your lease started, like 1988."])
        errors = RegisterForm({}).errors
        self.assertEqual(errors["fullName"], ["Enter your full name."])
        self.assertEqual(errors["email"], ["Enter your email address."])
        self.assertEqual(errors["role"], ["Choose whether you want to buy or sell."])
        self.assertEqual(
            RegisterForm({"email": "asdf"}).errors["email"],
            ["Enter an email address like name@example.com."],
        )

    def test_staff_clear_required_invalid(self):
        errors = TicketForm({"email": "asdf"}).errors
        self.assertEqual(errors["email"], ["Enter an email address like name@example.com."])
        self.assertEqual(errors["topic"], ["Choose what your message is about."])
        errors = ProductForm({"price": "abc"}).errors
        self.assertEqual(errors["name"], ["Enter a name, like Boost 30 days."])
        self.assertEqual(errors["price"], ["Enter the price in dollars, like 60."])
        self.assertEqual(StaffForm({}).errors["fullName"], ["Enter their full name."])
        errors = ListingDetailsForm({"askingPrice": "abc"}).errors
        self.assertEqual(errors["askingPrice"], [PRICE_ERROR])
        self.assertEqual(errors["flatModel"], ["Choose a flat model."])


class HelperTests(TestCase):

    def test_postal_lookup_returns_block(self):
        record = get_postal("601225")
        self.assertEqual(
            (record["block"], record["town"], record["leaseYear"]), ("225A", "JURONG EAST", 1984)
        )
        self.assertEqual(record["flatTypes"][0]["label"], "3-room")
        self.assertIsNone(get_postal("asdf"))

    def test_nearest_station_lookup_handles(self):
        self.assertEqual(nearest_station("460157"), {"name": "Bayshore MRT", "metres": 800})
        self.assertIsNone(nearest_station("000000"))

    def sale(self, month, price, block, area=92, lease=1985, storeys="04 TO 06"):
        Sale.objects.create(
            month=month,
            town="BEDOK",
            flatType="4 ROOM",
            block=block,
            street="asdf",
            storeyRange=storeys,
            floorAreaSqm=area,
            flatModel="Model A",
            leaseCommenceYear=lease,
            price=price,
        )

    def test_recent_sales_rank_same(self):
        Sale.objects.all().delete()
        self.sale(date(2026, 1, 1), 500000, "157")
        self.sale(date(2026, 2, 1), 520000, "999")
        self.sale(date(2025, 6, 1), 480000, "157")
        self.sale(date(2026, 2, 1), 900000, "1", area=150)
        sales = list(lastSales("BEDOK", "4 ROOM", 90, "157"))
        self.assertEqual([sale.price for sale in sales], [500000, 480000, 520000])
        self.assertEqual(list(lastSales("", "4 ROOM", 90)), [])

    def test_typical_sales_calculates(self):
        Sale.objects.all().delete()
        self.assertEqual(usualSales("BEDOK", "4 ROOM"), {})
        for n in range(20):
            self.sale(date(2026, 1, 1), 400000 + n * 10000, "157")
        with self.assertNumQueries(4):
            typical = usualSales("BEDOK", "4 ROOM")
        self.assertEqual((typical["lo"], typical["est"], typical["hi"]), (420000, 500000, 580000))
        self.assertEqual(typical["saleCount"], 20)

    def test_savings_decimal_gst_never(self):
        self.assertEqual(
            savings(500000, 49),
            {
                "agentFee": Decimal("10900.00"),
                "cost": Decimal("49.00"),
                "saved": Decimal("10851.00"),
            },
        )
        self.assertEqual(savings(1000, 999)["saved"], Decimal("0.00"))
        self.assertEqual(mightSave()["examplePrice"], 620000)
        self.assertEqual(mightSave(612345)["agentFee"], Decimal("13300"))

    def test_map_dots_ignore_unknown(self):
        dots = mapDots(
            [
                {"town": "BEDOK", "count": 4},
                {"town": "KALLANG/WHAMPOA", "count": 1},
                {"town": "ASDF", "count": 2},
            ]
        )
        self.assertEqual([dot["town"] for dot in dots], ["BEDOK", "KALLANG/WHAMPOA"])
        self.assertEqual(dots[0]["r"], 18)

    def test_recent_sales_prioritize_similar(self):
        Sale.objects.all().delete()
        self.sale(date(2026, 3, 1), 950000, "1", lease=2020)
        self.sale(date(2026, 2, 1), 510000, "2", lease=1988, storeys="22 TO 24")
        self.sale(date(2026, 1, 1), 500000, "3", lease=1983, storeys="07 TO 09")
        sales = list(lastSales("BEDOK", "4 ROOM", 92, None, 1985, 8))
        self.assertEqual([sale.price for sale in sales], [500000, 510000])

    def test_floor_prices_recent_well(self):
        Sale.objects.all().delete()
        self.assertEqual(floorCosts("BEDOK", "4 ROOM", 8), {})
        month = date.today().replace(day=1)
        for n in range(5):
            self.sale(month, 500000, "1", storeys="01 TO 03")
            self.sale(month, 540000 + n * 3000, "2", storeys="07 TO 09")
            self.sale(month, 600000, "3", storeys="22 TO 24")
        self.sale(month, 900000, "4", storeys="40 TO 42")
        self.sale(date(2020, 1, 1), 100000, "5", storeys="01 TO 03")
        with self.assertNumQueries(1):
            floors = floorCosts("BEDOK", "4 ROOM", 8)
        self.assertEqual([row["label"] for row in floors["rows"]], ["22–24", "7–9", "1–3"])
        self.assertEqual([row["mine"] for row in floors["rows"]], [False, True, False])
        self.assertEqual(floors["rows"][1]["average"], 546000)
        self.assertEqual(floors["rows"][0]["width"], 100)
        self.assertEqual(floors["gap"], 100000)

    def test_nearest_mrt_includes_walk(self):
        mrt = nearest_mrt("460001")
        self.assertEqual(
            (mrt["name"], mrt["minutes"], mrt["metres"], mrt["exit"]),
            ("Bedok MRT", 11, 840, "Exit A"),
        )
        self.assertEqual(mrt["badges"][0]["code"], "EW5")
        self.assertEqual(mrt["badges"][0]["colour"], "#007A38")
        self.assertEqual(nearest_mrt("", "1", "QUEENSTOWN")["name"], "one-north MRT")
        self.assertIsNone(nearest_mrt("000000"))

    def test_nearest_mrt_falls_back(self):
        empty = {"stations": [], "lines": {}, "blocks": {}, "byBlock": {}}
        with patch("storefront.address.walk_index", return_value=empty):
            self.assertEqual(nearest_mrt("460157"), {"name": "Bayshore MRT", "metres": 800})


class EstateSearchTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("estate@test.com", "seller")
        cls.yewTee = makeListing(
            cls.seller, town="CHOA CHU KANG", street="CHOA CHU KANG NTH 6", postalCode=""
        )
        cls.teckWhye = makeListing(
            makeUser("teck@test.com", "seller"),
            town="CHOA CHU KANG",
            street="TECK WHYE LANE",
            postalCode="",
        )

    def found(self, **query):
        listings, filters = findHomes(query)
        return set(listings.values_list("pk", flat=True))

    def test_street_real_in_town(self):
        towns = {}
        for entry in blocks().values():
            towns.setdefault(entry["s"], set()).add(entry["t"])
        for name, (town, streets) in searchData()["areas"].items():
            self.assertIn(town, TOWNS)
            self.assertNotIn(name, TOWNS)
            for street in streets:
                self.assertIn(street, street_names())
                self.assertIn(town, towns[street])

    def test_name_finds_streets(self):
        self.assertEqual(self.found(q="yew tee"), {self.yewTee.pk})
        self.assertEqual(self.found(q="Yew  Tee 4 room"), {self.yewTee.pk})
        self.assertEqual(self.found(q="yew tee 5 room"), set())

    def test_results_page_names_town(self):
        response = self.client.get(reverse("listings"), {"q": "yew tee"})
        self.assertContains(response, "Yew Tee is part of Choa Chu Kang.")
        self.assertNotContains(response, "Teck Whye")
        self.assertNotContains(self.client.get(reverse("listings"), {"q": "teck whye"}), "is part of")

    def test_typing_suggests_first(self):
        results = self.client.get(reverse("search_suggestions"), {"q": "yew"}).json()["results"]
        self.assertEqual(results[0], {"kind": "area", "label": "Yew Tee", "value": "Yew Tee"})

    def test_joining_words_not_searched(self):
        found = readSearch("5-room in Punggol")
        self.assertEqual((found["flatType"], found["town"], found["q"]), ("5 ROOM", "PUNGGOL", ""))
        self.assertEqual(readSearch("flats near Yew Tee")["q"], "flats Yew Tee")
        self.assertEqual(readSearch("in")["q"], "in")


class DataUpdateTests(SimpleTestCase):

    def setUp(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from storefront import address
        from storefront import interiors
        import storefront.address as mrt
        from storefront import search

        self.json = json
        self.address = address
        self.interiors = interiors
        self.mrt = mrt
        self.search = search
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.blocks = self.folder / "blocks.json"
        self.stations = self.folder / "stations.csv"
        self.stops = self.folder / "stations.json"
        self.walks = self.folder / "walks.json"
        self.points = self.folder / "points.json"
        self.shapes = self.folder / "shapes.svg"
        for module, name, value in [
            (address, "BLOCKS_JSON", self.blocks),
            (mrt, "STATIONS_CSV", self.stations),
            (mrt, "STATIONS_JSON", self.stops),
            (mrt, "WALK_FILE", self.walks),
            (interiors, "CENTROIDS", self.points),
            (interiors, "OUTLINES", self.shapes),
        ]:
            change = patch.object(module, name, value)
            change.start()
            self.addCleanup(change.stop)
        for function in [
            address.blocks,
            address.search_index,
            address.hint_index,
            mrt.walk_index,
        ]:
            function.cache_clear()
            self.addCleanup(function.cache_clear)
        self.writeBlocks("OLD ROAD")

    def writeBlocks(self, street, postal="123456", town="BEDOK"):
        self.blocks.write_text(
            self.json.dumps({"blocks": {postal: {"b": "1", "s": street, "t": town, "y": 1980}}})
        )

    def searchFile(self):
        path = self.folder / "search.json"
        data = {
            "towns": {"SMALL": "BEDOK"},
            "areas": {"SMALL PLACE": ["OLD ROAD"]},
            "street_words": {"ROAD": "RD"},
            "number_words": {"ONE": "1"},
        }
        path.write_text(self.json.dumps(data))
        change = patch.object(self.search, "SEARCH_DATA", path)
        change.start()
        self.addCleanup(change.stop)

        return path, data

    def test_town_aliases_reload_search(self):
        path, data = self.searchFile()
        self.assertEqual(self.search.knownTown("small"), "BEDOK")
        data["towns"]["SMALL"] = "YISHUN"
        path.write_text(self.json.dumps(data))
        self.assertEqual(self.search.knownTown("small"), "YISHUN")

    def test_area_towns_follow_address(self):
        self.searchFile()
        self.assertEqual(self.search.searchedEstate({"q": "Small place"})["town"], "BEDOK")
        self.writeBlocks("OLD ROAD", town="YISHUN")
        self.assertEqual(self.search.searchedEstate({"q": "Small place"})["town"], "YISHUN")
        self.writeBlocks("NEW ROAD")
        self.assertEqual(self.search.estate_of("Small place"), "")

    def test_street_aliases_reload_search(self):
        path, data = self.searchFile()
        self.assertEqual(self.search.roadWords("Old Road One"), "OLD RD 1")
        data["street_words"]["STREET"] = "ST"
        path.write_text(self.json.dumps(data))
        self.assertEqual(self.search.roadWords("Old Street One"), "OLD ST 1")

    def test_blocks_reload_missing(self):
        original = self.address.blocks()
        self.assertIs(original, self.address.blocks())
        self.writeBlocks("NEW ROAD")
        self.assertEqual(self.address.blocks()["123456"]["s"], "NEW ROAD")

    def test_address_street_indexes_follow(self):
        self.assertTrue(self.address.search("OLD ROAD"))
        self.assertIn("OLD ROAD", self.address.street_names())
        self.writeBlocks("NEW ROAD")
        self.assertEqual(self.address.search("OLD ROAD"), [])
        self.assertTrue(self.address.search("NEW ROAD"))
        self.assertEqual(self.address.street_names(), frozenset(["NEW ROAD"]))

    def test_suggestions_follow_replaced(self):
        self.assertTrue(self.address.search_hints("123456"))
        self.writeBlocks("NEW ROAD", "654321")
        self.assertEqual(self.address.search_hints("123456"), [])
        self.assertTrue(self.address.search_hints("654321"))

    def test_search_prefixes_follow_address(self):
        self.assertIn("OLD", self.search.roadStarts())
        self.writeBlocks("NEW ROAD")
        self.assertNotIn("OLD", self.search.roadStarts())
        self.assertIn("NEW", self.search.roadStarts())

    def test_station_csv_updated_running(self):
        self.stations.write_text("name,lat,lon\nOld station,1.2,103.8\n")
        self.assertEqual(self.mrt.stations()[0][0], "Old station")
        self.stations.write_text("name,lat,lon\nNew station,1.3,103.9\n")
        self.assertEqual(self.mrt.stations()[0], ("New station", 1.3, 103.9))

    def test_walk_index_recovers_files(self):
        self.assertEqual(self.mrt.walk_index()["stations"], [])
        self.stops.write_text(
            self.json.dumps({"stations": [{"id": "demo", "name": "Demo station"}], "lines": {}})
        )
        self.walks.write_text(self.json.dumps({"stationIds": ["demo"], "blocks": {}, "byBlock": {}}))
        self.assertEqual(self.mrt.walk_index()["stations"][0]["name"], "Demo station")

    def test_map_points_reload_file(self):
        self.points.write_text(self.json.dumps({"centroids": {"BEDOK": [1.0, 2.0]}}))
        self.assertEqual(self.interiors.centroids()["BEDOK"], [1.0, 2.0])
        self.points.write_text(self.json.dumps({"centroids": {"BEDOK": [3.0, 4.0]}}))
        self.assertEqual(self.interiors.centroids()["BEDOK"], [3.0, 4.0])

    def test_map_shapes_bounds_follow(self):
        self.shapes.write_text('<svg><path data-area="BEDOK" d="M 0 0 L 10 0 L 10 20 Z"/></svg>')
        first = self.interiors.mapBounds()
        self.assertIn("10", self.interiors.areaPaths()["BEDOK"])
        self.shapes.write_text('<svg><path data-area="BEDOK" d="M 0 0 L 40 0 L 40 80 Z"/></svg>')
        self.assertNotEqual(self.interiors.mapBounds(), first)
        self.assertIn("40", self.interiors.areaPaths()["BEDOK"])


class ForgivingSearchTests(TestCase):

    def setUp(self):
        self.flats = {}
        places = (
            ("cck", "CHOA CHU KANG", "4 ROOM", "CHOA CHU KANG AVE 1"),
            ("tampinesFour", "TAMPINES", "4 ROOM", "TAMPINES ST 21"),
            ("tampinesFive", "TAMPINES", "5 ROOM", "TAMPINES ST 21"),
            ("amk", "ANG MO KIO", "5 ROOM", "ANG MO KIO AVE 10"),
            ("bedokNorth", "BEDOK", "4 ROOM", "BEDOK NTH AVE 1"),
            ("bedokOther", "BEDOK", "4 ROOM", "BEDOK RESERVOIR RD"),
            ("jurongEast", "JURONG EAST", "4 ROOM", "JURONG EAST ST 21"),
            ("jurongWest", "JURONG WEST", "4 ROOM", "JURONG WEST ST 41"),
            ("punggol", "PUNGGOL", "4 ROOM", "PUNGGOL FIELD"),
            ("bishan", "BISHAN", "EXECUTIVE", "BISHAN ST 22"),
            ("sengkang", "SENGKANG", "4 ROOM", "COMPASSVALE LANE"),
            ("woodlands", "WOODLANDS", "4 ROOM", "MARSILING DR"),
            ("toaPayoh", "TOA PAYOH", "3 ROOM", "LOR 1 TOA PAYOH"),
        )
        for index, (name, town, flatType, street) in enumerate(places):
            seller = makeUser(f"forgiving{index}@test.com", "seller")
            self.flats[name] = makeListing(
                seller,
                town=town,
                flatType=flatType,
                street=street,
                block=str(100 + index),
                postalCode=str(990000 + index),
            )

    def search(self, text, **more):
        return self.client.get(reverse("listings"), {"q": text, **more})

    def found(self, response):
        return {flat.pk for flat in response.context["listings"]}

    def pks(self, *names):
        return {self.flats[name].pk for name in names}

    def test_exact_town_names_filter(self):
        for text, name in (("punggol", "punggol"), ("Choa Chu Kang", "cck"), ("ang mo kio", "amk")):
            with self.subTest(text=text):
                response = self.search(text)
                self.assertEqual(self.found(response), self.pks(name))
                self.assertEqual(response.context["searchQuery"], "")
                self.assertEqual(response.context["correctedTown"], "")
                self.assertNotContains(response, "Showing flats in")

    def test_misspelt_towns_become_closest(self):
        cases = (
            ("chua chu kang", "CHOA CHU KANG", "cck"),
            ("pungol", "PUNGGOL", "punggol"),
            ("tampenis", "TAMPINES", None),
            ("seng kang", "SENGKANG", "sengkang"),
            ("woodland", "WOODLANDS", "woodlands"),
            ("toa payo", "TOA PAYOH", "toaPayoh"),
        )
        for text, town, name in cases:
            with self.subTest(text=text):
                response = self.search(text)
                self.assertEqual(response.context["town"], town)
                self.assertEqual(response.context["searchQuery"], "")
                self.assertEqual(response.context["searchParams"], {"town": town})
                self.assertEqual([chip["label"] for chip in response.context["filterChips"]], [town.title()])
                self.assertContains(
                    response,
                    f'<p class="result-note">Showing flats in {town.title()}.</p>',
                    html=True,
                )
                self.assertContains(response, f'type="hidden" name="q" value="{town.title()}"')
                if name:
                    self.assertEqual(self.found(response), self.pks(name))
        self.assertEqual(self.found(self.search("tampenis")), self.pks("tampinesFour", "tampinesFive"))

    def test_unrelated_words_street_not(self):
        for text in ("garden", "station", "yishun ave", "bedok reservoir", "blk 105"):
            with self.subTest(text=text):
                response = self.search(text)
                self.assertEqual(response.context["town"], "")
                self.assertEqual(response.context["searchQuery"], text)
                self.assertEqual(response.context["correctedTown"], "")
                self.assertNotContains(response, "Showing flats in")
        self.assertEqual(self.search("garden").context["total"], 0)
        self.assertEqual(self.found(self.search("bedok reservoir")), self.pks("bedokOther"))

    def test_short_forms_jurong_alone(self):
        for text, town, name in (
            ("amk", "ANG MO KIO", "amk"),
            ("CCK", "CHOA CHU KANG", "cck"),
            ("tpy", "TOA PAYOH", "toaPayoh"),
        ):
            with self.subTest(text=text):
                response = self.search(text)
                self.assertEqual(response.context["town"], town)
                self.assertEqual(self.found(response), self.pks(name))
                self.assertContains(response, f"Showing flats in {town.title()}.")
        self.assertEqual(self.search("whampoa").context["town"], "KALLANG/WHAMPOA")
        jurong = self.search("jurong")
        self.assertEqual(jurong.context["town"], "")
        self.assertEqual(self.found(jurong), self.pks("jurongEast", "jurongWest"))
        self.assertEqual(self.found(self.search("jurong west")), self.pks("jurongWest"))

    def test_town_flat_type_in(self):
        cases = (
            ("tampines 4 room", "TAMPINES", "4 ROOM", "tampinesFour"),
            ("4-room punggol", "PUNGGOL", "4 ROOM", "punggol"),
            ("executive bishan", "BISHAN", "EXECUTIVE", "bishan"),
            ("5rm amk", "ANG MO KIO", "5 ROOM", "amk"),
            ("Tampines 5-Rooms", "TAMPINES", "5 ROOM", "tampinesFive"),
            ("exec bishan", "BISHAN", "EXECUTIVE", "bishan"),
        )
        for text, town, flatType, name in cases:
            with self.subTest(text=text):
                response = self.search(text, sort="price_asc")
                self.assertEqual((response.context["town"], response.context["flatType"]), (town, flatType))
                self.assertEqual(response.context["searchQuery"], "")
                self.assertEqual(self.found(response), self.pks(name))
                self.assertEqual(response.context["searchParams"], {"town": town, "flatType": flatType})
                chips = response.context["filterChips"]
                self.assertEqual([chip["label"] for chip in chips], [town.title(), flat_label(flatType)])
                self.assertEqual(
                    parse_qs(chips[0]["url"][1:]), {"flatType": [flatType], "sort": ["price_asc"]}
                )
                self.assertEqual(parse_qs(chips[1]["url"][1:]), {"town": [town], "sort": ["price_asc"]})
                self.assertEqual(
                    parse_qs(response.context["pageQuery"]),
                    {"town": [town], "flatType": [flatType], "sort": ["price_asc"]},
                )
        response = self.search("tampines 4 room")
        self.assertEqual(response.context["typeTotal"], 2)
        self.assertContains(
            response, '<input type="radio" name="flatType" value="4 ROOM" checked>', html=True
        )
        self.assertNotContains(response, "Showing flats in")
        self.assertEqual(self.search("tampines 4 room", flatType="5 ROOM").context["flatType"], "5 ROOM")

    def test_words_left_over_town(self):
        response = self.search("tampines 4 room blk 101")
        self.assertEqual(
            (
                response.context["town"],
                response.context["flatType"],
                response.context["searchQuery"],
            ),
            ("TAMPINES", "4 ROOM", "blk 101"),
        )
        self.assertEqual(self.found(response), self.pks("tampinesFour"))
        self.assertEqual(
            [chip["label"] for chip in response.context["filterChips"]],
            ["blk 101", "Tampines", "4-room"],
        )
        self.assertEqual(
            tidySearch({"q": "bedok 3 room", "town": "TAMPINES"}),
            {"q": "bedok", "town": "TAMPINES", "flatType": "3 ROOM"},
        )

    def test_sector_words_become_region(self):
        northEast = self.pks("amk", "punggol", "sengkang")
        for text in ("north east", "North-East", "northeast", "in the north east"):
            with self.subTest(text=text):
                response = self.search(text)
                self.assertEqual(
                    (
                        response.context["region"],
                        response.context["town"],
                        response.context["searchQuery"],
                    ),
                    ("North-East", "", ""),
                )
                self.assertEqual(self.found(response), northEast)
                self.assertEqual([chip["label"] for chip in response.context["filterChips"]], ["North-East"])
                self.assertEqual(response.context["searchParams"], {"region": "North-East"})
                self.assertNotContains(response, "Showing flats in")
        self.assertEqual(self.search("in the west").context["region"], "West")
        self.assertEqual(self.search("central").context["region"], "Central")
        for text, town, region, left in (
            ("jurong west", "JURONG WEST", "", ""),
            ("central area", "CENTRAL AREA", "", ""),
            ("bedok north", "", "", "bedok north"),
            ("tampines east", "TAMPINES", "", "east"),
        ):
            with self.subTest(text=text):
                response = self.search(text)
                self.assertEqual(
                    (
                        response.context["town"],
                        response.context["region"],
                        response.context["searchQuery"],
                    ),
                    (town, region, left),
                )
        self.assertEqual(self.search("north east", town="BEDOK").context["region"], "")

    def test_money_words_become_price(self):
        prices = {"cck": 450000, "tampinesFour": 600000, "tampinesFive": 800000, "amk": 1100000}
        for name, price in prices.items():
            Listing.objects.filter(pk=self.flats[name].pk).update(askingPrice=price)
        Listing.objects.exclude(pk__in=self.pks(*prices)).update(status="hidden")
        cases = (
            ("under 600k", "", 600000, ("cck", "tampinesFour")),
            ("below $600,000", "", 600000, ("cck", "tampinesFour")),
            ("max 600k", "", 600000, ("cck", "tampinesFour")),
            ("600k to 800k", 600000, 800000, ("tampinesFour", "tampinesFive")),
            ("above 1m", 1000000, "", ("amk",)),
            ("over 0.7m", 700000, "", ("tampinesFive", "amk")),
        )
        for text, lowest, highest, names in cases:
            with self.subTest(text=text):
                response = self.search(text)
                self.assertEqual(
                    (response.context["minPrice"], response.context["maxPrice"]), (lowest, highest)
                )
                self.assertEqual(response.context["searchQuery"], "")
                self.assertEqual(self.found(response), self.pks(*names))
        response = self.search("tampines 4 room under 700k")
        self.assertEqual(
            [chip["label"] for chip in response.context["filterChips"]],
            ["Tampines", "4-room", "Up to $700,000"],
        )
        self.assertEqual(
            response.context["searchParams"],
            {"town": "TAMPINES", "flatType": "4 ROOM", "maxPrice": "700000"},
        )
        self.assertEqual(self.found(response), self.pks("tampinesFour"))
        self.assertEqual(self.search("under 600k", maxPrice="900000").context["maxPrice"], 900000)
        for text in ("under 600", "blk 10 to 12", "600-800k", "cheap", "max"):
            with self.subTest(text=text):
                response = self.search(text)
                self.assertEqual(
                    (
                        response.context["minPrice"],
                        response.context["maxPrice"],
                        response.context["searchQuery"],
                    ),
                    ("", "", text),
                )

    def test_number_words_street_word(self):
        self.assertEqual(roadWords("ang mo kio avenue ten"), "ANG MO KIO AVE 10")
        self.assertEqual(roadWords("bedok north street three"), "BEDOK NTH ST 3")
        self.assertEqual(roadWords("one north"), "ONE NTH")
        self.assertEqual(self.found(self.search("ang mo kio avenue ten")), self.pks("amk"))
        self.assertEqual(self.found(self.search("bedok north avenue one")), self.pks("bedokNorth"))
        self.assertEqual(
            self.found(self.search("ang mo kio avenue ten")),
            self.found(self.search("ang mo kio avenue 10")),
        )

    def test_street_words_matched_way(self):
        self.assertEqual(roadWords("Bukit Batok Street 21"), "BT BATOK ST 21")
        self.assertEqual(roadWords("upper serangoon crescent"), "UPP SERANGOON CRES")
        self.assertEqual(roadWords("punggol central"), "PUNGGOL CTRL")
        for text in (
            "bedok north avenue 1",
            "Bedok North Ave 1",
            "bedok north",
            "blk 104 bedok north avenue 1",
        ):
            with self.subTest(text=text):
                response = self.search(text)
                self.assertEqual(self.found(response), self.pks("bedokNorth"))
                self.assertEqual(response.context["searchQuery"], text)
        self.assertEqual(
            self.found(self.search("tampines street 21")), self.pks("tampinesFour", "tampinesFive")
        )
        self.assertEqual(self.found(self.search("toa payoh lorong 1")), self.pks("toaPayoh"))
        self.assertEqual(self.found(self.search("marsiling drive")), self.pks("woodlands"))
