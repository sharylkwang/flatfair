from datetime import timedelta
from io import StringIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.cache import caches
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import EmailCheck
from adminpanel.models import AuditLog, Decision, DocumentRequest, Ticket
from sellerpanel.tests.helpers import CompletedPhotoQueueMixin
from storefront.listing_rules import (
    AUTO_APPROVE_PRICE_GAP,
    AUTO_COMPLETE_DAYS,
    AUTO_COMPLETE_KEY,
    passPrice,
    autoClose,
    closeOrder,
    heldByCost,
)
from storefront.models import Listing, ListingPhoto, OrderItem
from storefront.tests.helpers import (
    closeResponse,
    CLEAN,
    FLAT,
    ROOMS,
    jpeg,
    makeListing,
    makeOrder,
    makeProduct,
    makeStaff,
    makeUser,
)


class PriceMonitoringTests(TestCase):

    def test_automatic_approvals_separate(self):
        staff = makeStaff("monitor@test.com")
        self.client.force_login(staff)
        flat = makeListing(makeUser("monitor-seller@test.com", "seller"))
        for actor, decision, price in (
            (staff, "approved", 700000),
            (staff, "rejected", 700000),
            (None, "approved", 630000),
            (None, "rejected", 700000),
        ):
            Decision.objects.create(
                listing=flat,
                staff=actor,
                decision=decision,
                askingPrice=price,
                bandLow=500000,
                estimatedPrice=550000,
                bandHigh=600000,
            )
        response = self.client.get(reverse("admin_price_model"))
        self.assertEqual(response.context["decisionCount"], 2)
        self.assertEqual(response.context["overrideRate"], 50)
        self.assertEqual(response.context["approvedCount"], 1)
        self.assertEqual(response.context["rejectedCount"], 1)
        self.assertEqual(response.context["automaticCount"], 1)
        self.assertContains(response, "Approved automatically (within 5%)")
        Decision.objects.filter(staff=staff).delete()
        response = self.client.get(reverse("admin_price_model"))
        self.assertIsNone(response.context["overrideRate"])
        self.assertEqual(response.context["decisionCount"], 0)


class BulkApprovalTests(TestCase):

    def setUp(self):
        self.staff = makeStaff("admin@test.com")
        self.client.force_login(self.staff)

    def flat(self, number):
        seller = makeUser(f"seller{number}@test.com", "seller")
        flat = makeListing(seller, status="review", askingPrice=700000)
        ListingPhoto.objects.create(
            listing=flat,
            room="kitchen",
            image="room.jpg",
            modelVersion=CLEAN["modelVersion"],
            aiIrrelevant=0.01,
            aiGenerated=0.01,
        )
        return flat

    def test_mixed_batch_skips_holds(self):
        flats = [self.flat(i) for i in range(6)]
        flats[1].photos.update(aiGenerated=0.9)
        flats[2].photos.update(aiGenerated=None)
        DocumentRequest.objects.create(
            listing=flats[3], requestedBy=self.staff, message="Proof", status="submitted"
        )
        flats[5].seller.is_active = False
        flats[5].seller.save()
        data = {"selected": [str(flat.pk) for flat in flats]}
        for flat in flats:
            data[f"revision-{flat.pk}"] = flat.revision
            data[f"status-{flat.pk}"] = "review"
        data[f"revision-{flats[4].pk}"] = flats[4].revision - 1
        response = self.client.post(reverse("admin_bulk_approve"), data, follow=True)
        for check_text_1 in (
            "2 approved, 4 skipped",
            "Open document request",
            "Changed since review",
            "Seller is suspended",
        ):
            self.assertContains(response, check_text_1)
        for i, flat in enumerate(flats):
            flat.refresh_from_db()
            self.assertEqual(flat.status, "live" if i < 2 else "review")
        self.assertEqual(Decision.objects.filter(decision="approved", staff=self.staff).count(), 2)
        self.assertEqual(AuditLog.objects.filter(action="listing approved", actor=self.staff).count(), 2)
        self.assertFalse(AuditLog.objects.filter(action="photos manually approved").exists())
        self.client.post(reverse("admin_bulk_approve"), data)
        self.assertEqual(AuditLog.objects.filter(action="listing approved").count(), 2)

    def test_form_keyboard_accessible(self):
        flat = self.flat(1)
        response = self.client.get(reverse("admin_queue"))
        self.assertContains(response, 'form="bulk-approve" type="checkbox" name="selected"')
        self.assertContains(response, f'name="revision-{flat.pk}"')
        self.assertContains(response, "Approve selected")
        self.client.force_login(makeUser("customer@test.com"))
        self.assertEqual(
            self.client.post(reverse("admin_bulk_approve"), {"selected": [flat.pk]}).status_code,
            403,
        )
        flat.refresh_from_db()
        self.assertEqual(flat.status, "review")


