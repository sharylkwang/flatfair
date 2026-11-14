from datetime import timedelta
import io
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection, IntegrityError
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
import requests

from adminpanel.models import AuditLog
from sellerpanel.billing import payCopy
from sellerpanel.tests.helpers import stripe_reply
from storefront.models import ActivityNotice, Order, Payment, Refund
from storefront.tests.helpers import makeListing, makeOrder, makeProduct, makeStaff, makeUser


def attentionPayment(seller, product, **changes):
    values = {
        "seller": seller,
        "method": "stripe",
        "status": "paid",
        "amount": product.price,
        "checkoutToken": "attention-checkout",
        "providerSessionId": "cs_test_attention",
        "paymentIntentId": "pi_test_attention",
        "needsAttention": True,
        "snapshot": payCopy(
            [
                {
                    "subtotal": product.price,
                    "gst": 0,
                    "total": product.price,
                    "status": "paid",
                    "items": [product],
                }
            ],
            None,
        ),
    }
    values.update(changes)
    return Payment.objects.create(**values)


@override_settings(STRIPE_SECRET_KEY="sk_test_attention")
class PaymentResolutionTests(TestCase):

    def setUp(self):
        self.staff = makeStaff("attentionstaff@test.com")
        self.seller = makeUser("attentionseller@test.com", "seller")
        self.product = makeProduct("Floor plan", "addon", 40, None)
        self.payment = attentionPayment(self.seller, self.product)
        self.client.force_login(self.staff)
        self.url = reverse("admin_payment_action", args=[self.payment.pk])

    def act(self, action, **kwargs):
        with patch("sellerpanel.stripepay.requests.post", **kwargs) as post:
            response = self.client.post(self.url, {"action": action}, follow=True)
        self.payment.refresh_from_db()
        return (response, post)

    def refundReply(self, **changes):
        values = {
            "id": "re_test_attention",
            "status": "succeeded",
            "amount": 4000,
            "currency": "sgd",
            "payment_intent": self.payment.paymentIntentId,
        }
        values.update(changes)
        return stripe_reply(values)

    def sessionReply(self, **changes):
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
            "payment_intent": self.payment.paymentIntentId,
        }
        values.update(changes)
        return stripe_reply(values)

    def test_attention_page_shows_snapshot(self):
        response = self.client.get(reverse("admin_payments"))
        for text in (
            "Payments needing attention",
            "Floor plan",
            "cs_test_attention",
            "Check with Stripe",
            "Create order",
            "Refund payment without an order",
            "Not checked by staff",
        ):
            self.assertContains(response, text)
        for user in (makeUser("attentionbuyer@test.com"), self.seller):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("admin_payments")).status_code, 403)
            self.assertEqual(self.client.post(self.url, {"action": "refund"}).status_code, 403)
        self.assertFalse(Refund.objects.exists())

    def test_actions_require_post_known(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(self.client.post(self.url, {"action": "unknown"}).status_code, 400)
        self.assertFalse(AuditLog.objects.exists())

    def test_place_purchase_audited_notified(self):
        self.act("place")
        self.act("place")
        self.assertFalse(self.payment.needsAttention)
        order = self.payment.orders.get()
        self.assertEqual(order.items.get().name, "Floor plan")
        self.assertEqual(AuditLog.objects.get().action, "payment purchase")
        self.assertEqual(ActivityNotice.objects.get().user, self.seller)

    def test_place_purchase_rechecks_seller(self):
        self.seller.is_active = False
        self.seller.save(update_fields=["is_active"])
        self.act("place")
        self.assertTrue(self.payment.needsAttention)
        self.assertFalse(Order.objects.exists())
        self.assertIn("active seller", AuditLog.objects.get().detail)
        self.assertIn("active seller", ActivityNotice.objects.get().text)

    def test_fixed_listing_placed_snapshot(self):
        flat = makeListing(self.seller, status="sold")
        package = makeProduct()
        self.payment.snapshot = payCopy(
            [
                {
                    "subtotal": package.price,
                    "gst": 0,
                    "total": package.price,
                    "status": "completed",
                    "items": [package],
                }
            ],
            flat,
        )
        self.payment.amount = package.price
        self.payment.save()
        self.act("place")
        self.assertFalse(Order.objects.exists())
        flat.status = "live"
        flat.save(update_fields=["status"])
        self.act("place")
        self.assertFalse(self.payment.needsAttention)
        self.assertEqual(self.payment.orders.get().items.get().listing, flat)

    def test_failed_placement_rolls_back(self):

        def partialPlacement(*args, **kwargs):
            order = makeOrder(self.seller, self.product)
            order.payment = self.payment
            order.save(update_fields=["payment"])
            raise IntegrityError

        with patch("sellerpanel.billing.addOrders", side_effect=partialPlacement):
            self.act("place")
        self.assertTrue(self.payment.needsAttention)
        self.assertFalse(Order.objects.exists())
        self.assertIn("could not be placed", AuditLog.objects.get().detail)

    def test_check_stripe_finalises_paid(self):
        self.payment.status = "pending"
        self.payment.save(update_fields=["status"])
        with patch("sellerpanel.stripepay.requests.get", return_value=self.sessionReply()):
            self.act("check")
        self.assertEqual(self.payment.status, "paid")
        self.assertIsNotNone(self.payment.paidAt)
        self.assertFalse(self.payment.needsAttention)
        self.assertEqual(self.payment.snapshot["providerStatus"], "complete / paid")
        self.assertEqual(self.payment.orders.get().items.get().name, "Floor plan")
        self.assertEqual(AuditLog.objects.get().action, "payment checked")
        self.assertEqual(ActivityNotice.objects.get().user, self.seller)

    def stalePayment(self):
        self.payment.status = "pending"
        self.payment.needsAttention = False
        self.payment.createdAt = timezone.now() - timedelta(minutes=31)
        self.payment.save()

    def test_attention_page_action_include(self):
        self.payment.status = "pending"
        self.payment.needsAttention = False
        self.payment.save()
        page = reverse("admin_payments")
        self.assertNotContains(self.client.get(page), f'id="payment-{self.payment.pk}"')
        with patch("sellerpanel.stripepay.requests.get") as get:
            self.act("check")
        get.assert_not_called()
        self.stalePayment()
        self.assertContains(self.client.get(page), f'id="payment-{self.payment.pk}"')
        self.payment.method = "demo"
        self.payment.save()
        self.assertNotContains(self.client.get(page), f'id="payment-{self.payment.pk}"')

    def test_page_says_what_automatic(self):
        from sellerpanel.reconciliation import AUTO_RECONCILE_KEY, resourceCache

        self.stalePayment()
        resourceCache.delete(AUTO_RECONCILE_KEY)
        with patch("sellerpanel.stripepay.requests.get", return_value=self.sessionReply()):
            response = self.client.get(reverse("admin_payments"), follow=True)
        notes = [str(note) for note in response.context["messages"]]
        self.assertTrue(
            any((note.startswith(f"Checked with Stripe. Payment #{self.payment.pk}: ") for note in notes)),
            notes,
        )

    def test_stale_paid_no_browser(self):
        self.stalePayment()
        with patch("sellerpanel.stripepay.requests.get", return_value=self.sessionReply()) as get:
            self.act("check")
            self.act("check")
        get.assert_called_once()
        self.assertEqual((self.payment.status, self.payment.needsAttention), ("paid", False))
        self.assertEqual(self.payment.orders.count(), 1)

    def test_stale_blocks_placed_becomes(self):
        self.stalePayment()
        self.seller.is_active = False
        self.seller.save()
        with patch("sellerpanel.stripepay.requests.get", return_value=self.sessionReply()):
            self.act("check")
        self.assertEqual((self.payment.status, self.payment.needsAttention), ("paid", True))
        self.assertIn("active seller", self.payment.snapshot["attentionReason"])
        self.assertFalse(Order.objects.exists())

    def test_reconcile_command_recovers(
        self,
    ):
        self.stalePayment()
        for token, changes in (
            ("recent", {}),
            ("demo", {"method": "demo", "createdAt": self.payment.createdAt}),
        ):
            attentionPayment(
                self.seller,
                self.product,
                checkoutToken=token,
                providerSessionId="cs_test_" + token,
                status="pending",
                needsAttention=False,
                **changes,
            )
        output = io.StringIO()
        with patch("sellerpanel.stripepay.requests.get", return_value=self.sessionReply()) as get:
            call_command("reconcile_payments", stdout=output)
            call_command("reconcile_payments", stdout=output)
        get.assert_called_once()
        self.payment.refresh_from_db()
        self.assertEqual((self.payment.status, self.payment.needsAttention), ("paid", False))
        self.assertEqual(self.payment.orders.count(), 1)
        self.assertEqual(Payment.objects.filter(status="pending").count(), 2)
        self.assertIn("Checked 1 stale pending Stripe payments.", output.getvalue())
        self.assertIn("Checked 0 stale pending Stripe payments.", output.getvalue())

    def test_reconcile_command_leaves(self):
        self.stalePayment()
        for reply in (
            self.sessionReply(amount_total=1),
            self.sessionReply(status="open", payment_status="unpaid"),
        ):
            with patch("sellerpanel.stripepay.requests.get", return_value=reply):
                call_command("reconcile_payments", stdout=io.StringIO())
            self.payment.refresh_from_db()
            self.assertEqual(self.payment.status, "pending")
        with patch("sellerpanel.stripepay.requests.get", side_effect=requests.Timeout):
            call_command("reconcile_payments", stdout=io.StringIO())
        self.assertContains(self.client.get(reverse("admin_payments")), f'id="payment-{self.payment.pk}"')
        self.assertFalse(Order.objects.exists())

    def test_reconcile_command_resolves(self):
        self.stalePayment()
        with patch(
            "sellerpanel.stripepay.requests.get",
            return_value=self.sessionReply(status="expired", payment_status="unpaid"),
        ):
            call_command("reconcile_payments", stdout=io.StringIO())
        self.payment.refresh_from_db()
        self.assertEqual((self.payment.status, self.payment.needsAttention), ("failed", False))

    def test_check_stripe_not_place(self):
        Refund.objects.create(payment=self.payment, amount=self.payment.amount, staff=self.staff)
        with patch("sellerpanel.stripepay.requests.get", return_value=self.sessionReply()):
            self.act("check")
        self.assertTrue(self.payment.needsAttention)
        self.assertFalse(Order.objects.exists())

    def test_check_stripe_keeps_partial(
        self,
    ):
        order = makeOrder(self.seller, self.product)
        order.payment = self.payment
        order.save()
        self.payment.status = "partly_refunded"
        self.payment.save()
        with patch("sellerpanel.stripepay.requests.get", return_value=self.sessionReply()):
            self.act("check")
        self.assertEqual((self.payment.status, self.payment.needsAttention), ("partly_refunded", False))
        self.assertEqual(self.payment.orders.count(), 1)

    def test_check_stripe_expired_unpaid(self):
        self.payment.status = "pending"
        self.payment.paymentIntentId = ""
        self.payment.save()
        with patch(
            "sellerpanel.stripepay.requests.get",
            return_value=self.sessionReply(status="expired", payment_status="unpaid", payment_intent=None),
        ):
            self.act("check")
        self.assertEqual((self.payment.status, self.payment.needsAttention), ("failed", False))

    def test_unpaid_checkout_explains_what(self):
        self.payment.status = "pending"
        self.payment.save()
        response = self.client.get(reverse("admin_payments"))
        for text in (
            "What happened",
            "opened the Stripe payment page but hasn’t paid",
            "Close as unpaid",
        ):
            self.assertContains(response, text)
        self.assertNotContains(response, "Create order")

    def test_close_as_unpaid_expires(self):
        self.payment.status = "pending"
        self.payment.paymentIntentId = ""
        self.payment.save()
        response, post = self.act("close", return_value=stripe_reply({"status": "expired"}))
        self.assertIn("/expire", post.call_args.args[0])
        self.assertEqual((self.payment.status, self.payment.needsAttention), ("failed", False))
        self.assertContains(response, "closed as unpaid")
        self.assertTrue(AuditLog.objects.filter(action="payment closed").exists())
        self.assertTrue(
            ActivityNotice.objects.filter(user=self.seller, text__contains="No money was taken").exists()
        )

    def test_close_refused_stripe_will(self):
        self.payment.status = "pending"
        self.payment.save()
        response, post = self.act(
            "close", return_value=stripe_reply({"error": {"message": "already complete"}})
        )
        self.assertEqual(self.payment.status, "pending")
        self.assertContains(response, "Stripe would not close this checkout")

    def test_paid_blocks_closed(self):
        response, post = self.act("close")
        post.assert_not_called()
        self.assertEqual(self.payment.status, "paid")

    def test_unchanged_check_not_notify(self):
        self.payment.status = "pending"
        self.payment.paymentIntentId = ""
        self.payment.save()
        with patch(
            "sellerpanel.stripepay.requests.get",
            return_value=self.sessionReply(status="open", payment_status="unpaid", payment_intent=None),
        ):
            self.act("check")
            self.act("check")
        self.assertEqual(self.payment.status, "pending")
        self.assertFalse(ActivityNotice.objects.filter(user=self.seller).exists())
        self.assertEqual(AuditLog.objects.filter(action="payment checked").count(), 2)

    def test_check_stripe_blocks_mismatch(self):
        self.payment.status = "pending"
        self.payment.save(update_fields=["status"])
        replies = (
            self.sessionReply(amount_total=1),
            self.sessionReply(livemode=True),
            self.sessionReply(metadata={"seller": "999"}),
            self.sessionReply(currency="usd"),
            self.sessionReply(payment_intent="pi_other"),
        )
        for reply in replies:
            with patch("sellerpanel.stripepay.requests.get", return_value=reply):
                self.act("check")
            self.assertEqual(self.payment.status, "pending")
            self.assertTrue(self.payment.needsAttention)
        with patch("sellerpanel.stripepay.requests.get", side_effect=requests.Timeout):
            self.act("check")
        self.assertFalse(Order.objects.exists())

    def test_check_intent_session_id(self):
        self.payment.providerSessionId = None
        self.payment.status = "pending"
        self.payment.save()
        reply = stripe_reply(
            {
                "id": "pi_test_attention",
                "livemode": False,
                "amount": 4000,
                "amount_received": 4000,
                "currency": "sgd",
                "status": "succeeded",
            }
        )
        with patch("sellerpanel.stripepay.requests.get", return_value=reply) as get:
            self.act("check")
        self.assertTrue(get.call_args.args[0].endswith("/payment_intents/pi_test_attention"))
        self.assertEqual(self.payment.status, "paid")

    def test_full_stripe_refund_records(self):
        response, post = self.act("refund", return_value=self.refundReply())
        self.assertContains(
            response,
            f"Payment #{self.payment.pk} from attentionseller@test.com was refunded in full ($40.00).",
        )
        self.assertNotContains(response, "We couldn’t place your order")
        refund = Refund.objects.get()
        self.assertIsNone(refund.order)
        self.assertEqual(
            (refund.payment, refund.amount, refund.staff, refund.status),
            (self.payment, self.payment.amount, self.staff, "succeeded"),
        )
        self.assertEqual((self.payment.status, self.payment.needsAttention), ("refunded", False))
        self.assertEqual(post.call_args.kwargs["data"]["amount"], 4000)
        self.assertFalse(Order.objects.exists())
        self.assertEqual(ActivityNotice.objects.get().user, self.seller)
        self.assertTrue(AuditLog.objects.filter(action="payment refund").exists())
        _, repeat = self.act("refund")
        repeat.assert_not_called()
        self.assertEqual(Refund.objects.count(), 1)

    def test_demo_refund_is_local(self):
        self.payment.method = "demo"
        self.payment.save(update_fields=["method"])
        _, post = self.act("refund")
        post.assert_not_called()
        self.assertEqual(self.payment.status, "refunded")

    def test_pending_refund_blocks_placement(self):
        _, first = self.act("refund", side_effect=requests.Timeout)
        self.act("place")
        self.assertTrue(self.payment.needsAttention)
        self.assertFalse(Order.objects.exists())
        _, second = self.act("refund", return_value=self.refundReply())
        self.assertEqual(
            first.call_args.kwargs["headers"]["Idempotency-Key"],
            second.call_args.kwargs["headers"]["Idempotency-Key"],
        )
        self.assertEqual(Refund.objects.count(), 1)
        self.assertEqual(self.payment.status, "refunded")

    def test_pending_provider_refund_checked(self):
        self.act("refund", return_value=self.refundReply(status="pending"))
        with patch("sellerpanel.stripepay.requests.get", return_value=self.refundReply(amount=1)):
            _, post = self.act("refund")
        post.assert_not_called()
        self.assertTrue(self.payment.needsAttention)
        self.assertEqual(Refund.objects.get().status, "pending")
        with patch("sellerpanel.stripepay.requests.get", return_value=self.refundReply()):
            self.act("refund")
        self.assertFalse(self.payment.needsAttention)

    def test_failed_refund_retried_new(self):
        self.act("refund", return_value=self.refundReply(status="failed"))
        self.assertTrue(self.payment.needsAttention)
        self.act("refund", return_value=self.refundReply(id="re_second_attention"))
        self.assertEqual(
            list(Refund.objects.order_by("pk").values_list("status", flat=True)),
            ["failed", "succeeded"],
        )

    def test_unplaced_refund_blocks_orders(self):
        order = makeOrder(self.seller, self.product)
        order.payment = self.payment
        order.save(update_fields=["payment"])
        _, post = self.act("refund")
        post.assert_not_called()
        order.delete()
        self.payment.status = "pending"
        self.payment.save(update_fields=["status"])
        _, post = self.act("refund")
        post.assert_not_called()
        self.assertFalse(Refund.objects.exists())

    @override_settings(STRIPE_SECRET_KEY="sk_live_forbidden")
    def test_live_key_never_calls(self):
        with patch("sellerpanel.stripepay.requests.get") as get:
            self.act("check")
        _, post = self.act("refund")
        get.assert_not_called()
        post.assert_not_called()
        self.assertTrue(self.payment.needsAttention)
        self.assertEqual(self.payment.status, "paid")


@override_settings(STRIPE_SECRET_KEY="sk_test_transactions")
class PaymentResolutionTransactionTests(TransactionTestCase):

    def test_staff_check_refund_http(self):
        staff = makeStaff("attentiontransactionstaff@test.com")
        seller = makeUser("attentiontransactionseller@test.com", "seller")
        product = makeProduct("Floor plan", "addon", 40, None)
        payment = attentionPayment(seller, product)
        self.client.force_login(staff)
        url = reverse("admin_payment_action", args=[payment.pk])

        def check(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            return stripe_reply(
                {
                    "id": payment.providerSessionId,
                    "livemode": False,
                    "mode": "payment",
                    "client_reference_id": payment.checkoutToken,
                    "amount_total": 4000,
                    "currency": "sgd",
                    "metadata": {"seller": str(seller.pk)},
                    "status": "complete",
                    "payment_status": "paid",
                    "payment_intent": payment.paymentIntentId,
                }
            )

        with patch("sellerpanel.stripepay.requests.get", side_effect=check):
            self.client.post(url, {"action": "check"})
        self.assertEqual(payment.orders.count(), 1)
        payment = attentionPayment(
            seller,
            product,
            checkoutToken="refund-transaction",
            providerSessionId="cs_test_refund_transaction",
        )
        url = reverse("admin_payment_action", args=[payment.pk])

        def refund(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(Refund.objects.get().status, "pending")
            return stripe_reply(
                {
                    "id": "re_test_transaction",
                    "payment_intent": payment.paymentIntentId,
                    "amount": 4000,
                    "currency": "sgd",
                    "status": "succeeded",
                }
            )

        with patch("sellerpanel.stripepay.requests.post", side_effect=refund):
            self.client.post(url, {"action": "refund"})
        payment.refresh_from_db()
        self.assertEqual(payment.status, "refunded")
