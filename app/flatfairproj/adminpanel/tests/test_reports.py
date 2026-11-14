import csv
from datetime import timedelta
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from adminpanel.models import AuditLog
from storefront.tests.helpers import makeStaff, makeUser


class AuditExportTests(TestCase):

    def setUp(self):
        self.admin = makeStaff("audit-export@test.com", name="Lim")
        self.other = makeStaff("audit-other@test.com", name="Tan")
        AuditLog.objects.create(
            actor=self.admin,
            action="listing approved",
            target="Listing #1 (Yishun)",
            detail="Price in range",
        )
        AuditLog.objects.create(
            actor=self.other, action="order refunded", target="Order #7", detail="=SUM(A1)"
        )
        AuditLog.objects.create(
            actor=None,
            action="payment checked",
            target="Payment #3",
            createdAt=timezone.now() - timedelta(days=40),
        )
        self.client.force_login(self.admin)

    def rows(self, **params):
        response = self.client.get(reverse("admin_export_audit"), params)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("flatfair-audit-log-", response["Content-Disposition"])
        return list(csv.reader(StringIO(response.content.decode())))

    def test_page_links_filters(self):
        page = self.client.get(reverse("admin_audit"), {"action": "order refunded"})
        self.assertContains(page, reverse("admin_export_audit") + "?action=order+refunded")
        self.assertContains(page, "Export these (CSV)")

    def test_entry_readable_columns(self):
        rows = self.rows()
        self.assertEqual(rows[0], ["When", "Staff", "Staff email", "Action", "Record", "Details"])
        self.assertEqual(len(rows), 4)
        system = [row for row in rows if row[4] == "Payment #3"][0]
        self.assertEqual(system[1:3], ["System", ""])
        refund = [row for row in rows if row[4] == "Order #7"][0]
        self.assertEqual(refund[2], "audit-other@test.com")
        self.assertEqual(refund[5], "'=SUM(A1)")

    def test_filters_match_the_page(self):
        self.assertEqual([row[4] for row in self.rows(action="order refunded")[1:]], ["Order #7"])
        self.assertEqual([row[4] for row in self.rows(actor="system")[1:]], ["Payment #3"])
        self.assertEqual(
            [row[4] for row in self.rows(actor=self.admin.pk)[1:] if row[4] != "Audit log"],
            ["Listing #1 (Yishun)"],
        )
        recent = (timezone.localdate() - timedelta(days=7)).isoformat()
        self.assertNotIn("Payment #3", [row[4] for row in self.rows(**{"from": recent})[1:]])

    def test_itself_recorded(self):
        self.rows(action="order refunded")
        entry = AuditLog.objects.filter(action="export", target="Audit log").get()
        self.assertEqual(entry.actor, self.admin)
        self.assertIn("1 row", entry.detail)
        self.assertIn("filtered by action", entry.detail)

    def test_only_staff_can_export(self):
        self.client.force_login(makeUser("audit-buyer@test.com"))
        self.assertEqual(self.client.get(reverse("admin_export_audit")).status_code, 403)
        self.client.logout()
        self.assertNotEqual(self.client.get(reverse("admin_export_audit")).status_code, 200)


class PriceBandTests(TestCase):

    def setUp(self):
        self.client.force_login(makeStaff("price-band@test.com"))
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.reportPath = Path(directory.name) / "price_band_eval.json"
        pathPatch = patch("adminpanel.views.priceBandEvalPath", self.reportPath)
        pathPatch.start()
        self.addCleanup(pathPatch.stop)
        modelPatch = patch("adminpanel.views.modelInfo", return_value={"available": False, "version": "v5.3"})
        modelPatch.start()
        self.addCleanup(modelPatch.stop)
        summary = {
            "count": 100,
            "servedCoverage": 0.8,
            "rawCoverage": 0.72,
            "meanWidthDollars": 100000,
            "meanWidthPercent": 15,
            "mapePercent": 4,
            "medianAbsolutePercentError": 3,
            "flaggedShare": 0.1,
            "aboveShare": 0.09,
            "typoShare": 0.01,
        }
        self.report = {
            "schemaVersion": 1,
            "evaluationPeriod": {"start": "2025-11", "end": "2026-10"},
            "overall": summary,
            "byTown": {
                town: {**summary, "count": 10, "servedCoverage": coverage}
                for town, coverage in (
                    ("BEDOK", 0.75),
                    ("BISHAN", 0.55),
                    ("BUKIT TIMAH", 0.6),
                    ("CENTRAL AREA", 0.5),
                    ("CLEMENTI", 0.65),
                    ("GEYLANG", 0.7),
                )
            },
        }

    def test_page_shows_saved_evaluation(self):
        self.reportPath.write_text(json.dumps(self.report), encoding="utf-8")
        response = self.client.get(reverse("admin_price_model"))
        for check_text_1 in (
            "How often sales fell inside our range (Nov 2025–Oct 2026)",
            "80.0%",
            "72.0%",
            "$100,000",
            "15.0%",
            "4.00%",
            "3.00%",
            "10.0%",
            "9.0%",
            "1.00%",
            "possible holds, rather than staff mistakes we observed",
        ):
            self.assertContains(response, check_text_1)
        self.assertEqual(
            [row["town"] for row in response.context["bandEval"]["worstTowns"]],
            ["CENTRAL AREA", "BISHAN", "BUKIT TIMAH", "CLEMENTI", "GEYLANG"],
        )

    def test_missing_evaluation_hides_block(self):
        response = self.client.get(reverse("admin_price_model"))
        self.assertIsNone(response.context["bandEval"])
        self.assertNotContains(response, "served-band-evaluation")

    def test_invalid_evaluation_hides_block(self):
        badSummaries = [
            {**self.report["overall"], "servedCoverage": value}
            for value in (None, "0.8", True, -0.1, 1.1, float("nan"), float("inf"))
        ]
        invalid = (
            ["{", "[]", "null", "{}"]
            + [json.dumps({**self.report, "overall": summary}) for summary in badSummaries]
            + [
                json.dumps({**self.report, "byTown": {}}),
                json.dumps({**self.report, "byTown": {"BEDOK": None}}),
                json.dumps({**self.report, "evaluationPeriod": {"start": "2026-01", "end": "2026-10"}}),
            ]
        )
        for content in invalid:
            with self.subTest(content=content):
                self.reportPath.write_text(content, encoding="utf-8")
                response = self.client.get(reverse("admin_price_model"))
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.context["bandEval"])
                self.assertNotContains(response, "served-band-evaluation")
        self.reportPath.write_bytes(b"\xff")
        self.assertNotContains(self.client.get(reverse("admin_price_model")), "served-band-evaluation")
