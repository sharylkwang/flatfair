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


class AutoPriceTests(CompletedPhotoQueueMixin, TestCase):

    def setUp(self):
        media = TemporaryDirectory()
        self.addCleanup(media.cleanup)
        setting = override_settings(MEDIA_ROOT=media.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.seller = makeUser("seller@test.com", "seller")
        self.staff = makeStaff("admin@test.com")
        self.client.force_login(self.seller)
        checker = patch(
            "sellerpanel.photos.checkPics", side_effect=lambda photos: [dict(CLEAN)] * len(photos)
        )
        checker.start()
        self.addCleanup(checker.stop)
        price = patch("sellerpanel.listings.band", return_value=(500000, 550000, 600000))
        price.start()
        self.addCleanup(price.stop)

    def submit(self, price):
        token = self.client.get(reverse("sell")).context["draftToken"]
        return self.client.post(
            reverse("sell"),
            {
                **FLAT,
                "askingPrice": str(price),
                "draftToken": token,
                "confirm": "1",
                **{f"photo_{room}": jpeg() for room in ROOMS},
            },
        )

    def test_submit_five_percent_edge(self):
        self.assertEqual(str(AUTO_APPROVE_PRICE_GAP), "0.05")
        for price, status in ((630000, "live"), (630060, "review"), (299999, "review")):
            with self.subTest(price=price):
                response = self.submit(price)
                flat = Listing.objects.get()
                self.assertEqual(flat.status, status)
                self.assertEqual(
                    Decision.objects.filter(listing=flat, staff=None).count(), int(status == "live")
                )
                if status == "live":
                    self.assertIn("above the usual range", str(list(response.wsgi_request._messages)))
                    self.assertTrue(
                        AuditLog.objects.filter(actor=None, action="listing auto-approved").exists()
                    )
                    self.assertContains(
                        self.client.get(reverse("seller_listing", args=[flat.publicId])),
                        "Listing history",
                    )
                    self.assertContains(
                        self.client.get(reverse("seller_listing", args=[flat.publicId])),
                        "Automatic check",
                    )
                    self.assertContains(
                        self.client.get(reverse("listing_detail", args=[flat.publicId])),
                        "This price is on the high side",
                    )
                flat.delete()

    def test_small_gap_preview_confirm(self):
        response = self.client.get(reverse("sell_price_check"), {**FLAT, "askingPrice": "630000"})
        self.assertIn("can go live at this price", response.json()["html"])
        token = self.client.get(reverse("sell")).context["draftToken"]
        response = self.client.post(
            reverse("sell"),
            {
                **FLAT,
                "askingPrice": "630000",
                "draftToken": token,
                **{f"photo_{room}": jpeg() for room in ROOMS},
            },
        )
        self.assertTrue(response.context["needsConfirm"])
        self.assertContains(
            response,
            "With clear photos and no documents to check, your listing can go live at this price.",
        )
        self.assertFalse(Listing.objects.exists())

    def test_edit_price_small_gaps(self):
        self.submit(700000)
        flat = Listing.objects.get()
        self.client.post(
            reverse("edit_listing", args=[flat.publicId]),
            {**FLAT, "askingPrice": "630000", "confirm": "1", "revision": flat.revision},
        )
        flat.refresh_from_db()
        self.assertEqual(flat.status, "live")
        self.client.post(reverse("update_price", args=[flat.publicId]), {"askingPrice": "630060"})
        flat.refresh_from_db()
        self.assertEqual(flat.status, "review")
        self.client.post(reverse("update_price", args=[flat.publicId]), {"askingPrice": "630000"})
        flat.refresh_from_db()
        self.assertEqual(flat.status, "live")
        self.assertEqual(AuditLog.objects.filter(action="listing auto-approved").count(), 2)

    def test_photo_document_holds(self):
        flat = makeListing(self.seller, status="review", askingPrice=630000)
        photo = ListingPhoto.objects.create(
            listing=flat,
            room="kitchen",
            image="room.jpg",
            modelVersion=CLEAN["modelVersion"],
            aiIrrelevant=0.95,
            aiGenerated=0.01,
        )
        self.assertFalse(passPrice(flat))
        photo.aiIrrelevant = 0.01
        photo.modelVersion = None
        photo.save()
        self.assertFalse(passPrice(flat))
        photo.modelVersion = CLEAN["modelVersion"]
        photo.save()
        document = DocumentRequest.objects.create(listing=flat, requestedBy=self.staff, message="Proof")
        self.assertFalse(passPrice(flat))
        document.status = "submitted"
        document.save()
        self.assertFalse(passPrice(flat))
        self.assertFalse(AuditLog.objects.exists())

    def test_corrections_share_publication(self):
        for route in ("edit_listing", "update_price"):
            for price, status in (
                (550000, "live"),
                (630000, "live"),
                (630060, "review"),
                (450000, "review"),
            ):
                with self.subTest(route=route, price=price):
                    self.submit(700000)
                    flat = Listing.objects.get()
                    response = self.client.post(
                        reverse(route, args=[flat.publicId]),
                        {
                            **FLAT,
                            "askingPrice": str(price),
                            "confirm": "1",
                            "revision": flat.revision,
                        },
                    )
                    flat.refresh_from_db()
                    self.assertEqual((flat.askingPrice, flat.status), (price, status))
                    self.assertEqual(
                        Decision.objects.filter(listing=flat, staff=None).count(),
                        int(status == "live"),
                    )
                    if price == 630000:
                        self.assertIn("above the usual range", str(list(response.wsgi_request._messages)))
                        self.assertContains(
                            self.client.get(reverse("listing_detail", args=[flat.publicId])),
                            "This price is on the high side",
                        )
                    flat.delete()

    def test_other_holds_survive_both(self):
        for route in ("edit_listing", "update_price"):
            for hold in ("photo", "document", "reactivation", "staff", "low"):
                for price in (550000, 630000):
                    with self.subTest(route=route, hold=hold, price=price):
                        self.submit(299999 if hold == "low" else 700000)
                        flat = Listing.objects.get()
                        if hold == "photo":
                            flat.photos.filter(room="kitchen").update(aiIrrelevant=0.95)
                        elif hold == "document":
                            DocumentRequest.objects.create(
                                listing=flat, requestedBy=self.staff, message="Proof"
                            )
                        elif hold == "reactivation":
                            self.seller.profile.reactivatedAt = timezone.now()
                            self.seller.profile.save(update_fields=["reactivatedAt"])
                        elif hold == "staff":
                            AuditLog.objects.create(
                                actor=self.staff,
                                action="listing sent to review",
                                target=f"Listing #{flat.pk} ({flat.town.title()})",
                                detail="Check ownership",
                            )
                        self.client.post(
                            reverse(route, args=[flat.publicId]),
                            {
                                **FLAT,
                                "askingPrice": str(price),
                                "confirm": "1",
                                "revision": flat.revision,
                            },
                        )
                        flat.refresh_from_db()
                        self.assertEqual((flat.askingPrice, flat.status), (price, "review"))
                        self.assertFalse(Decision.objects.filter(listing=flat, staff=None).exists())
                        flat.delete()
                        self.seller.profile.reactivatedAt = None
                        self.seller.profile.save(update_fields=["reactivatedAt"])

    def test_full_edit_changed_facts(self):
        for change in ("facts", "photo"):
            for price in (550000, 630000):
                with self.subTest(change=change, price=price):
                    self.submit(700000)
                    flat = Listing.objects.get()
                    changes = {"storey": "9"} if change == "facts" else {"photo_kitchen": jpeg()}
                    self.client.post(
                        reverse("edit_listing", args=[flat.publicId]),
                        {
                            **FLAT,
                            "askingPrice": str(price),
                            "confirm": "1",
                            "revision": flat.revision,
                            **changes,
                        },
                    )
                    flat.refresh_from_db()
                    self.assertEqual((flat.askingPrice, flat.status), (price, "review"))
                    self.client.post(reverse("update_price", args=[flat.publicId]), {"askingPrice": "630000"})
                    flat.refresh_from_db()
                    self.assertEqual(flat.status, "review")
                    self.assertFalse(Decision.objects.filter(listing=flat, staff=None).exists())
                    self.client.force_login(self.staff)
                    self.client.post(
                        reverse("admin_decide", args=[flat.pk]),
                        {"revision": flat.revision, "status": flat.status, "decision": "approved"},
                    )
                    flat.refresh_from_db()
                    self.assertEqual(flat.status, "live")
                    self.client.force_login(self.seller)
                    flat.delete()

    def test_low_hold_survives_high(self):
        for route in ("edit_listing", "update_price"):
            with self.subTest(route=route):
                self.submit(299999)
                flat = Listing.objects.get()
                self.client.post(
                    reverse(route, args=[flat.publicId]),
                    {**FLAT, "askingPrice": "700000", "confirm": "1", "revision": flat.revision},
                )
                self.client.post(reverse("update_price", args=[flat.publicId]), {"askingPrice": "630000"})
                flat.refresh_from_db()
                self.assertEqual(flat.status, "review")
                self.assertFalse(Decision.objects.filter(listing=flat, staff=None).exists())
                flat.delete()

    def test_reactivated_seller_edits(self):
        self.submit(700000)
        flat = Listing.objects.get()
        self.client.force_login(self.staff)
        url = reverse("admin_customer_suspend", args=[self.seller.pk])
        self.client.post(url, {"action": "suspend", "reason": "Check account"})
        self.client.post(url, {"action": "reactivate", "reason": "Account checked"})
        self.client.force_login(self.seller)
        flat.refresh_from_db()
        self.client.post(
            reverse("edit_listing", args=[flat.publicId]),
            {**FLAT, "askingPrice": "630000", "confirm": "1", "revision": flat.revision},
        )
        flat.refresh_from_db()
        self.assertEqual(flat.status, "review")
        self.assertFalse(heldByCost(flat))
        self.client.post(reverse("update_price", args=[flat.publicId]), {"askingPrice": "620000"})
        flat.refresh_from_db()
        self.assertEqual(flat.status, "review")
        self.assertFalse(Decision.objects.filter(listing=flat, staff=None).exists())

    def test_staff_account_holds_prevent(self):
        flat = makeListing(self.seller, status="review", askingPrice=630000)
        for decision in ("rejected", "documents", "takendown"):
            row = Decision.objects.create(
                listing=flat,
                staff=self.staff,
                decision=decision,
                reason="Staff hold",
                askingPrice=flat.askingPrice,
            )
            self.assertFalse(passPrice(flat))
            self.assertFalse(heldByCost(flat))
            row.delete()
        self.seller.is_active = False
        self.seller.save()
        self.assertFalse(passPrice(flat))
        self.seller.is_active = True
        self.seller.save()
        EmailCheck.objects.filter(user=self.seller).update(verifiedAt=None)
        self.assertFalse(passPrice(flat))
        self.assertFalse(Decision.objects.exists())
        flat.refresh_from_db()
        self.assertEqual(flat.status, "review")

    def test_staff_review_audit(self):
        flat = makeListing(self.seller, askingPrice=630000)
        self.client.force_login(self.staff)
        self.client.post(
            reverse("admin_listing_action", args=[flat.pk]),
            {"action": "review", "reason": "Check ownership"},
        )
        flat.refresh_from_db()
        self.assertEqual(flat.status, "review")
        self.client.force_login(self.seller)
        self.client.post(reverse("update_price", args=[flat.publicId]), {"askingPrice": "620000"})
        flat.refresh_from_db()
        self.assertEqual(flat.status, "review")
        self.assertFalse(passPrice(flat))


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
