from datetime import timedelta
from unittest.mock import patch

from django.db import connection, IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from adminpanel.models import AuditLog
from sellerpanel.reconciliation import syncBatch
from storefront.deals import STEPS
from storefront.listing_rules import closeSent
from storefront.models import (
    ActivityNotice,
    Deal,
    DealStep,
    Offer,
    ViewingBooking,
    ViewingSlot,
    Payment,
    Refund,
)
from storefront.reminders import nudgeDeals
from storefront.tests.helpers import makeListing, makeStaff, makeUser, makeOrder, makeProduct


class DealTests(TestCase):

    def setUp(self):
        self.seller = makeUser("deal-seller@test.com", "seller", name="Wei Ming")
        self.buyer = makeUser("deal-buyer@test.com", "buyer", name="Priya")
        self.flat = makeListing(self.seller)
        self.offer = Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=540000)

    def act(self, user, kind, pk, **data):
        self.client.force_login(user)
        return self.client.post(reverse("activity_action"), {"action": kind, "id": pk, **data}, follow=True)

    def accept(self):
        self.act(self.seller, "accept", self.offer.pk)
        return Deal.objects.get(offer=self.offer)

    def tick(self, user, deal, key, note=""):
        self.client.force_login(user)
        return self.client.post(
            reverse("deal_step", args=[deal.pk]), {"step": key, "note": note}, follow=True
        )

    def test_accepting_offer_opens_hdb(self):
        deal = self.accept()
        self.assertEqual((deal.status, deal.price, deal.buyer_id), ("open", 540000, self.buyer.pk))
        steps = list(deal.steps.all())
        self.assertEqual([step.key for step in steps], [step["key"] for step in STEPS])
        self.assertEqual(steps[0].dueOn, timezone.localdate() + timedelta(days=7))
        self.assertIsNone(steps[1].dueOn)
        page = self.client.get(reverse("deal_page", args=[deal.pk]))
        for check_text_1 in (
            "Your deal for Blk 123 Yishun",
            "0 of 6 steps done",
            "Grant the Option to Purchase",
            "Mark as done",
        ):
            self.assertContains(page, check_text_1)

    def test_right_person_tick_step(self):
        deal = self.accept()
        response = self.tick(self.buyer, deal, "otp")
        self.assertContains(response, "Only the seller can tick this step.")
        self.assertContains(response, "Waiting for the seller.")
        response = self.tick(self.seller, deal, "otp", note="OTP sent on the portal")
        self.assertContains(response, "is ticked. We’ve told the buyer.")
        step = deal.steps.get(key="otp")
        self.assertEqual((step.doneBy, step.note), ("seller", "OTP sent on the portal"))
        self.assertEqual(deal.steps.get(key="valuation").dueOn, timezone.localdate() + timedelta(days=1))
        notice = ActivityNotice.objects.get(user=self.buyer, kind="deal_step")
        self.assertIn("The seller ticked “Grant the Option to Purchase”", notice.text)
        self.assertContains(self.tick(self.seller, deal, "otp"), "This step has changed.")

    def test_completing_last_step_marks(self):
        slot = ViewingSlot.objects.create(
            listing=self.flat, startsAt=timezone.now() + timedelta(days=3), capacity=2
        )
        other = makeUser("deal-other@test.com", "buyer")
        ViewingBooking.objects.create(slot=slot, buyer=other, status="confirmed")
        deal = self.accept()
        for step in STEPS:
            who = self.buyer if step["who"] == "buyer" else self.seller
            self.tick(who, deal, step["key"])
        response = self.client.get(reverse("deal_page", args=[deal.pk]))
        self.assertContains(response, "The sale is complete. FlatFair has marked the flat as sold.")
        deal.refresh_from_db()
        self.flat.refresh_from_db()
        self.assertEqual((deal.status, self.flat.status), ("complete", "sold"))
        self.assertEqual(ViewingBooking.objects.get(buyer=other).status, "cancelled")

    def test_undoing_acceptance_cancels(self):
        deal = self.accept()
        self.tick(self.seller, deal, "otp")
        self.act(self.seller, "reopen", self.offer.pk)
        deal.refresh_from_db()
        self.assertEqual(deal.status, "cancelled")
        self.assertContains(self.client.get(reverse("deal_page", args=[deal.pk])), "This deal was cancelled")
        self.act(self.seller, "accept", self.offer.pk)
        deal.refresh_from_db()
        self.assertEqual(deal.status, "open")
        self.assertFalse(deal.steps.filter(doneAt__isnull=False).exists())
        self.assertEqual(Deal.objects.count(), 1)

    def test_strangers_blocks_open_admin(self):
        deal = self.accept()
        self.client.force_login(makeUser("stranger@test.com", "buyer"))
        self.assertEqual(self.client.get(reverse("deal_page", args=[deal.pk])).status_code, 404)
        admin = makeStaff("deal-admin@test.com")
        self.client.force_login(admin)
        page = self.client.get(reverse("deal_page", args=[deal.pk]))
        self.assertContains(page, "Deal for Blk 123 Yishun")
        self.assertNotContains(page, "Mark as done")
        self.assertEqual(
            self.client.post(reverse("deal_step", args=[deal.pk]), {"step": "otp"}).status_code, 302
        )
        self.assertFalse(deal.steps.filter(doneAt__isnull=False).exists())

    def test_late_step_makes_stuck(self):
        deal = self.accept()
        DealStep.objects.filter(deal=deal, key="otp").update(dueOn=timezone.localdate() - timedelta(days=5))
        self.client.force_login(makeStaff("deal-admin@test.com"))
        page = self.client.get(reverse("admin_deals"), {"tab": "stuck"})
        self.assertContains(page, "Stuck <span>1</span>")
        self.assertContains(page, "Next: Grant the Option to Purchase (seller)")
        overview = self.client.get(reverse("admin_overview"))
        self.assertContains(overview, "Stuck deals")

    def test_reminders_go_out_step(self):
        deal = self.accept()
        DealStep.objects.filter(deal=deal, key="otp").update(dueOn=timezone.localdate() + timedelta(days=1))
        self.assertEqual(nudgeDeals(), 1)
        self.assertEqual(nudgeDeals(), 0)
        notice = ActivityNotice.objects.get(kind="deal_due")
        self.assertEqual(notice.user_id, self.seller.pk)
        self.assertIn("is due tomorrow", notice.text)
        DealStep.objects.filter(deal=deal, key="otp").update(
            dueOn=timezone.localdate() - timedelta(days=1),
            reminderSentOn=timezone.localdate() - timedelta(days=2),
        )
        self.assertEqual(nudgeDeals(), 1)
        self.assertIn("is overdue", ActivityNotice.objects.filter(kind="deal_due").latest("pk").text)

    def test_buyer_seller_see_their(self):
        deal = self.accept()
        self.client.force_login(self.seller)
        self.assertContains(self.client.get(reverse("seller_viewings")), reverse("deal_page", args=[deal.pk]))
        self.client.force_login(self.buyer)
        self.assertContains(self.client.get(reverse("my_offers")), "Deal 0 of 6")
        self.assertContains(
            self.client.get(reverse("listing_detail", args=[self.flat.publicId])), "Open your deal"
        )


