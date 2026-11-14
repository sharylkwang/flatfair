import csv
from datetime import timedelta
from decimal import Decimal
import io
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import urlencode

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
import requests

from adminpanel.models import AuditLog, Ticket, TicketMessage
from adminpanel.reports import csvFile
from sellerpanel.tests.helpers import stripe_reply
from storefront.models import ActivityNotice, Order, OrderItem, Payment, Refund
from storefront.tests.helpers import jpeg, makeListing, makeOrder, makeProduct, makeStaff, makeUser


class OrderTests(TestCase):

    def setUp(self):
        self.admin = makeStaff("admin@test.com")
        self.seller = makeUser("asdf@test.com", "seller", name="Tan")
        self.flat = makeListing(self.seller)
        self.addon = makeProduct("Floor plan", "addon", 40, None)
        self.package = makeProduct("Boost 30 days")
        self.client.force_login(self.admin)

    def status(self, order, value, back="", **extra):
        return self.client.post(
            reverse("admin_order_status", args=[order.pk]), {"status": value, "back": back, **extra}
        )

    def test_type_filter_separates_services(self):
        service = makeOrder(self.seller, self.addon, listing=self.flat)
        package = makeOrder(self.seller, self.package, listing=self.flat)
        response = self.client.get(reverse("admin_orders"), {"status": ""})
        self.assertEqual(len(response.context["orders"]), 2)
        found = self.client.get(reverse("admin_orders"), {"type": "service"}).context["orders"]
        self.assertEqual([order.pk for order in found], [service.pk])
        found = self.client.get(reverse("admin_orders"), {"type": "package", "status": ""}).context["orders"]
        self.assertEqual([order.pk for order in found], [package.pk])

    def test_date_filters_handle_invalid(self):
        order = makeOrder(self.seller, self.addon)
        Order.objects.filter(pk=order.pk).update(createdAt=timezone.now() - timedelta(days=10))
        today = timezone.localdate().isoformat()
        self.assertEqual(len(self.client.get(reverse("admin_orders"), {"from": today}).context["orders"]), 0)
        response = self.client.get(reverse("admin_orders"), {"from": "2026-02-31"})
        self.assertIn("did not work", response.context["dateProblem"])
        response = self.client.get(reverse("admin_orders"), {"from": "2026-10-25", "to": "2026-01-01"})
        self.assertIn("start date", response.context["dateProblem"])
        self.assertEqual(len(response.context["orders"]), 1)

    def test_service_completion_requires(self):
        order = makeOrder(self.seller, self.addon)
        self.status(order, "completed")
        order.refresh_from_db()
        self.assertEqual(order.status, "paid")
        self.status(order, "processing")
        item = order.items.get()
        response = self.status(order, "completed")
        self.assertEqual(response.url, reverse("admin_order", args=[order.pk]) + "#delivery")
        order.refresh_from_db()
        self.assertEqual(order.status, "processing")
        self.status(order, "completed", **{f"note-{item.pk}": "Plan attached"})
        order.refresh_from_db()
        self.assertEqual(order.status, "completed")
        self.assertEqual(order.items.get().deliveryNote, "Plan attached")
        self.status(order, "refunded")
        order.refresh_from_db()
        self.assertEqual(order.status, "completed")
        self.assertEqual(AuditLog.objects.filter(action="order status").count(), 2)

    def test_completed_packages_blocks(self):
        order = makeOrder(self.seller, self.package, "completed", listing=self.flat)
        self.status(order, "refunded")
        order.refresh_from_db()
        self.assertEqual(order.status, "completed")
        response = self.client.get(reverse("admin_orders"), {"status": ""})
        self.assertEqual(response.context["orders"][0].refundAsk, "")
        self.assertContains(response, "Completed")

    def test_status_redirect_blocks_external(self):
        order = makeOrder(self.seller, self.addon)
        response = self.status(order, "processing", back="https://evil.example/")
        self.assertRedirects(response, reverse("admin_orders"), fetch_redirect_response=False)
        note = {f"note-{order.items.get().pk}": "asdf"}
        response = self.status(order, "completed", back="/staff/orders?status=paid", **note)
        self.assertEqual(response.url, "/staff/orders?status=paid")

    def test_service_detail_shows_work(self):
        order = makeOrder(self.seller, self.addon)
        response = self.client.get(reverse("admin_order", args=[order.pk]))
        self.assertEqual(
            response.context["order"].workActions, [{"status": "processing", "label": "Start work"}]
        )
        self.assertEqual(response.context["order"].refundAsk, "Refund $40.00 to Tan for Floor plan?")
        self.assertEqual(
            response.context["order"].guide[0]["text"],
            "You email the seller for their HDB layout so the plan can be drawn.",
        )
        self.status(order, "processing")
        history = self.client.get(reverse("admin_order", args=[order.pk])).context["history"]
        self.assertEqual([row["what"] for row in history], ["Paid at checkout", "Paid → Processing"])
        self.status(order, "refunded")
        order.refresh_from_db()
        self.assertEqual(order.status, "processing")

    def test_package_no_work_actions(self):
        order = makeOrder(self.seller, makeProduct("Top spot", price=299, days=None), listing=self.flat)
        response = self.client.get(reverse("admin_order", args=[order.pk]))
        self.assertEqual(
            (response.context["order"].refundAsk, response.context["order"].workActions), ("", [])
        )
        self.assertRedirects(self.client.get(reverse("admin_order", args=[999])), reverse("admin_orders"))

    def test_delivery_upload_checks_type(self):
        order = makeOrder(self.seller, self.addon, listing=self.flat)
        item = order.items.get()
        self.status(order, "processing")
        bad = SimpleUploadedFile("plan.exe", b"asdf")
        self.status(order, "completed", **{f"file-{item.pk}": bad})
        order.refresh_from_db()
        self.assertEqual(order.status, "processing")
        plan = SimpleUploadedFile("plan.pdf", b"%PDF-1.4 asdf", content_type="application/pdf")
        self.status(order, "completed", **{f"file-{item.pk}": plan})
        item.refresh_from_db()
        self.assertEqual(item.deliveryFileName, "plan.pdf")
        url = reverse("delivery_file", args=[item.pk])
        self.client.force_login(self.seller)
        self.assertNotContains(self.client.get(reverse("my_orders")), url)
        self.assertContains(self.client.get(reverse("receipt", args=[order.pk])), url)
        download = self.client.get(url)
        self.assertIn("attachment;", download["Content-Disposition"])
        self.assertEqual(b"".join(download.streaming_content), b"%PDF-1.4 asdf")
        self.client.force_login(makeUser("qwer@test.com", "seller"))
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_delivery_blocks_mislabelled(self):
        order = makeOrder(self.seller, self.addon, listing=self.flat)
        item = order.items.get()
        self.status(order, "processing")
        cases = [
            ("plan.pdf", b"<html>fake</html>", "application/pdf"),
            ("photo.jpg", b"\xff\xd8\xffbroken", "image/jpeg"),
            ("photo.png", jpeg().read(), "image/png"),
            ("photos.zip", b"PKbroken", "application/zip"),
        ]
        for name, data, contentType in cases:
            with self.subTest(name=name):
                upload = SimpleUploadedFile(name, data, content_type=contentType)
                self.status(order, "completed", **{f"file-{item.pk}": upload})
                order.refresh_from_db()
                item.refresh_from_db()
                self.assertEqual(order.status, "processing")
                self.assertFalse(item.deliveryFile)
                response = self.client.get(reverse("admin_order", args=[order.pk]))
                self.assertContains(response, "does not match")

    def test_service_delivery_notifies(self):
        order = makeOrder(self.seller, self.addon, listing=self.flat)
        item = order.items.get()
        self.status(order, "processing")
        self.status(order, "completed", **{f"note-{item.pk}": "Plan attached"})
        notice = ActivityNotice.objects.get(user=self.seller)
        self.assertEqual(notice.kind, "service_delivered")
        self.assertEqual(notice.text, "Your Floor plan is ready. Open the receipt to see the file or note.")
        self.assertEqual(notice.url, reverse("receipt", args=[order.pk]))

    def test_missing_delivery_error_shown(self):
        order = makeOrder(self.seller, self.addon, listing=self.flat)
        self.status(order, "processing")
        response = self.client.get(self.status(order, "completed").url)
        self.assertContains(response, 'id="delivery-error"')
        self.assertContains(response, "Add a note or a file for Floor plan")
        self.assertNotContains(
            self.client.get(reverse("admin_order", args=[order.pk])), 'id="delivery-error"'
        )

    def test_refund_notifies_seller_resolves(self):
        order = makeOrder(self.seller, self.addon, listing=self.flat)
        ticket = Ticket.objects.create(
            customer=self.seller,
            email="asdf@test.com",
            topic="refund",
            message="Please",
            order=order,
        )
        other = Ticket.objects.create(
            customer=self.seller,
            email="asdf@test.com",
            topic="orders",
            message="When?",
            order=order,
        )
        self.status(order, "refunded")
        notice = ActivityNotice.objects.get(user=self.seller)
        self.assertEqual((notice.kind, notice.get_kind_display()), ("order_refunded", "Order refunded"))
        self.assertEqual(notice.url, reverse("my_orders") + f"#order-{order.pk}")
        ticket.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((ticket.status, other.status), ("resolved", "open"))
        reply = TicketMessage.objects.get(ticket=ticket)
        self.assertTrue(reply.fromStaff)
        self.assertTrue(reply.body.startswith("Refunded on "))

    def test_refund_resolution_logs_actor(self):
        order = makeOrder(self.seller, self.addon)
        ticket = Ticket.objects.create(
            customer=self.seller,
            email=self.seller.email,
            topic="refund",
            message="Please refund",
            order=order,
        )
        resolved = Ticket.objects.create(
            customer=self.seller,
            email=self.seller.email,
            topic="refund",
            message="Earlier request",
            order=order,
            status="resolved",
            resolvedAt=timezone.now(),
        )
        self.status(order, "refunded")
        entry = AuditLog.objects.get(action="ticket resolved")
        self.assertEqual(entry.actor, self.admin)
        self.assertEqual(entry.target, f"Ticket #{ticket.pk}")
        self.assertIn(str(order.pk), entry.detail)
        self.assertFalse(TicketMessage.objects.filter(ticket=resolved).exists())

    def test_refund_not_resolve_concurrently(self):
        order = makeOrder(self.seller, self.addon)
        ticket = Ticket.objects.create(
            customer=self.seller,
            email=self.seller.email,
            topic="refund",
            message="Please refund",
            order=order,
        )
        createReplies = TicketMessage.objects.bulk_create

        def addTicket(replies):
            Ticket.objects.create(
                customer=self.seller,
                email=self.seller.email,
                topic="refund",
                message="Another question",
                order=order,
            )
            return createReplies(replies)

        with patch.object(TicketMessage.objects, "bulk_create", side_effect=addTicket):
            self.status(order, "refunded")
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, "resolved")
        lateTicket = Ticket.objects.exclude(pk=ticket.pk).get()
        self.assertEqual(lateTicket.status, "open")
        self.assertFalse(lateTicket.replies.exists())
        self.assertFalse(
            AuditLog.objects.filter(action="ticket resolved", target=f"Ticket #{lateTicket.pk}").exists()
        )


