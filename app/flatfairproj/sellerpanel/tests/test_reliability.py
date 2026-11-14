from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from importlib import import_module
from io import StringIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import time
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth.models import AnonymousUser
from django.core.cache import caches
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings, Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from adminpanel.forms import ProductForm
from adminpanel.models import AuditLog, Decision, DocumentRequest, Ticket, TicketMessage
from .helpers import CompletedPhotoQueueMixin
from sellerpanel import photocheck
from sellerpanel.billing import finishPay, payCopy
from sellerpanel.plans import packsNow, packState, packInfo
from sellerpanel.reconciliation import (
    autoSync,
    syncBatch,
    syncRefund,
    AUTO_RECONCILE_KEY,
    AUTO_RECONCILE_SECONDS,
)
from sellerpanel.tests.helpers import stripe_reply
from storefront import lens
from storefront.models import ActivityNotice, Listing, ListingPhoto, Payment, Product, Refund, Order
from storefront.presentation import withTier
from storefront.tests.helpers import (
    CLEAN,
    FAIR,
    FLAT,
    ROOMS,
    jpeg,
    makeListing,
    makeOrder,
    makeProduct,
    makeStaff,
    makeUser,
)


@override_settings(STRIPE_SECRET_KEY="sk_test_reliability")
class ReconciliationReliabilityTests(TestCase):

    def setUp(self):
        self.seller = makeUser("reliabilityseller@test.com", "seller")
        self.staff = makeStaff("reliabilitystaff@test.com")
        self.product = makeProduct("Floor plan", "addon", 40, None)
        caches["resources"].clear()
        self.addCleanup(caches["resources"].clear)

    def payment(self, token, status="pending"):
        return Payment.objects.create(
            seller=self.seller,
            method="stripe",
            status=status,
            amount=40,
            checkoutToken=token,
            providerSessionId="cs_" + token,
            paymentIntentId="pi_" + token,
            createdAt=timezone.now() - timedelta(minutes=31),
            snapshot=payCopy(
                [
                    {
                        "subtotal": 40,
                        "gst": 0,
                        "total": 40,
                        "status": "paid",
                        "items": [self.product],
                    }
                ],
                None,
            ),
        )

    def refund(self, token, unplaced=False):
        payment = self.payment(token, "paid")
        if unplaced:
            payment.needsAttention = True
            payment.save(update_fields=["needsAttention"])
            refund = Refund.objects.create(
                payment=payment, amount=40, staff=self.staff, providerRefundId="re_" + token
            )
        else:
            order = makeOrder(self.seller, self.product)
            order.payment = payment
            order.save(update_fields=["payment"])
            refund = Refund.objects.create(
                order=order, amount=40, staff=self.staff, providerRefundId="re_" + token
            )
        return (refund, payment)

    def reply(self, refund, payment, status="succeeded"):
        return stripe_reply(
            {
                "id": refund.providerRefundId,
                "payment_intent": payment.paymentIntentId,
                "amount": 4000,
                "currency": "sgd",
                "status": status,
            }
        )

    def test_command_logs_counts_continues(self):
        broken = self.payment("broken")
        healthy = self.payment("healthy")
        reply = stripe_reply(
            {
                "id": healthy.providerSessionId,
                "livemode": False,
                "mode": "payment",
                "client_reference_id": healthy.checkoutToken,
                "amount_total": 4000,
                "currency": "sgd",
                "metadata": {"seller": str(self.seller.pk)},
                "status": "complete",
                "payment_status": "paid",
                "payment_intent": healthy.paymentIntentId,
            }
        )
        output = StringIO()
        with (
            patch("sellerpanel.stripepay.requests.get", side_effect=[RuntimeError("broken"), reply]),
            self.assertLogs("sellerpanel.reconciliation", level="ERROR") as logs,
        ):
            call_command("reconcile_payments", stdout=output)
        broken.refresh_from_db()
        self.assertEqual(broken.status, "pending")
        self.assertEqual(healthy.orders.count(), 1)
        self.assertIn(f"payment {broken.pk}", logs.output[0])
        self.assertIn("Failed 1 reconciliation checks.", output.getvalue())

    def test_automatic_batch_continues(self):
        self.payment("broken")
        broken, payment = self.refund("brokenrefund")
        healthy, healthyPayment = self.refund("healthyrefund")
        with (
            patch(
                "sellerpanel.stripepay.requests.get",
                side_effect=[
                    RuntimeError("broken payment"),
                    RuntimeError("broken refund"),
                    self.reply(healthy, healthyPayment),
                ],
            ),
            self.assertLogs("sellerpanel.reconciliation", level="ERROR"),
        ):
            self.assertEqual(autoSync(), (0, 1, 2))
        healthy.refresh_from_db()
        self.assertEqual(healthy.status, "succeeded")
        self.assertEqual(broken.status, "pending")

    def test_pending_order_refund_resolves(self):
        refund, payment = self.refund("orderrefund")
        ticket = Ticket.objects.create(
            customer=self.seller,
            email=self.seller.email,
            topic="refund",
            message="Refund please",
            order=refund.order,
        )
        with patch("sellerpanel.stripepay.requests.get", return_value=self.reply(refund, payment)) as get:
            self.assertEqual(syncBatch(), (0, 1, 0))
            self.assertEqual(syncBatch(), (0, 0, 0))
        get.assert_called_once()
        refund.refresh_from_db()
        refund.order.refresh_from_db()
        payment.refresh_from_db()
        ticket.refresh_from_db()
        self.assertEqual(
            (refund.status, refund.order.status, payment.status),
            ("succeeded", "refunded", "refunded"),
        )
        self.assertEqual((ticket.status, ticket.assignee), ("resolved", self.staff))
        self.assertEqual(TicketMessage.objects.get().author, self.staff)
        self.assertEqual(ActivityNotice.objects.get().kind, "order_refunded")
        self.assertTrue(AuditLog.objects.filter(actor=self.staff, action="order status").exists())

    def test_pending_refund_failure(self):
        for token, status in [("failedrefund", "failed"), ("pendingrefund", "pending")]:
            refund, payment = self.refund(token)
            with patch(
                "sellerpanel.stripepay.requests.get",
                return_value=self.reply(refund, payment, status),
            ):
                syncRefund(refund)
            refund.refresh_from_db()
            self.assertEqual(refund.status, status)
            self.assertEqual(refund.order.status, "paid")
        self.assertFalse(ActivityNotice.objects.exists())

    def test_pending_unplaced_refund(self):
        refund, payment = self.refund("unplaced", True)
        with patch("sellerpanel.stripepay.requests.get", return_value=self.reply(refund, payment)):
            call_command("reconcile_payments", stdout=StringIO())
        payment.refresh_from_db()
        self.assertEqual((payment.status, payment.needsAttention), ("refunded", False))
        self.assertIn("refunded your payment in full", ActivityNotice.objects.get().text)

    def test_timeout_missing_provider_id(self):
        refund, payment = self.refund("timeoutrefund")
        refund.providerRefundId = ""
        refund.save(update_fields=["providerRefundId"])
        reply = self.reply(refund, payment)
        reply.json.return_value["id"] = "re_recovered"
        with patch("sellerpanel.stripepay.requests.post", return_value=reply) as post:
            syncRefund(refund)
        self.assertEqual(post.call_args.kwargs["headers"]["Idempotency-Key"], f"refund-{refund.pk}")
        refund.refresh_from_db()
        self.assertEqual((refund.status, refund.providerRefundId), ("succeeded", "re_recovered"))