class CounterMigrationTests(TransactionTestCase):

    def test_invalid_historic_return_pending(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        previous = [("storefront", "0042_profile")]
        executor.migrate(previous)
        try:
            apps = executor.loader.project_state(previous).apps
            flat = apps.get_model("storefront", "Listing").objects.create(
                town="YISHUN",
                block="123",
                postalCode="760123",
                flatType="4 ROOM",
                floorAreaSqm=92,
                bedrooms=3,
                bathrooms=2,
                hasBalcony=False,
                askingPrice=550000,
                storey=8,
                leaseCommenceYear=1984,
            )
            offers = apps.get_model("storefront", "Offer").objects
            ids = []
            for index, amount in enumerate((None, 0, -1, 525000)):
                buyer = apps.get_model("auth", "User").objects.create(username=f"migration{index}")
                ids.append(
                    offers.create(
                        listing=flat,
                        buyer=buyer,
                        amount=500000,
                        status="countered",
                        counterAmount=amount,
                    ).pk
                )
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            offers = executor.loader.project_state(latest).apps.get_model("storefront", "Offer").objects
            self.assertEqual(
                list(offers.filter(pk__in=ids).order_by("pk").values_list("status", flat=True)),
                ["pending", "pending", "pending", "countered"],
            )
            with self.assertRaises(IntegrityError), transaction.atomic():
                offers.filter(pk=ids[0]).update(status="countered")
        finally:
            MigrationExecutor(connection).migrate(latest)


class OfferRulesMigrationTests(TransactionTestCase):

    def test_invalid_rows_repaired_new(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        previous = [("storefront", "0045_offers")]
        executor.migrate(previous)
        try:
            apps = executor.loader.project_state(previous).apps
            flat = apps.get_model("storefront", "Listing").objects.create(
                town="YISHUN",
                block="123",
                postalCode="760123",
                flatType="4 ROOM",
                floorAreaSqm=92,
                bedrooms=3,
                bathrooms=2,
                hasBalcony=False,
                askingPrice=550000,
                storey=8,
                leaseCommenceYear=1984,
            )
            offers = apps.get_model("storefront", "Offer").objects
            invalid = [
                {"amount": "500000.50", "status": "declined", "counterAmount": "500000.10"},
                {"amount": 3000001, "status": "countered", "counterAmount": 3000002},
                {"amount": 500000, "status": "pending", "counterAmount": 525000},
                {"amount": 500000, "status": "withdrawn", "counterAmount": 525000},
            ]
            ids = []
            for index, values in enumerate(invalid):
                buyer = apps.get_model("auth", "User").objects.create(username=f"rulesmigration{index}")
                ids.append(offers.create(listing=flat, buyer=buyer, **values).pk)
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            offers = executor.loader.project_state(latest).apps.get_model("storefront", "Offer").objects
            first, second, third, fourth = [offers.get(pk=pk) for pk in ids]
            self.assertEqual(first.amount, 500001)
            self.assertIsNone(first.counterAmount)
            self.assertEqual(first.respondedAt, first.createdAt)
            self.assertEqual(second.amount, 3000000)
            self.assertEqual(second.status, "pending")
            self.assertIsNone(second.counterAmount)
            self.assertIsNone(third.counterAmount)
            self.assertEqual(fourth.counterAmount, 525000)
            self.assertEqual(fourth.respondedAt, fourth.createdAt)
            with self.assertRaises(IntegrityError), transaction.atomic():
                offers.filter(pk=ids[3]).update(respondedAt=None)
        finally:
            MigrationExecutor(connection).migrate(latest)


class CompletionBatchTests(TestCase):

    def setUp(self):
        self.seller = makeUser("completion@test.com", "seller")
        self.product = makeProduct("Floor plan", "addon", 40, None)
        self.broken = self.deliveredOrder()
        self.healthy = self.deliveredOrder()
        self.createAudit = AuditLog.objects.create

    def deliveredOrder(self):
        order = makeOrder(self.seller, self.product, status="processing")
        order.items.update(deliveryNote="Plan delivered", deliveredAt=timezone.now() - timedelta(days=8))
        return order

    def auditFailure(self, **values):
        if values["target"] == f"Order #{self.broken.pk}":
            raise RuntimeError("Audit unavailable for this order")
        return self.createAudit(**values)

    def test_rolls_back_failed_order(self):
        with (
            patch("adminpanel.models.AuditLog.objects.create", side_effect=self.auditFailure),
            self.assertLogs("storefront.automation", level="ERROR") as logs,
        ):
            self.assertEqual(closeSent(), 1)
        self.broken.refresh_from_db()
        self.healthy.refresh_from_db()
        self.assertEqual(self.broken.status, "processing")
        self.assertEqual(self.healthy.status, "completed")
        self.assertEqual(
            list(AuditLog.objects.values_list("target", flat=True)), [f"Order #{self.healthy.pk}"]
        )
        self.assertIn(f"order {self.broken.pk}", logs.output[0])

    def test_failure_not_stop_payment(self):
        payment = Payment.objects.create(
            seller=self.seller,
            method="stripe",
            amount=40,
            checkoutToken="pending-completion",
            createdAt=timezone.now() - timedelta(minutes=31),
        )
        paid = Payment.objects.create(
            seller=self.seller,
            method="stripe",
            status="paid",
            amount=40,
            checkoutToken="paid-completion",
        )
        refund = Refund.objects.create(payment=paid, amount=40, staff=makeStaff("completionstaff@test.com"))
        with (
            patch("adminpanel.models.AuditLog.objects.create", side_effect=self.auditFailure),
            patch("sellerpanel.reconciliation.syncPay", return_value="Payment checked") as checkPayment,
            patch("sellerpanel.reconciliation.syncRefund", return_value="Refund checked") as checkRefund,
            self.assertLogs("storefront.automation", level="ERROR"),
        ):
            self.assertEqual(syncBatch(), (1, 1, 0))
        self.assertEqual(checkPayment.call_args.args[0].pk, payment.pk)
        self.assertEqual(checkRefund.call_args.args[0].pk, refund.pk)
        checkPayment.assert_called_once()
        checkRefund.assert_called_once()
