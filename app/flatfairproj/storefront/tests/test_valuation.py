from datetime import date
import math
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
import numpy as np

from storefront import address, valuation
from storefront.forms import ValuationForm
from storefront.models import Sale, flatIssues, sizeHint
from storefront.valuation import bandSpots, flagReason, verdict

QUERY = {"town": "BEDOK", "flatType": "4 ROOM", "area": "92", "storey": "8", "lease": "1990"}


class PageTests(TestCase):

    def test_initial_valuation_unbound_form(self):
        response = self.client.get(reverse("valuation"))
        self.assertFalse(response.context["form"].is_bound)
        self.assertNotIn("est", response.context)

    @patch("storefront.views.band")
    def test_invalid_valuation_links_error(self, band):
        response = self.client.get(
            reverse("valuation"), {**QUERY, "area": "abc", "storey": "99", "lease": "1900"}
        )
        self.assertContains(response, 'action="/valuation#valuation-feedback"')
        self.assertContains(response, 'id="valuation-feedback"', count=1)
        content = response.content.decode()
        summary = content.split('class="error-summary"', 1)[1].split("</div>", 1)[0]
        self.assertIn('role="alert" tabindex="-1"', summary)
        for field in response.context["form"]:
            for error in field.errors:
                self.assertInHTML(f'<a href="#{field.id_for_label}">{field.label}: {error}</a>', summary)
        self.assertLess(content.index('id="valuation-feedback"'), content.index('id="id_area"'))
        band.assert_not_called()

    def test_valuation_summary_includes_non(self):
        form = ValuationForm(QUERY)
        self.assertTrue(form.is_valid())
        form.add_error(None, "Check the flat details and try again.")
        content = render_to_string("storefront/valuation.html", {"form": form})
        self.assertInHTML('<a href="#valuation-fields">Check the flat details and try again.</a>', content)

    @patch("storefront.views.band", return_value=(548000, 577000, 639000))
    def test_valid_valuation_shows(self, band):
        response = self.client.get(reverse("valuation"), {**QUERY, "ask": "600000"})
        self.assertEqual(
            (response.context["lo"], response.context["est"], response.context["hi"]),
            (548000, 577000, 639000),
        )
        self.assertContains(response, "It’s our best estimate, not an official HDB valuation.")
        self.assertEqual(response.context["asking"], 600000)
        self.assertEqual(band.call_args.args[:6], ("BEDOK", "4 ROOM", "Model A", 92.0, 8, 1990))
        self.assertNotContains(response, 'class="error-summary"')
        self.assertContains(response, 'id="valuation-feedback"', count=1)
        content = response.content.decode()
        self.assertLess(content.index('id="valuation-result"'), content.index('id="valuation-feedback"'))

    def test_empty_six_question_form(self):
        response = self.client.get(reverse("valuation"))
        self.assertEqual(response.context["layout"], "")
        self.assertEqual(response.context["startStep"], 1)
        self.assertContains(response, 'storefront/price-check.js?v=3" defer')
        self.assertContains(response, 'data-steps data-start="1" data-check="/valuation/check"')
        self.assertContains(response, '<fieldset class="val-step"', count=6)
        legends = [
            "Where is the flat?",
            "What type of flat?",
            "How big is it?",
            "Which floor is it on?",
            "What year did the lease start?",
            "Anything else? (optional)",
        ]
        content = response.content.decode()
        places = []
        for legend in legends:
            self.assertContains(response, f'<legend tabindex="-1">{legend}</legend>', html=True)
            places.append(content.index(legend))
        self.assertEqual(places, sorted(places))
        self.assertEqual(
            [step["short"] for step in response.context["steps"]],
            ["Town", "Flat type", "Size", "Floor", "Lease start", "Extras"],
        )
        self.assertContains(response, 'class="val-layout" data-layout="/floor-plan/preview" hidden')
        self.assertNotContains(response, "/floor-plan/preview?")
        self.assertContains(response, 'id="id_area_hint"', count=1)
        self.assertContains(response, 'role="progressbar"')
        self.assertContains(response, "<h2>Working out the price</h2>", html=True)

    def test_flat_type_group_radio(self):
        response = self.client.get(reverse("valuation"))
        self.assertNotContains(response, '<select name="flatType"')
        for index, kind in enumerate(["2 ROOM", "3 ROOM", "4 ROOM", "5 ROOM", "EXECUTIVE"]):
            self.assertContains(
                response,
                f'<input type="radio" name="flatType" value="{kind}" required id="id_flatType_{index}">',
                html=True,
            )
        self.assertContains(response, 'class="val-choice"', count=5)
        self.assertContains(response, '<select name="town"')

    def test_prefilled_link_opens_at(self):
        query = {"town": "TAMPINES", "flatType": "4 ROOM", "storey": "7"}
        response = self.client.get(reverse("valuation"), query)
        self.assertFalse(response.context["form"].is_bound)
        self.assertEqual(response.context["startStep"], 3)
        self.assertContains(response, 'data-start="3"')
        self.assertContains(
            response,
            '<input type="radio" name="flatType" value="4 ROOM" required id="id_flatType_2" checked>',
            html=True,
        )
        self.assertContains(response, '<option value="TAMPINES" selected>Tampines</option>', html=True)
        self.assertNotContains(response, 'class="error-summary"')
        response = self.client.get(reverse("valuation"), {"flatType": "4 ROOM"})
        self.assertEqual(response.context["startStep"], 1)

    @patch("storefront.views.band", return_value=(548000, 577000, 639000))
    def test_invalid_submission_opens(self, band):
        response = self.client.get(reverse("valuation"), {**QUERY, "storey": "99"})
        self.assertEqual(response.context["startStep"], 4)
        self.assertContains(response, 'data-start="4"')
        self.assertContains(
            response,
            '<p class="val-note is-error" data-note="storey" id="id_storey_error" role="status">Enter a floor level from 1 to 50.</p>',
            html=True,
        )
        response = self.client.get(reverse("valuation"), {**QUERY, "flatModel": "Maisonette"})
        self.assertEqual(response.context["startStep"], 6)
        response = self.client.get(reverse("valuation"), {**QUERY, "town": "YISHUN", "postal": "460157"})
        self.assertEqual(response.context["startStep"], 1)
        band.assert_not_called()

    @patch("storefront.views.band", return_value=(548000, 577000, 639000))
    def test_result_shows_price_answers(self, band):
        response = self.client.get(reverse("valuation"), {**QUERY, "ask": "600000"})
        url = "/floor-plan/preview?flatType=4+ROOM&floorAreaSqm=92&leaseCommenceYear=1990&storey=8"
        self.assertEqual(response.context["layout"], url)
        self.assertContains(response, 'src="' + url.replace("&", "&amp;") + '"')
        self.assertContains(response, 'alt="Typical layout: 4-room flat, 92 sqm"')
        self.assertContains(response, "A typical layout for a flat like this. Yours may look different.")
        self.assertNotContains(response, "data-steps")
        self.assertNotContains(response, "<fieldset")
        self.assertContains(response, "<h1>4-room flat in Bedok</h1>", html=True)
        self.assertContains(response, "92 sqm · 8th floor · Lease from 1990 · Your price $600,000")
        edit = "/valuation?edit=1&town=BEDOK&flatType=4+ROOM&area=92&storey=8&lease=1990&ask=600000"
        self.assertEqual(response.context["editUrl"], edit)
        self.assertContains(response, "Edit details</a>")
        content = response.content.decode()
        self.assertLess(content.index("data-fair-evidence"), content.index('class="val-layout"'))
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.get(edit)
        self.assertFalse(response.context["form"].is_bound)
        self.assertNotIn("done", response.context)
        self.assertEqual(response.context["startStep"], 1)
        self.assertContains(response, 'name="area" value="92"')
        self.assertEqual(band.call_count, 1)

    @patch("storefront.views.band", return_value=(548000, 577000, 639000))
    def test_layout_needs_valid_flat(self, band):
        response = self.client.get(reverse("valuation"), {**QUERY, "flatModel": "Model A", "storey": "99"})
        self.assertEqual(
            response.context["layout"],
            "/floor-plan/preview?flatType=4+ROOM&floorAreaSqm=92&flatModel=Model+A&leaseCommenceYear=1990",
        )
        for query in ({**QUERY, "area": "abc"}, {**QUERY, "area": "9"}, {**QUERY, "flatType": ""}):
            response = self.client.get(reverse("valuation"), query)
            self.assertEqual(response.context["layout"], "")
            self.assertNotContains(response, "/floor-plan/preview?")
        band.assert_not_called()

    @patch("storefront.views.band", return_value=(548000, 577000, 639000))
    def test_invalid_flat_model_storey(self, band):
        response = self.client.get(reverse("valuation"), {**QUERY, "flatModel": "Maisonette"})
        self.assertIn("flatModel", response.context["form"].errors)
        response = self.client.get(reverse("valuation"), {**QUERY, "storey": "99"})
        self.assertIn("storey", response.context["form"].errors)
        band.assert_not_called()

    @patch(
        "storefront.views.band",
        side_effect=ValueError("Sorry, the price check isn’t available right now. Please try again later."),
    )
    def test_unavailable_price_model_falls(self, band):
        with patch("storefront.views.usualSales", return_value={}) as typical:
            response = self.client.get(reverse("valuation"), QUERY)
        typical.assert_called_once_with("BEDOK", "4 ROOM")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("est", response.context)
        self.assertTrue(response.context["done"])
        self.assertContains(response, "<h2>We can’t price this flat yet</h2>", html=True)
        self.assertContains(response, "too few recent sales of 4-room flats in Bedok")
        self.assertContains(response, "Edit details</a>")
        self.assertNotContains(response, "data-steps")
        sales = {"lo": 510000, "est": 560000, "hi": 600000, "saleCount": 12}
        with patch("storefront.views.usualSales", return_value=sales):
            response = self.client.get(reverse("valuation"), QUERY)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<h2>Estimated price about $560,000</h2>", html=True)
        self.assertContains(
            response,
            "Based on recent sales only: 8 in 10 4-room flats in Bedok sold between $510,000 and $600,000.",
        )
        self.assertContains(response, "Sorry, we can’t run the price check for your exact flat right now.")
        self.assertNotContains(response, "data-fair-evidence")
        self.assertNotContains(response, 'class="error-summary"')

    @patch("storefront.views.band", side_effect=RuntimeError("broken model"))
    def test_unexpected_price_failure_logged(self, band):
        with self.assertLogs("storefront.views", level="ERROR"):
            response = self.client.get(reverse("valuation"), QUERY)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["done"])
        self.assertContains(response, "Sorry, we can’t run the price check for your exact flat right now.")

    @patch("storefront.views.band", return_value=(548000, 577000, 639000))
    def test_address_lookup_failure_drops(self, band):
        with patch("storefront.address.check", side_effect=OSError("no block data")):
            with self.assertLogs("storefront.views", level="ERROR"):
                response = self.client.get(reverse("valuation"), {**QUERY, "postal": "460157"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["est"], 577000)
            with self.assertLogs("storefront.views", level="ERROR"):
                response = self.client.get(reverse("valuation"), {**QUERY, "town": "", "postal": "460157"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["startStep"], 1)

    @patch("storefront.views.band", return_value=(548000, 577000, 639000))
    def test_recent_lease_shows_minimum(self, band):
        year = timezone.localdate().year - 3
        query = {
            "town": "BEDOK",
            "flatType": "4 ROOM",
            "floorAreaSqm": "92",
            "storey": "8",
            "leaseCommenceYear": year,
        }
        response = self.client.get(reverse("valuation"), query)
        self.assertEqual(response.context["mopEnds"], year + 5)

    @patch("storefront.views.band", return_value=(548000, 577000, 639000))
    def test_valuation_derives_town_postal(self, band):
        response = self.client.get(reverse("valuation"), {**QUERY, "town": "", "postal": "460 157"})
        self.assertEqual(response.context["form"].cleaned_data["town"], "BEDOK")
        response = self.client.get(reverse("valuation"), {**QUERY, "town": "YISHUN", "postal": "460157"})
        self.assertIn("town", response.context["form"].errors)

    @patch("storefront.views.band", return_value=(548000, 577000, 639000))
    def test_valuation_shows_comparable(self, band):
        Sale.objects.create(
            month=date(2026, 12, 1),
            town="BEDOK",
            flatType="4 ROOM",
            block="157",
            street="BEDOK STH AVE 3",
            storeyRange="07 TO 09",
            floorAreaSqm=92,
            flatModel="Model A",
            leaseCommenceYear=1988,
            price=570000,
        )
        response = self.client.get(reverse("valuation"), QUERY)
        for check_text_1 in (
            "Lease from",
            ">1988<",
            "We compare this flat with about 240,000 HDB resale flats sold since 2017",
        ):
            self.assertContains(response, check_text_1)
        self.assertNotContains(response, "same size, floor and age")
        self.assertContains(
            response,
            "When we tested this on 24,542 past sales, about 81 in every 100 sold within our range.",
        )
        self.assertNotContains(response, "In testing, about")


class ModelTests(SimpleTestCase):

    def test_prediction_band_matches(self):
        for median, q10, q90, lowerOffset, upperOffset in (
            (500000, 499000, 501000, 0, 0),
            (500000, 550000, 450000, 8491.9362, 8491.9362),
            (5000, 1000, 9000, 2000, 3000),
            (200000, 50000, 400000, 0, 1000),
            (123456.5, 130000, 140000, 111.5, 222.5),
        ):
            with self.subTest(median=median, q10=q10, q90=q90):
                predictions = {
                    "median": math.log(median),
                    "q10": math.log(q10),
                    "q90": math.log(q90),
                }
                offsets = {"lower": lowerOffset, "upper": upperOffset}
                estimate = round(np.exp(predictions["median"]))
                low = np.exp(min(predictions["q10"], predictions["q90"])) - lowerOffset
                high = np.exp(max(predictions["q10"], predictions["q90"])) + upperOffset
                spread = max(10000, round(estimate * 0.05))
                expected = (
                    max(1, min(round(low), estimate - spread)),
                    estimate,
                    max(round(high), estimate + spread),
                )
                self.assertEqual(valuation.rawBand(predictions, offsets), (low, estimate, high))
                self.assertEqual(valuation.guessBand(predictions, offsets), expected)
                artifact = {
                    "models": {
                        name: SimpleNamespace(predict=lambda row, value=value: [value])
                        for name, value in predictions.items()
                    },
                    "calibration_offsets": offsets,
                }
                with (
                    patch.object(valuation, "makeRow"),
                    patch.object(valuation, "_load", return_value=artifact),
                ):
                    self.assertEqual(valuation.band("BEDOK", "4 ROOM", "Model A", 92, 8, 1990), expected)

    def test_prediction_band_blocks(self):
        for invalid in (float("inf"), float("nan")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                valuation.guessBand(
                    {"median": math.log(500000), "q10": math.log(450000), "q90": math.log(550000)},
                    {"lower": invalid, "upper": 0},
                )

    def test_publication_flag_boundaries(self):
        for asking, reason in (
            (600000, None),
            (600001, "above"),
            (350000, None),
            (349999, "typo"),
            (450000, None),
        ):
            with self.subTest(asking=asking):
                self.assertEqual(flagReason(asking, 500000, 550000, 600000), reason)

    def test_price_verdict_flag_reason(self):
        self.assertEqual(verdict(0, 1, 2, 3)["key"], "none")
        self.assertEqual(verdict(700000, 500000, 550000, 600000)["key"], "above")
        self.assertEqual(verdict(300000, 500000, 550000, 600000)["key"], "typo")
        self.assertEqual(verdict(450000, 500000, 550000, 600000)["text"], "Priced low")
        self.assertEqual(flagReason(550000, 500000, 550000, 600000), None)
        self.assertEqual(flagReason(650000, 500000, 550000, 600000), "above")

    def test_price_band_positions_order(self):
        pos = bandSpots(500000, 550000, 600000, 550000)
        self.assertEqual(pos["est"], pos["pos"])
        self.assertLess(pos["lo"], pos["est"])
        self.assertLess(pos["est"], pos["hi"])

    def test_missing_artifact_makes(self):
        with patch("storefront.valuation.joblib.load", side_effect=OSError("missing")):
            valuation._CACHE = None
            with self.assertRaises(ValueError):
                valuation.band("BEDOK", "4 ROOM", "Model A", 92, 8, 1990)
            self.assertFalse(valuation.modelInfo()["available"])
        valuation._CACHE = None

    def test_valuation_blocks_invalid_lease(self):
        artifact = {"categories": {"town": ["BEDOK"], "flat_type": ["4 ROOM"], "flat_model": ["Model A"]}}
        with patch.object(valuation, "_load", return_value=artifact):
            with self.assertRaises(ValueError):
                valuation.band("BEDOK", "4 ROOM", "Model A", 92, 8, 1900)
            with self.assertRaises(ValueError):
                valuation.band("ASDF", "4 ROOM", "Model A", 92, 8, 1990)

    def test_mocked_predicts_ordered(self):
        artifact = {
            "models": {
                name: Mock(predict=Mock(return_value=[math.log(value)]))
                for name, value in (("q10", 500000), ("median", 550000), ("q90", 600000))
            },
            "calibration_offsets": {"lower": 0, "upper": 0},
            "max_training_month": "2025-01",
            "metrics": {"v5": {"test": {"MAPE": 0.1}}, "band": {"coverage": 0.8}},
        }
        with (
            patch.object(valuation, "makeRow"),
            patch.object(valuation, "_load", return_value=artifact),
        ):
            lo, est, hi = valuation.band(
                "BEDOK", "4 ROOM", "Model A", 92, 8, 1990, postal="460157", block="157"
            )
            self.assertTrue(valuation.modelInfo()["available"])
        self.assertLess(lo, est)
        self.assertLess(est, hi)
        self.assertGreater(est, 200000)


class RuleTests(SimpleTestCase):

    def test_flat_validate_type_model(self):
        self.assertEqual(flatIssues("4 ROOM", "Model A", 92), {})
        self.assertIn("flatModel", flatIssues("2 ROOM", "Maisonette", 45))
        self.assertIn("flatType", flatIssues("9 ROOM", "Model A", 92))
        self.assertIn("usually", sizeHint("4 ROOM", 200))
        self.assertEqual(sizeHint("4 ROOM", 92), "")

    def test_address_check_fills_missing(self):
        record, errors, warnings, fixes = address.check("460157", town=None, block="", lease=1985)
        self.assertEqual(fixes, {"town": "BEDOK", "block": "157"})
        self.assertEqual(errors, {})
        record, errors, warnings, fixes = address.check("460157", town="BEDOK", block="999", lease=1950)
        self.assertEqual(set(errors), {"block", "lease"})
        record, errors, warnings, fixes = address.check("000000")
        self.assertIsNone(record)
        self.assertIn("postal", errors)


class AddressSearchTests(TestCase):

    def results(self, query):
        response = self.client.get(reverse("address_search"), {"q": query})
        self.assertEqual(response.status_code, 200)
        return response.json()["results"]

    def test_postal_prefix_capped_returns(self):
        results = self.results("5602")
        self.assertEqual(len(results), 8)
        self.assertTrue(all((item["postal"].startswith("5602") for item in results)))
        self.assertEqual(
            results[0],
            {
                "postal": "560201",
                "block": "201",
                "label": "Blk 201 Ang Mo Kio Ave 3",
                "town": "ANG MO KIO",
                "townLabel": "Ang Mo Kio",
                "leaseYear": 1977,
            },
        )

    def test_matches_block_street_town(self):
        results = self.results("201 Ang Mo")
        self.assertEqual([item["postal"] for item in results], ["560201"])
        self.assertEqual(len(self.results("jurong east st 21")), 8)

    def test_returns_no_results_empty(self):
        self.assertEqual(self.results(""), [])
        self.assertEqual(self.results("   "), [])
        self.assertEqual(self.results("zzzz street"), [])
        self.assertEqual(self.results("999999"), [])

    def test_blocks_post_requests(self):
        response = self.client.post(reverse("address_search"), {"q": "5602"})
        self.assertEqual(response.status_code, 405)


class FairRangeBoundaryTests(SimpleTestCase):

    @patch(
        "storefront.templatetags.estate_tags.modelInfo",
        return_value={"available": True, "version": "v5.3", "month": "2026-07"},
    )
    def test_seller_source_note_matches(self, info):
        context = {
            "lo": 500000,
            "est": 550000,
            "hi": 600000,
            "flat": SimpleNamespace(modelVersion="older"),
        }
        source = "using prices up to Jul 2026"
        self.assertNotIn(source, render_to_string("sellerpanel/price_bar.html", context))
        self.assertIn(source, render_to_string("sellerpanel/price_bar.html", {**context, "next": True}))
        context["flat"].modelVersion = "v5.3"
        self.assertIn(source, render_to_string("sellerpanel/price_bar.html", context))

    @patch("storefront.templatetags.estate_tags.modelInfo", return_value={"available": False})
    def test_buyer_seller_charts_show(self, info):
        context = {
            "lo": 500125,
            "est": 550000,
            "hi": 600875,
            "asking": 560000,
            "flat": SimpleNamespace(askingPrice=560000, modelVersion="v5.3"),
        }
        for template in (
            "storefront/listing_worth.html",
            "storefront/fair_evidence.html",
            "sellerpanel/price_bar.html",
        ):
            with self.subTest(template=template):
                content = render_to_string(template, context)
                typicalBoundary = (
                    "<div><dt>Estimate</dt><dd>$550,000</dd></div>"
                    if template == "storefront/listing_worth.html"
                    else ""
                )
                self.assertInHTML(
                    '<dl class="fair-boundaries" aria-label="Fair range boundaries"><div><dt>Low</dt><dd>$500,000</dd></div>'
                    + typicalBoundary
                    + "<div><dt>High</dt><dd>$601,000</dd></div></dl>",
                    content,
                )
                self.assertLess(content.index('class="fair-track"'), content.index('class="fair-boundaries"'))
                if template == "sellerpanel/price_bar.html":
                    self.assertIn("Similar flats may sell between $500,000 and $601,000.", content)
                else:
                    self.assertNotIn("Similar flats may sell", content)
                self.assertNotIn("$500,125", content)
                self.assertNotIn("$600,875", content)
                marker = '<details class="evidence-method">'
                if template == "storefront/listing_worth.html":
                    marker = '<details class="fold glance-fold glance-method">'
                disclosure = content.split(marker, 1)[1].split("</details>", 1)[0]
                self.assertIn("How did we get this price?", disclosure.split("</summary>", 1)[0])
                self.assertIn("We compare this flat with about 240,000", disclosure)
                self.assertNotIn("We compare this flat with about 240,000", content.split(marker, 1)[0])
                self.assertIn("not an official HDB valuation.", disclosure)
                self.assertNotIn("widened for safety", disclosure)

    def test_unavailable_estimate_not_show(self):
        for template in (
            "storefront/listing_worth.html",
            "storefront/fair_evidence.html",
            "sellerpanel/price_bar.html",
        ):
            with self.subTest(template=template):
                self.assertNotIn('class="fair-boundaries"', render_to_string(template, {}))


class AnswerCheckTests(TestCase):

    def check(self, **answers):
        return self.client.get(reverse("valuation_check"), answers).json()

    @patch("storefront.views.band", return_value=(548000, 577000, 639000))
    def test_fine_answers_return_nothing(self, band):
        reply = self.check(**QUERY, step="6")
        self.assertEqual(reply, {"errors": {}, "warnings": {}, "fixes": {}})
        band.assert_not_called()

    def test_errors_forms_own_words(self):
        reply = self.check(town="BEDOK", flatType="4 ROOM", area="9", storey="99", lease="1900")
        form = ValuationForm(
            {"town": "BEDOK", "flatType": "4 ROOM", "area": "9", "storey": "99", "lease": "1900"}
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(
            reply["errors"], {name: form.errors[name][0] for name in ("area", "storey", "lease")}
        )
        future = str(timezone.localdate().year + 1)
        self.assertIn("lease", self.check(town="BEDOK", lease=future)["errors"])
        self.assertEqual(self.check(town="BEDOK")["errors"], {})

    def test_unanswered_questions_count_up(self):
        self.assertEqual(self.check(step="0")["errors"], {})
        self.assertEqual(list(self.check(step="1")["errors"]), ["town"])
        reached = self.check(town="BEDOK", flatType="4 ROOM", step="3")["errors"]
        self.assertEqual(reached, {"area": "Enter your flat’s size in sqm, like 92."})

    def test_unusual_size_recent_lease(self):
        year = timezone.localdate().year - 2
        reply = self.check(town="BEDOK", flatType="2 ROOM", area="92", lease=str(year))
        self.assertEqual(reply["errors"], {})
        self.assertEqual(
            reply["warnings"]["area"],
            "2-room flats are usually 32 to 68 sqm. Check your HDB papers or change the flat type.",
        )
        self.assertEqual(
            reply["warnings"]["lease"],
            f"This flat can’t be resold until {year + 5}. You can still see a price.",
        )

    def test_postal_code_in_another(self):
        reply = self.check(town="YISHUN", postal="460157")
        self.assertIn("is Blk 157 Bedok Sth Ave 3 in Bedok, not Yishun", reply["errors"]["town"])
        self.assertEqual(reply["fixes"], {"town": "BEDOK", "townLabel": "Bedok"})
        reply = self.check(postal="460157", step="1")
        self.assertEqual(reply["errors"], {})
        self.assertEqual(reply["fixes"]["town"], "BEDOK")
        self.assertIn("postal", self.check(postal="999999")["errors"])

    def test_flat_model_not_fit(self):
        reply = self.check(town="BEDOK", flatType="4 ROOM", flatModel="Maisonette")
        self.assertIn("4-room flats don’t come in Maisonette", reply["errors"]["flatModel"])

    @patch("storefront.views.band", return_value=(548000, 577000, 639000))
    def test_price_far_outside_likely(self, band):
        reply = self.check(**QUERY, ask="90000")
        self.assertEqual(reply["errors"], {})
        self.assertEqual(
            reply["warnings"]["ask"],
            "Similar flats may sell between $548,000 and $639,000. Please check the price you typed.",
        )
        self.assertNotIn("ask", self.check(**QUERY, ask="600000")["warnings"])
        band.side_effect = ValueError(
            "Sorry, the price check isn’t available right now. Please try again later."
        )
        self.assertEqual(self.check(**QUERY, ask="90000")["warnings"], {})

    def test_get_survives_address_failure(self):
        self.assertEqual(self.client.post(reverse("valuation_check")).status_code, 405)
        with patch("storefront.address.check", side_effect=OSError("no block data")):
            with self.assertLogs("storefront.views", level="ERROR"):
                reply = self.check(town="BEDOK", postal="460157")
        self.assertEqual(reply["errors"], {})

    @patch("storefront.views.band", return_value=(548000, 577000, 639000))
    def test_pass_always_end_in(self, band):
        answers = {**QUERY, "ask": "600000"}
        self.assertEqual(self.check(**answers, step="6")["errors"], {})
        response = self.client.get(reverse("valuation"), answers)
        self.assertEqual(response.context["est"], 577000)
        band.side_effect = ValueError("Sorry, we couldn’t work out a price for this flat.")
        response = self.client.get(reverse("valuation"), answers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["done"])
        self.assertNotContains(response, 'class="error-summary"')