class ExportTests(TestCase):

    def setUp(self):
        self.admin = makeStaff("admin@test.com")
        self.seller = makeUser("asdf@test.com", "seller", name="=cmd")
        self.flat = makeListing(self.seller)
        makeOrder(self.seller, makeProduct("Floor plan", "addon", 40, None), status="completed")
        self.client.force_login(self.admin)

    def rows(self, response):
        return list(csv.reader(io.StringIO(response.content.decode())))

    def test_order_escapes_formulas_audits(self):
        response = self.client.get(reverse("admin_export_orders"), {"status": "completed"})
        self.assertEqual(response["Content-Type"], "text/csv")
        rows = self.rows(response)
        self.assertEqual(rows[0][:3], ["Order", "Date", "Seller"])
        self.assertEqual(rows[1][2], "'=cmd")
        entry = AuditLog.objects.get(action="export")
        self.assertEqual((entry.target, entry.detail), ("orders", "1 row, filtered by status completed"))

    def test_customer_listing_include(self):
        rows = self.rows(self.client.get(reverse("admin_export_customers"), {"q": "asdf"}))
        self.assertEqual(rows[1][2], "asdf@test.com")
        self.assertEqual(AuditLog.objects.get().detail, "1 row, filtered by a search")
        rows = self.rows(self.client.get(reverse("admin_export_listings")))
        self.assertEqual(rows[1][2], "YISHUN")
        self.assertEqual(len(rows[0]), 14)

    def test_listing_reuses_page_filter(self):
        self.flat.askingPrice = 700000
        self.flat.save()
        other = makeUser("other@test.com", "seller")
        makeListing(other, askingPrice=700000)
        makeListing(self.seller, town="BEDOK", status="hidden", block="9", postalCode="460009")
        filters = (
            {"status": "live"},
            {"town": "BEDOK"},
            {"above": "1"},
            {"q": "blk 123"},
            {"q": "760"},
            {"seller": self.seller.pk},
            {
                "status": "live",
                "town": "YISHUN",
                "above": "1",
                "q": "760",
                "seller": self.seller.pk,
            },
        )
        for params in filters:
            with self.subTest(params=params):
                page = self.client.get(reverse("admin_listings"), params)
                rows = self.rows(self.client.get(reverse("admin_export_listings"), params))
                self.assertEqual(
                    [int(row[0]) for row in rows[1:]], [flat.pk for flat in page.context["rows"]]
                )
        self.assertEqual([int(row[0]) for row in rows[1:]], [self.flat.pk])
        detail = AuditLog.objects.filter(action="export").latest("pk").detail
        for value in ("town YISHUN", "above 1", f"seller {self.seller.pk}"):
            self.assertIn(value, detail)

    def test_listing_link_keeps_filters(self):
        params = {
            "status": "live",
            "town": "YISHUN",
            "above": "1",
            "q": "blk 123",
            "seller": self.seller.pk,
        }
        response = self.client.get(reverse("admin_listings"), {**params, "page": "2"})
        exportUrl = reverse("admin_export_listings") + "?" + urlencode(params)
        self.assertContains(response, f'href="{escape(exportUrl)}"')

    def test_order_matches_status_type(self):
        makeOrder(self.seller, makeProduct(), status="completed")
        old = makeOrder(self.seller, makeProduct("Old service", "addon"), status="completed")
        Order.objects.filter(pk=old.pk).update(createdAt=timezone.now() - timedelta(days=5))
        params = {
            "status": "completed",
            "type": "service",
            "from": timezone.localdate().isoformat(),
            "to": timezone.localdate().isoformat(),
        }
        page = self.client.get(reverse("admin_orders"), params)
        rows = self.rows(self.client.get(reverse("admin_export_orders"), params))
        self.assertEqual([int(row[0]) for row in rows[1:]], [order.pk for order in page.context["orders"]])
        self.assertEqual(len(rows), 2)
        exportUrl = reverse("admin_export_orders") + "?" + urlencode(params)
        self.assertContains(page, f'href="{escape(exportUrl)}"')

    def test_customer_matches_search_role(self):
        other = makeUser("other@test.com", "seller")
        other.is_active = False
        other.save()
        makeUser("buyer@test.com")
        params = {"q": "asdf", "role": "seller", "state": "active"}
        page = self.client.get(reverse("admin_customers"), params)
        rows = self.rows(self.client.get(reverse("admin_export_customers"), params))
        self.assertEqual([int(row[0]) for row in rows[1:]], [person.pk for person in page.context["people"]])
        self.assertEqual([int(row[0]) for row in rows[1:]], [self.seller.pk])
        exportUrl = reverse("admin_export_customers") + "?" + urlencode(params)
        self.assertContains(page, f'href="{escape(exportUrl)}"')

    def test_csv_output_escapes_dangerous(self):
        values = ["=SUM(A1)", "  +cmd", "\ttext", "\x00@cmd", "-cmd", 12, "ordinary"]
        row = self.rows(csvFile("test", ["Header"], [values]))[1]
        self.assertEqual(row, ["'=SUM(A1)", "'  +cmd", "'\ttext", "'\x00@cmd", "'-cmd", "12", "ordinary"])