class PackageSnapshotTests(TestCase):

    def test_purchased_benefits_survive(self):
        seller = makeUser("snapshot@test.com", "seller")
        flat = makeListing(seller)
        product = makeProduct()
        payment = Payment.objects.create(
            seller=seller,
            method="demo",
            status="paid",
            amount=49,
            checkoutToken="snapshot",
            snapshot=payCopy(
                [
                    {
                        "subtotal": 49,
                        "gst": 0,
                        "total": 49,
                        "status": "completed",
                        "items": [product],
                    }
                ],
                flat,
            ),
        )
        Product.objects.filter(pk=product.pk).update(tier=3, durationDays=None)
        orders, problem = finishPay(payment)
        self.assertEqual(problem, "")
        item = orders[0].items.get()
        self.assertEqual((item.tier, item.durationDays), (1, 30))
        Product.objects.filter(pk=product.pk).update(tier=2, durationDays=90)
        self.assertEqual(packsNow([flat])[0]["tier"], 1)
        self.assertEqual(withTier(Listing.objects.all()).get().rank, 1)
        orders[0].createdAt = timezone.now() - timedelta(days=31)
        orders[0].save(update_fields=["createdAt"])
        item.refresh_from_db()
        self.assertEqual(packsNow([flat]), [])
        self.assertEqual(packState(item), "Ended")
        self.assertEqual(withTier(Listing.objects.all()).get().rank, 0)

    def test_until_sold_survives_catalogue(self):
        seller = makeUser("topsnapshot@test.com", "seller")
        flat = makeListing(seller)
        product = makeProduct("Top spot", days=None)
        order = makeOrder(seller, product, listing=flat)
        order.createdAt = timezone.now() - timedelta(days=365)
        order.save(update_fields=["createdAt"])
        Product.objects.filter(pk=product.pk).update(tier=1, durationDays=30)
        self.assertEqual(packsNow([flat])[0]["tier"], 3)
        self.assertIsNone(packsNow([flat])[0]["expiry"])

    def test_data_migration_backfills(self):
        seller = makeUser("migration@test.com", "seller")
        product = makeProduct("Boost 90 days", days=90)
        item = makeOrder(seller, product).items.get()
        item.tier = 0
        item.durationDays = 30
        item.save()
        migration = import_module("storefront.migrations.0042_profile")
        migration.old_0044_snapshotBenefits(apps, None)
        item.refresh_from_db()
        self.assertEqual((item.tier, item.durationDays), (2, 90))