class AutoCompletionTests(TestCase):

    def setUp(self):
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        setting = override_settings(MEDIA_ROOT=media.name)
        setting.enable()
        self.addCleanup(setting.disable)
        caches["resources"].delete(AUTO_COMPLETE_KEY)
        self.addCleanup(caches["resources"].delete, AUTO_COMPLETE_KEY)
        self.staff = makeStaff("admin@test.com")
        self.seller = makeUser("seller@test.com", "seller")
        self.product = makeProduct("Floor plan", "addon", 40, None)
        self.order = makeOrder(self.seller, self.product, status="processing")
        self.item = self.order.items.get()
        self.client.force_login(self.staff)

    def deliver(self):
        return self.client.post(
            reverse("admin_order_status", args=[self.order.pk]),
            {"status": "delivered", f"file-{self.item.pk}": jpeg()},
        )

    def test_download_start_full_stream(self):
        self.deliver()
        self.client.force_login(self.seller)
        for consume in (False, True):
            response = self.client.get(reverse("delivery_file", args=[self.item.pk]))
            if consume:
                self.assertTrue(b"".join(response.streaming_content))
            closeResponse(response)
            self.item.refresh_from_db()
            self.order.refresh_from_db()
            self.assertIsNone(self.item.downloadedAt)
            self.assertEqual(self.order.status, "processing")
        response = self.client.get(reverse("receipt", args=[self.order.pk]))
        self.assertContains(response, "I’ve received the files or delivery notes")
        url = reverse("confirm_delivery", args=[self.order.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.post(url)
        repeated = self.client.post(url, follow=True)
        self.assertContains(repeated, "Your order is already complete.")
        self.assertNotContains(repeated, "You can confirm once all your services have been delivered.")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "completed")
        entry = AuditLog.objects.get(action="order status", actor=self.seller)
        self.assertEqual(entry.actor, self.seller)
        self.assertIn("confirmed receipt of all deliveries", entry.detail)
        self.assertFalse(AuditLog.objects.filter(actor=None).exists())

    def test_owner_download_missing_file(self):
        self.deliver()
        self.client.force_login(makeUser("other@test.com", "seller"))
        self.assertEqual(self.client.get(reverse("delivery_file", args=[self.item.pk])).status_code, 404)
        self.item.refresh_from_db()
        self.item.deliveryFile.delete(save=False)
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get(reverse("delivery_file", args=[self.item.pk])).status_code, 404)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "processing")

    def test_services_must_delivered_receipt(self):
        self.deliver()
        extra = OrderItem.objects.create(
            order=self.order, product=self.product, price=40, kind="addon", name="More"
        )
        self.client.force_login(self.seller)
        response = self.client.get(reverse("delivery_file", args=[self.item.pk]))
        closeResponse(response)
        self.client.post(reverse("confirm_delivery", args=[self.order.pk]))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "processing")
        self.assertFalse(closeOrder(self.order.pk))
        extra.deliveryFile.save("plan.pdf", ContentFile(b"%PDF-1.4"))
        extra.deliveredAt = timezone.now()
        extra.save()
        self.assertFalse(closeOrder(self.order.pk))
        response = self.client.get(reverse("delivery_file", args=[extra.pk]))
        closeResponse(response)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "processing")
        self.client.post(reverse("confirm_delivery", args=[self.order.pk]))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "completed")

    def test_note_delivery_needs_owner(self):
        url = reverse("confirm_delivery", args=[self.order.pk])
        self.client.post(url)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "processing")
        self.item.deliveryNote = "See the supplied delivery instructions."
        self.item.deliveredAt = timezone.now()
        self.item.save()
        self.assertFalse(closeOrder(self.order.pk))
        self.assertEqual(self.client.post(url).status_code, 404)
        self.client.force_login(makeUser("other-receipt@test.com", "seller"))
        self.assertEqual(self.client.post(url).status_code, 404)
        self.client.force_login(self.seller)
        self.assertContains(
            self.client.get(reverse("my_orders")), "I’ve received the files or delivery notes"
        )
        self.client.post(url)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "completed")
        self.assertEqual(AuditLog.objects.get(action="order status").actor, self.seller)

    def test_scheduled_command_completes(self):
        self.item.deliveryNote = "Delivered"
        self.item.deliveredAt = timezone.now() - timedelta(days=8)
        self.item.downloadedAt = timezone.now() - timedelta(days=20)
        self.item.save()
        extra = OrderItem.objects.create(
            order=self.order,
            product=self.product,
            price=40,
            kind="addon",
            name="Second service",
            deliveryNote="Also delivered",
            deliveredAt=timezone.now() - timedelta(days=6),
        )
        call_command("reconcile_payments", stdout=StringIO())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "processing")
        extra.deliveredAt = timezone.now() - timedelta(days=7)
        extra.save()
        call_command("reconcile_payments", stdout=StringIO())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "completed")
        self.assertIn("7 days since delivery", AuditLog.objects.get(action="order status").detail)

    def test_reconciliation_completes_at(self):
        self.deliver()
        now = timezone.now()
        self.item.refresh_from_db()
        self.item.deliveredAt = now - timedelta(days=AUTO_COMPLETE_DAYS) + timedelta(seconds=1)
        self.item.save()
        with patch("storefront.listing_rules.timezone.now", return_value=now):
            self.assertFalse(closeOrder(self.order.pk))
            self.item.deliveredAt = now - timedelta(days=AUTO_COMPLETE_DAYS)
            self.item.save()
            output = StringIO()
            call_command("reconcile_payments", stdout=output)
            call_command("reconcile_payments", stdout=output)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "completed")
        self.assertEqual(AuditLog.objects.filter(actor=None, action="order status").count(), 1)
        self.assertIn("Completed 1 delivered service orders", output.getvalue())

    def test_page_visits_reconcile_rate(self):
        self.deliver()
        self.item.refresh_from_db()
        self.item.deliveredAt = timezone.now() - timedelta(days=7)
        self.item.save()
        caches["resources"].delete(AUTO_COMPLETE_KEY)
        self.client.force_login(self.seller)
        self.client.get(reverse("my_orders"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "completed")
        caches["resources"].delete(AUTO_COMPLETE_KEY)
        with patch("storefront.listing_rules.closeSent", return_value=0) as complete:
            autoClose()
            autoClose()
        complete.assert_called_once()

    def test_undelivered_refunded_orders(self):
        self.assertFalse(closeOrder(self.order.pk))
        self.deliver()
        self.item.refresh_from_db()
        self.item.deliveredAt = timezone.now() - timedelta(days=8)
        self.item.save()
        self.order.status = "refunded"
        self.order.save()
        self.assertFalse(closeOrder(self.order.pk))
        self.assertFalse(AuditLog.objects.filter(actor=None).exists())

    def test_orders_default_work_show(self):
        past = makeOrder(self.seller, self.product, status="completed")
        package = makeOrder(self.seller, makeProduct(), status="completed")
        default = self.client.get(reverse("admin_orders"))
        self.assertEqual([order.pk for order in default.context["orders"]], [self.order.pk])
        allOrders = self.client.get(reverse("admin_orders"), {"status": ""})
        self.assertCountEqual(
            [order.pk for order in allOrders.context["orders"]],
            [self.order.pk, past.pk, package.pk],
        )
        Ticket.objects.create(
            email=self.seller.email,
            customer=self.seller,
            topic="refund",
            message="Refund",
            order=past,
        )
        response = self.client.get(reverse("admin_orders"), {"refunds": "1"})
        self.assertEqual([order.pk for order in response.context["orders"]], [past.pk])