class DeliveryCleanupTests(TestCase):

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        setting = override_settings(MEDIA_ROOT=self.root)
        setting.enable()
        self.addCleanup(setting.disable)
        self.staff = makeStaff("deliveryadmin@test.com")
        self.seller = makeUser("deliveryseller@test.com", "seller")
        self.order = makeOrder(self.seller, makeProduct("Floor plan", "addon", 40, None), status="processing")
        self.item = self.order.items.get()
        self.item.deliveryFile.save("original.pdf", ContentFile(b"%PDF-1.4 original"))
        self.original = self.item.deliveryFile.name
        self.client.force_login(self.staff)

    def complete(self):
        upload = SimpleUploadedFile(
            "replacement.pdf", b"%PDF-1.4 replacement", content_type="application/pdf"
        )
        return self.client.post(
            reverse("admin_order_status", args=[self.order.pk]),
            {"status": "completed", f"file-{self.item.pk}": upload},
        )

    def files(self):
        return sorted((str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file()))

    def test_failures_writing_remove_new(self):
        for target in (
            "storefront.models.OrderItem.save",
            "storefront.models.Order.save",
            "adminpanel.orders.audit",
            "adminpanel.orders.sentNote",
        ):
            with self.subTest(target=target):
                with patch(target, side_effect=RuntimeError("forced failure")):
                    with self.assertRaisesMessage(RuntimeError, "forced failure"):
                        self.complete()
                self.assertEqual(self.files(), [self.original])
                self.item.refresh_from_db()
                self.order.refresh_from_db()
                self.assertEqual(self.item.deliveryFile.name, self.original)
                self.assertEqual(self.order.status, "processing")
                self.assertFalse(AuditLog.objects.filter(action="order status").exists())
                self.assertFalse(ActivityNotice.objects.filter(kind="service_delivered").exists())

    def test_replacement_deletes_old_file(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.complete()
            self.assertEqual(response.status_code, 302)
            self.assertTrue((self.root / self.original).exists())
            self.item.refresh_from_db()
            self.assertNotEqual(self.item.deliveryFile.name, self.original)
            self.assertTrue((self.root / self.item.deliveryFile.name).exists())
        self.assertEqual(self.files(), [self.item.deliveryFile.name])

    def test_second_item_failure_removes(self):
        second = OrderItem.objects.create(
            order=self.order, product=self.item.product, name="Second plan", kind="addon", price=40
        )
        originalSave = OrderItem.save

        def save(item, *args, **kwargs):
            if item.pk == second.pk:
                raise RuntimeError("second item failed")
            return originalSave(item, *args, **kwargs)

        uploads = {
            f"file-{item.pk}": SimpleUploadedFile(
                "plan.pdf", b"%PDF-1.4 plan", content_type="application/pdf"
            )
            for item in (self.item, second)
        }
        with patch.object(OrderItem, "save", save):
            with self.assertRaisesMessage(RuntimeError, "second item failed"):
                self.client.post(
                    reverse("admin_order_status", args=[self.order.pk]),
                    {"status": "completed", **uploads},
                )
        self.assertEqual(self.files(), [self.original])

    def test_old_file_deletion_failure(self):
        with patch.object(
            self.item.deliveryFile.storage, "delete", side_effect=OSError("storage unavailable")
        ):
            with self.assertLogs("django.test", level="ERROR"):
                with self.captureOnCommitCallbacks(execute=True):
                    self.complete()
        self.item.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "completed")
        self.assertTrue((self.root / self.item.deliveryFile.name).exists())