class PublicationReliabilityTests(CompletedPhotoQueueMixin, TransactionTestCase):

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.mediaRoot = Path(directory.name)
        settings = override_settings(MEDIA_ROOT=directory.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.seller = makeUser("publicationseller@test.com", "seller")
        self.staff = makeStaff("publicationstaff@test.com")

    def photo(self, flat):
        photo = ListingPhoto.objects.create(listing=flat, room="kitchen", modelVersion="old")
        photo.image.save("room.jpg", ContentFile(b"photo"))
        return photo

    def reviewData(self, flat):
        return {"decision": "approved", "revision": flat.revision, "status": flat.status}

    def test_seller_inference_precedes_lock(self):
        self.client.force_login(self.seller)
        token = self.client.get(reverse("sell")).context["draftToken"]
        events = []
        from sellerpanel.listings import lock_user

        def check(data):
            self.assertFalse(connection.in_atomic_block)
            events.append("check")
            return [CLEAN] * len(data)

        def lock(pk):
            events.append("lock")
            self.assertTrue(connection.in_atomic_block)
            return lock_user(pk)

        with (
            patch("sellerpanel.photos.checkPics", side_effect=check),
            patch("sellerpanel.listings.lock_user", side_effect=lock),
            patch("sellerpanel.listings.band", return_value=FAIR),
        ):
            self.client.post(
                reverse("sell"),
                {**FLAT, "draftToken": token, **{f"photo_{room}": jpeg() for room in ROOMS}},
            )
            self.assertEqual(events, ["check", "lock"])
            flat = Listing.objects.get()
            events.clear()
            self.client.post(
                reverse("edit_listing", args=[flat.publicId]),
                {**FLAT, "revision": flat.revision, "photo_kitchen": jpeg()},
            )
            self.assertEqual(events, ["check", "lock"])

    def test_staff_inference_precedes_lock(self):
        self.client.force_login(self.staff)
        flat = makeListing(self.seller, status="review")
        photo = self.photo(flat)
        events = []

        def check(data):
            self.assertFalse(connection.in_atomic_block)
            events.append("check")
            return [CLEAN] * len(data)

        for action in ("approve", "relist"):
            with self.subTest(action=action):
                flat.refresh_from_db()
                flat.status = "review" if action == "approve" else "hidden"
                flat.save()
                photo.modelVersion = "old"
                photo.save()
                events.clear()
                from storefront.models import lock_user

                def lock(pk):
                    self.assertTrue(connection.in_atomic_block)
                    events.append("lock")
                    return lock_user(pk)

                with (
                    patch("sellerpanel.photos.checkPics", side_effect=check),
                    patch("adminpanel.moderation.lock_user", side_effect=lock),
                ):
                    if action == "approve":
                        self.client.post(reverse("admin_decide", args=[flat.pk]), self.reviewData(flat))
                    else:
                        self.client.post(
                            reverse("admin_listing_action", args=[flat.pk]), {"action": "relist"}
                        )
                self.assertEqual(events, ["check", "lock"])
                flat.refresh_from_db()
                self.assertEqual(flat.status, "live")

    def test_staff_blocks_listing_change(self):
        flat = makeListing(self.seller, status="review")
        photo = self.photo(flat)
        self.client.force_login(self.staff)

        def check(data):
            changed = Listing.objects.get(pk=flat.pk)
            changed.description = "Replacement details"
            changed.save()
            return [CLEAN]

        with patch("sellerpanel.photos.checkPics", side_effect=check):
            response = self.client.post(
                reverse("admin_decide", args=[flat.pk]), self.reviewData(flat), follow=True
            )
        flat.refresh_from_db()
        photo.refresh_from_db()
        self.assertEqual((flat.status, photo.modelVersion), ("review", "old"))
        self.assertFalse(Decision.objects.exists())
        self.assertContains(response, "changed since you reviewed it")

    def test_staff_blocks_replacement_photo(self):
        flat = makeListing(self.seller, status="review")
        photo = self.photo(flat)
        self.client.force_login(self.staff)

        def check(data):
            ListingPhoto.objects.filter(pk=photo.pk).update(image="replacement.jpg")
            return [CLEAN]

        with patch("sellerpanel.photos.checkPics", side_effect=check):
            self.client.post(reverse("admin_decide", args=[flat.pk]), self.reviewData(flat))
        flat.refresh_from_db()
        photo.refresh_from_db()
        self.assertEqual((flat.status, photo.modelVersion), ("review", "old"))
        self.assertFalse(Decision.objects.exists())

    def test_failed_deletes_files_rolls(self):
        self.client.force_login(self.seller)
        token = self.client.get(reverse("sell")).context["draftToken"]
        originalSave = ListingPhoto.save

        def fail(photo, *args, **kwargs):
            originalSave(photo, *args, **kwargs)
            raise RuntimeError("publication failed")

        with (
            patch("sellerpanel.photos.checkPics", return_value=[CLEAN] * len(ROOMS)),
            patch("sellerpanel.listings.band", return_value=FAIR),
            patch.object(ListingPhoto, "save", fail),
            self.assertRaisesMessage(RuntimeError, "publication failed"),
        ):
            self.client.post(
                reverse("sell"),
                {**FLAT, "draftToken": token, **{f"photo_{room}": jpeg() for room in ROOMS}},
            )
        self.assertFalse(Listing.objects.exists())
        self.assertFalse(ListingPhoto.objects.exists())
        self.assertEqual(list((self.mediaRoot / "listings").rglob("*.jpg")), [])

    def test_later_failure_deletes_new(self):
        self.client.force_login(self.seller)
        token = self.client.get(reverse("sell")).context["draftToken"]
        from sellerpanel.listings import attachPics

        def fail(*args):
            attachPics(*args)
            self.assertEqual(ListingPhoto.objects.count(), len(ROOMS))
            raise RuntimeError("later publication failed")

        with (
            patch("sellerpanel.photos.checkPics", return_value=[CLEAN] * len(ROOMS)),
            patch("sellerpanel.listings.band", return_value=FAIR),
            patch("sellerpanel.listings.attachPics", side_effect=fail),
            self.assertRaisesMessage(RuntimeError, "later publication failed"),
        ):
            self.client.post(
                reverse("sell"),
                {**FLAT, "draftToken": token, **{f"photo_{room}": jpeg() for room in ROOMS}},
            )
        self.assertFalse(Listing.objects.exists())
        self.assertFalse(ListingPhoto.objects.exists())
        self.assertEqual(list((self.mediaRoot / "listings").rglob("*.jpg")), [])

    def test_failed_edit_deletes_replacement(self):
        self.client.force_login(self.seller)
        flat = makeListing(
            self.seller,
            **{
                "town": "JURONG EAST",
                "block": "225A",
                "postalCode": "601225",
                "flatType": "3 ROOM",
                "flatModel": "New Generation",
                "floorAreaSqm": 67,
                "bedrooms": 2,
                "bathrooms": 1,
                "leaseCommenceYear": 1984,
            },
        )
        for room in ROOMS:
            photo = ListingPhoto.objects.create(
                listing=flat,
                room=room,
                modelVersion=CLEAN["modelVersion"],
                aiIrrelevant=0.01,
                aiGenerated=0.01,
            )
            photo.image.save("room.jpg", ContentFile(b"original photo"))
        originalFiles = set(self.mediaRoot.rglob("*.jpg"))
        originalPhotos = list(flat.photos.values_list("pk", "image"))
        from sellerpanel.listings import attachPics

        def fail(*args):
            attachPics(*args)
            raise RuntimeError("edit failed")

        with (
            patch("sellerpanel.photos.checkPics", return_value=[CLEAN]),
            patch("sellerpanel.listings.band", return_value=FAIR),
            patch("sellerpanel.listings.attachPics", side_effect=fail),
            self.assertRaisesMessage(RuntimeError, "edit failed"),
        ):
            self.client.post(
                reverse("edit_listing", args=[flat.publicId]),
                {**FLAT, "revision": flat.revision, "photo_kitchen": jpeg()},
            )
        flat.refresh_from_db()
        self.assertEqual(flat.revision, 0)
        self.assertEqual(list(flat.photos.values_list("pk", "image")), originalPhotos)
        self.assertEqual(set((self.mediaRoot / "listings").rglob("*.jpg")), originalFiles)


class ModerationSnapshotTests(TestCase):

    def setUp(self):
        self.staff = makeStaff("snapshotstaff@test.com")
        self.seller = makeUser("moderationseller@test.com", "seller")
        self.flat = makeListing(self.seller, status="review")
        self.client.force_login(self.staff)
        self.url = reverse("admin_decide", args=[self.flat.pk])

    def test_queue_includes_reviewed(self):
        response = self.client.get(reverse("admin_queue_item", args=[self.flat.pk]))
        self.assertContains(response, f'name="revision" value="{self.flat.revision}"')
        self.assertContains(response, 'name="status" value="review"')

    def test_missing_stale_revision_changed(self):
        originalRevision = self.flat.revision
        self.flat.description = "Seller replacement"
        self.flat.save()
        for choice in ("approved", "rejected", "documents"):
            for data in (
                {},
                {"revision": originalRevision, "status": "review"},
                {"revision": self.flat.revision, "status": "live"},
            ):
                with (
                    self.subTest(choice=choice, data=data),
                    patch("sellerpanel.photos.checkPics") as check,
                ):
                    response = self.client.post(
                        self.url, {**data, "decision": choice, "reason": "Reason"}, follow=True
                    )
                check.assert_not_called()
                self.assertContains(response, "changed since you reviewed it")
                self.assertFalse(Decision.objects.exists())
                self.assertFalse(AuditLog.objects.exists())
                self.assertFalse(ActivityNotice.objects.exists())
                self.assertFalse(DocumentRequest.objects.exists())
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")

    def test_stale_document_submission(self):
        documents = DocumentRequest.objects.create(
            listing=self.flat, requestedBy=self.staff, message="Lease", status="submitted"
        )
        oldVersion = documents.updatedAt.isoformat()
        documents.updatedAt = timezone.now() + timedelta(seconds=1)
        documents.save()
        response = self.client.post(
            self.url,
            {
                "decision": "approved",
                "revision": self.flat.revision,
                "status": self.flat.status,
                "documentRequest": documents.pk,
                "documentVersion": oldVersion,
            },
            follow=True,
        )
        self.assertContains(response, "documents have already changed")
        documents.refresh_from_db()
        self.assertEqual(documents.status, "submitted")
        self.assertFalse(Decision.objects.exists())


resourceCache = caches["resources"]


class PackageDurationTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("duration@test.com", "seller")
        cls.flat = makeListing(cls.seller)

    def test_catalogue_requires_tier(self):
        for name, tier, duration in [
            ("Boost 30 days", 1, 30),
            ("Boost 90 days", 2, 90),
            ("Top spot", 3, None),
        ]:
            data = {
                "name": name,
                "kind": "package",
                "tier": tier,
                "price": 60,
                "durationDays": duration,
            }
            with self.subTest(name=name):
                self.assertTrue(ProductForm(data).is_valid())
                for days in [1, 60, None] if duration is not None else [30, 90]:
                    form = ProductForm({**data, "durationDays": days})
                    self.assertFalse(form.is_valid())
                    self.assertIn("must last", str(form.non_field_errors()))
        for name, tier, days in [
            ("Boost 30 days", 2, 90),
            ("Top spot", 1, 30),
            ("Boost 60 days", 2, 90),
        ]:
            form = ProductForm(
                {"name": name, "kind": "package", "tier": tier, "price": 60, "durationDays": days}
            )
            self.assertFalse(form.is_valid())
            self.assertIn("name must match", str(form.non_field_errors()))

    def test_editing_catalogue_blocks_keep(self):
        product = makeProduct(days=60)
        data = {"name": product.name, "kind": "package", "price": 60}
        self.assertFalse(ProductForm(data, instance=product).is_valid())
        product.durationDays = 30
        product.save()
        form = ProductForm({**data, "name": "Boost 90 days"}, instance=product)
        self.assertFalse(form.is_valid())

    def test_storefront_cards_actual(self):
        boost = makeProduct(days=17)
        top = makeProduct("Top spot", days=45)
        response = self.client.get(reverse("packages"))
        for product, duration, benefit in (
            (boost, 17, "Shown above free flats"),
            (top, 45, "Shown first in search"),
        ):
            card = (
                response.content.decode().split(f'id="package-{product.pk}"', 1)[1].split("</article>", 1)[0]
            )
            self.assertIn(f'<p class="package-duration">{duration} days</p>', card)
            self.assertIn(benefit, card)
            self.assertNotIn("Until sold", card)

    def test_checkout_blocks_invalid(self):
        product = makeProduct(days=60)
        self.client.force_login(self.seller)
        self.client.post(reverse("cart_add", args=[product.pk]))
        token = self.client.get(reverse("cart")).context["checkoutToken"]
        response = self.client.post(
            reverse("checkout"),
            {"checkoutToken": token, "listing": self.flat.publicId},
            follow=True,
        )
        self.assertContains(response, "must last 30 days")
        self.assertFalse(Payment.objects.exists())

    def test_paid_snapshot_invalid_requires(self):
        product = makeProduct(days=60)
        payment = Payment.objects.create(
            seller=self.seller,
            method="demo",
            status="paid",
            amount=product.price,
            checkoutToken="invalid-duration",
            snapshot=payCopy(
                [
                    {
                        "subtotal": product.price,
                        "gst": 0,
                        "total": product.price,
                        "status": "completed",
                        "items": [product],
                    }
                ],
                self.flat,
            ),
        )
        orders, problem = finishPay(payment)
        self.assertEqual(orders, [])
        self.assertIn("must last 30 days", problem)
        payment.refresh_from_db()
        self.assertTrue(payment.needsAttention)
        self.assertFalse(Order.objects.exists())

    def test_expiry_purchased_snapshot(self):
        now = timezone.now()
        cases = [
            ("Boost 30 days", 90, 31, True, "Active"),
            ("Boost 90 days", 10, 31, False, "Ended"),
            ("Top spot", None, 100, True, "Until sold"),
        ]
        for name, days, age, active, status in cases:
            with self.subTest(name=name):
                product = makeProduct(name, days=days)
                order = makeOrder(self.seller, product, listing=self.flat)
                order.createdAt = now - timedelta(days=age)
                order.save(update_fields=["createdAt"])
                line = order.items.select_related("product", "order", "listing").get()
                self.assertEqual(
                    bool([plan for plan in packsNow([self.flat], now) if plan["order"] == order]),
                    active,
                )
                self.assertTrue(packState(line, now).startswith(status))
                self.assertEqual(packInfo(line, now)["status"], packState(line, now))
                if name == "Top spot":
                    self.assertIsNone(packInfo(line, now)["expiry"])
        self.flat.status = "sold"
        self.flat.save(update_fields=["status"])
        self.assertEqual(packsNow([self.flat], now), [])


class PhotoResourceTests(TransactionTestCase):

    def setUp(self):
        resourceCache.clear()
        self.addCleanup(resourceCache.clear)

    def request(self, address="127.0.0.1", user=None):
        request = RequestFactory().post("/lens", REMOTE_ADDR=address)
        request.user = user or AnonymousUser()
        request.session = {}
        return request

    def test_ip_limit_survives_fresh(self):
        for number in range(lens.SEARCHES_PER_HOUR):
            self.assertFalse(lens.searchFlood(self.request()))
        request = self.request()
        request.META["HTTP_X_FORWARDED_FOR"] = "198.51.100.99"
        self.assertTrue(lens.searchFlood(request))
        self.assertFalse(lens.searchFlood(self.request("192.0.2.2")))

    def test_user_limit_survives_ip(self):
        user = SimpleNamespace(is_authenticated=True, pk=123)
        now = int(time.time() // 3600) * 3600 + 10
        with patch("storefront.lens.time.time", return_value=now):
            for number in range(lens.SEARCHES_PER_HOUR):
                self.assertFalse(lens.searchFlood(self.request(f"192.0.2.{number}", user)))
            self.assertTrue(lens.searchFlood(self.request("198.51.100.1", user)))
        with patch("storefront.lens.time.time", return_value=now + 3601):
            self.assertFalse(lens.searchFlood(self.request("198.51.100.1", user)))

    @patch("sellerpanel.photocheck.resourceCache", caches["default"])
    @patch("sellerpanel.photocheck.verifyModel", return_value="verified")
    @patch("sellerpanel.photocheck.isOn", return_value=True)
    def test_global_cap_allows_two(self, isOn, verifyModel):
        entered = Barrier(3)
        release = Event()

        def run(*args, **kwargs):
            entered.wait(timeout=10)
            release.wait(timeout=10)
            return SimpleNamespace(returncode=0, stdout='[{"label": "kitchen"}]')

        with patch("sellerpanel.photocheck.subprocess.run", side_effect=run) as child:
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [workers.submit(photocheck.checkPics, [b"photo"]) for number in range(2)]
                try:
                    entered.wait(timeout=10)
                    self.assertEqual(photocheck.checkPics([b"third"]), [{"busy": True, "unchecked": True}])
                    self.assertEqual(child.call_count, 2)
                finally:
                    release.set()
                self.assertEqual(
                    [future.result(timeout=10) for future in futures], [[{"label": "kitchen"}]] * 2
                )
        with patch(
            "sellerpanel.photocheck.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="[{}]"),
        ) as child:
            self.assertEqual(photocheck.checkPics([b"next"]), [{}])
            child.assert_called_once()
            self.assertEqual(child.call_args.kwargs["env"]["CUDA_VISIBLE_DEVICES"], "")

    @patch("sellerpanel.photocheck.verifyModel", return_value="verified")
    @patch("sellerpanel.photocheck.isOn", return_value=True)
    def test_slots_released_timeout_crash(self, isOn, verifyModel):
        for result in [
            subprocess.TimeoutExpired("photo", 120),
            SimpleNamespace(returncode=1, stderr="failed"),
            SimpleNamespace(returncode=0, stdout="invalid"),
        ]:
            with self.subTest(result=result), self.assertLogs("sellerpanel.photocheck", "ERROR"):
                kwargs = (
                    {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
                )
                with patch("sellerpanel.photocheck.subprocess.run", **kwargs):
                    self.assertEqual(photocheck.checkPics([b"photo"]), [None])
                self.assertIsNone(resourceCache.get("photo-check-slot:0"))
                self.assertIsNone(resourceCache.get("photo-check-slot:1"))


class LensUploadLimitTests(TestCase):

    def setUp(self):
        resourceCache.clear()
        self.addCleanup(resourceCache.clear)

    def test_fresh_browser_sessions_blocks(self):
        result = {**CLEAN, "embedding": [1.0] + [0.0] * 511}
        with patch("storefront.lens.checkPics", return_value=[result]) as check:
            for number in range(lens.SEARCHES_PER_HOUR):
                response = Client().post(reverse("lens"), {"photo": jpeg()})
                self.assertTrue(response.context["searched"])
            response = Client().post(reverse("lens"), {"photo": jpeg()})
            self.assertContains(response, lens.BUSY)
            self.assertEqual(check.call_count, lens.SEARCHES_PER_HOUR)

    def test_busy_photo_check_friendly(self):
        with patch("storefront.lens.checkPics", return_value=[{"busy": True, "unchecked": True}]):
            response = self.client.post(reverse("lens"), {"photo": jpeg()})
        self.assertContains(response, photocheck.PHOTO_BUSY)
        self.assertNotIn("searched", response.context)


@override_settings(STRIPE_SECRET_KEY="sk_test_automatic")
class AutomaticReconciliationTests(TransactionTestCase):

    def setUp(self):
        resourceCache.clear()
        self.addCleanup(resourceCache.clear)
        self.seller = makeUser("automatic@test.com", "seller")
        self.product = makeProduct("Floor plan", "addon", 40, None)
        self.payment = Payment.objects.create(
            seller=self.seller,
            method="stripe",
            status="pending",
            amount=40,
            checkoutToken="automatic-checkout",
            providerSessionId="cs_test_automatic",
            snapshot=payCopy(
                [
                    {
                        "subtotal": 40,
                        "gst": 0,
                        "total": 40,
                        "status": "paid",
                        "items": [self.product],
                    }
                ],
                None,
            ),
            createdAt=timezone.now() - timedelta(minutes=31),
        )
        self.client.force_login(makeStaff("automatic-staff@test.com"))

    def reply(self, **changes):
        values = {
            "id": self.payment.providerSessionId,
            "livemode": False,
            "mode": "payment",
            "client_reference_id": self.payment.checkoutToken,
            "amount_total": 4000,
            "currency": "sgd",
            "metadata": {"seller": str(self.seller.pk)},
            "status": "complete",
            "payment_status": "paid",
            "payment_intent": "pi_test_automatic",
        }
        values.update(changes)
        return stripe_reply(values)

    def test_staff_page_automatically(self):

        def check(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            return self.reply()

        with patch("sellerpanel.stripepay.requests.get", side_effect=check) as get:
            self.assertEqual(self.client.get(reverse("admin_payments")).status_code, 200)
            self.client.get(reverse("admin_payments"))
            self.assertEqual(get.call_count, 1)
        self.payment.refresh_from_db()
        self.assertEqual((self.payment.status, self.payment.needsAttention), ("paid", False))
        self.assertEqual(self.payment.orders.count(), 1)

    def test_unconfirmed_payment_retried_ten(self):
        with patch("sellerpanel.stripepay.requests.get", return_value=self.reply(amount_total=1)) as get:
            self.client.get(reverse("admin_payments"))
            self.client.get(reverse("admin_payments"))
            self.assertEqual(get.call_count, 1)
            resourceCache.delete(AUTO_RECONCILE_KEY)
            autoSync()
            self.assertEqual(get.call_count, 2)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "pending")
        self.assertFalse(Order.objects.exists())
        self.assertEqual(AUTO_RECONCILE_SECONDS, 600)

    def test_fresh_payments_unauthorized(self):
        self.payment.createdAt = timezone.now()
        self.payment.save(update_fields=["createdAt"])
        with patch("sellerpanel.stripepay.requests.get") as get:
            self.client.get(reverse("admin_payments"))
            resourceCache.delete(AUTO_RECONCILE_KEY)
            self.client.force_login(self.seller)
            self.assertEqual(self.client.get(reverse("admin_payments")).status_code, 403)
            get.assert_not_called()

    def test_check_continues_provider_error(self):
        with patch(
            "sellerpanel.reconciliation.syncPay", side_effect=RuntimeError("provider failed")
        ) as reconcile:
            with self.assertLogs("sellerpanel.reconciliation", "ERROR"):
                autoSync()
            autoSync()
            self.assertEqual(reconcile.call_count, 1)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "pending")