@override_settings(STRIPE_SECRET_KEY="sk_test_refunds")
class RefundTests(TestCase):

    def setUp(self):
        self.staff = makeStaff("refundstaff@test.com")
        self.seller = makeUser("refundseller@test.com", "seller")
        self.product = makeProduct("Floor plan", "addon", 40, None)
        self.order = makeOrder(self.seller, self.product)
        self.payment = Payment.objects.create(
            seller=self.seller,
            method="stripe",
            paymentIntentId="pi_test_refund",
            providerSessionId="cs_test_refund",
            amount=self.order.total,
            checkoutToken="refund-test",
            status="paid",
            paidAt=timezone.now(),
        )
        self.order.payment = self.payment
        self.order.save(update_fields=["payment"])
        self.client.force_login(self.staff)

    def reply(self, **changes):
        data = {
            "id": "re_test_refund",
            "payment_intent": "pi_test_refund",
            "amount": 4000,
            "currency": "sgd",
            "status": "succeeded",
        }
        data.update(changes)
        return stripe_reply(data)

    def refund(self, **kwargs):
        with patch("sellerpanel.stripepay.requests.post", **kwargs) as post:
            response = self.client.post(
                reverse("admin_order_status", args=[self.order.pk]),
                {"status": "refunded"},
                follow=True,
            )
        return (response, post)

    def test_confirmed_stripe_records(self):
        ticket = Ticket.objects.create(
            customer=self.seller,
            email=self.seller.email,
            topic="refund",
            message="Please refund",
            order=self.order,
        )
        response, post = self.refund(return_value=self.reply())
        refund = Refund.objects.get()
        self.order.refresh_from_db()
        self.payment.refresh_from_db()
        ticket.refresh_from_db()
        self.assertEqual(
            (self.order.status, self.payment.status, refund.status),
            ("refunded", "refunded", "succeeded"),
        )
        self.assertEqual(
            (refund.providerRefundId, refund.amount, refund.staff),
            ("re_test_refund", Decimal("40.00"), self.staff),
        )
        self.assertEqual(post.call_args.args[0], "https://api.stripe.com/v1/refunds")
        self.assertEqual(post.call_args.kwargs["data"], {"payment_intent": "pi_test_refund", "amount": 4000})
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer sk_test_refunds")
        self.assertEqual(post.call_args.kwargs["timeout"], 10)
        self.assertEqual(ticket.status, "resolved")
        self.assertEqual(TicketMessage.objects.get().author, self.staff)
        self.assertEqual(ActivityNotice.objects.get().kind, "order_refunded")
        self.assertTrue(AuditLog.objects.filter(action="order status", actor=self.staff).exists())
        self.assertContains(response, "refunded. The seller was told.")
        _, repeat = self.refund(return_value=self.reply())
        repeat.assert_not_called()
        self.assertEqual(Refund.objects.count(), 1)
        self.assertEqual(ActivityNotice.objects.count(), 1)

    def test_service_marks_split_payment(self):
        package = makeOrder(self.seller, makeProduct(), "completed")
        package.payment = self.payment
        package.save(update_fields=["payment"])
        self.payment.amount += package.total
        self.payment.save(update_fields=["amount"])
        self.refund(return_value=self.reply())
        self.payment.refresh_from_db()
        package.refresh_from_db()
        self.assertEqual((self.payment.status, package.status), ("partly_refunded", "completed"))

    def test_timeout_retains_pending_retries(self):
        response, first = self.refund(side_effect=requests.Timeout)
        refund = Refund.objects.get()
        self.order.refresh_from_db()
        self.assertEqual((refund.status, self.order.status), ("pending", "paid"))
        self.assertFalse(ActivityNotice.objects.exists())
        self.assertContains(response, "Stripe has not confirmed the refund")
        self.client.post(reverse("admin_order_status", args=[self.order.pk]), {"status": "processing"})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "paid")
        _, second = self.refund(return_value=self.reply())
        self.assertEqual(
            first.call_args.kwargs["headers"]["Idempotency-Key"],
            second.call_args.kwargs["headers"]["Idempotency-Key"],
        )
        self.assertEqual(Refund.objects.count(), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "refunded")

    def test_pending_provider_fetched(self):
        self.refund(return_value=self.reply(status="pending"))
        self.order.refresh_from_db()
        self.assertEqual((self.order.status, Refund.objects.get().status), ("paid", "pending"))
        with patch("sellerpanel.stripepay.requests.get", return_value=self.reply()) as get:
            _, post = self.refund()
        post.assert_not_called()
        self.assertEqual(get.call_args.args[0], "https://api.stripe.com/v1/refunds/re_test_refund")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "refunded")

    def test_failed_provider_recorded_new(self):
        response, first = self.refund(return_value=self.reply(status="failed"))
        self.order.refresh_from_db()
        self.assertEqual((self.order.status, Refund.objects.get().status), ("paid", "failed"))
        self.assertContains(response, "Stripe has not confirmed the refund")
        _, second = self.refund(return_value=self.reply(id="re_second"))
        self.assertEqual(
            list(Refund.objects.order_by("pk").values_list("status", flat=True)),
            ["failed", "succeeded"],
        )
        self.assertNotEqual(
            first.call_args.kwargs["headers"]["Idempotency-Key"],
            second.call_args.kwargs["headers"]["Idempotency-Key"],
        )

    def test_rejected_records_failure(self):
        reply = stripe_reply({"error": {"message": "Rejected"}})
        reply.status_code = 400
        self.refund(return_value=reply)
        self.order.refresh_from_db()
        self.assertEqual((self.order.status, Refund.objects.get().status), ("paid", "failed"))
        self.assertFalse(ActivityNotice.objects.exists())

    def test_invalid_provider_responses(self):
        for reply in (
            stripe_reply([]),
            stripe_reply(None),
            self.reply(amount=1),
            self.reply(payment_intent="pi_other"),
            self.reply(currency="usd"),
            self.reply(id=[]),
            self.reply(status="canceled"),
        ):
            with self.subTest(reply=reply.json.return_value):
                self.refund(return_value=reply)
                self.order.refresh_from_db()
                self.assertEqual(self.order.status, "paid")
        self.assertFalse(ActivityNotice.objects.exists())

    @override_settings(STRIPE_SECRET_KEY="sk_live_forbidden")
    def test_live_key_never_calls(self):
        _, post = self.refund()
        post.assert_not_called()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "paid")

    def test_demo_local_updates_payment(self):
        self.payment.method = "demo"
        self.payment.save(update_fields=["method"])
        _, post = self.refund()
        post.assert_not_called()
        self.payment.refresh_from_db()
        self.assertEqual((self.payment.status, Refund.objects.get().status), ("refunded", "succeeded"))
        self.assertContains(
            self.client.get(reverse("admin_order", args=[self.order.pk])), "Demo card · Refunded"
        )
        self.client.force_login(self.seller)
        self.assertContains(self.client.get(reverse("receipt", args=[self.order.pk])), "Demo card · Refunded")

    def test_packages_started_services_never(self):
        for product, status in ((self.product, "processing"), (makeProduct(), "paid")):
            with self.subTest(status=status):
                self.order.status = status
                self.order.save(update_fields=["status"])
                self.order.items.update(kind=product.kind)
                _, post = self.refund()
                post.assert_not_called()
        self.assertFalse(Refund.objects.exists())


@override_settings(STRIPE_SECRET_KEY="sk_test_transactions")
class RefundTransactionTests(TransactionTestCase):

    def test_http_runs_pending_record(self):
        staff = makeStaff("transactionstaff@test.com")
        seller = makeUser("transactionseller@test.com", "seller")
        order = makeOrder(seller, makeProduct("Floor plan", "addon", 40, None))
        order.payment = Payment.objects.create(
            seller=seller,
            method="stripe",
            amount=order.total,
            checkoutToken="transaction-refund",
            paymentIntentId="pi_transaction",
            status="paid",
        )
        order.save(update_fields=["payment"])
        self.client.force_login(staff)

        def refund(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(Refund.objects.get().status, "pending")
            self.assertEqual(Order.objects.get(pk=order.pk).status, "paid")
            return stripe_reply(
                {
                    "id": "re_transaction",
                    "payment_intent": "pi_transaction",
                    "amount": 4000,
                    "currency": "sgd",
                    "status": "succeeded",
                }
            )

        with patch("sellerpanel.stripepay.requests.post", side_effect=refund):
            self.client.post(reverse("admin_order_status", args=[order.pk]), {"status": "refunded"})
        order.refresh_from_db()
        self.assertEqual(order.status, "refunded")
