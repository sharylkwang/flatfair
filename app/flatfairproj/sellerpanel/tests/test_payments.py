from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.messages import get_messages
from django.core.cache import caches
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from adminpanel.models import Ticket
from sellerpanel.billing import cartGroups, payCopy
from sellerpanel.tests.helpers import CARD, stripe_reply
from storefront.models import Listing, Order, Payment, Product
from storefront.tests.helpers import makeListing, makeOrder, makeProduct, makeStaff, makeUser


@override_settings(STRIPE_SECRET_KEY="sk_test_payments")
class PaymentTests(TestCase):

    def setUp(self):
        self.seller = makeUser("payments@test.com", "seller")
        self.flat = makeListing(self.seller)
        self.package = makeProduct("Boost 30 days", price=49, days=30)
        self.service = makeProduct("Pro photography", "addon", 180, None)
        self.client.force_login(self.seller)
        for product in (self.package, self.service):
            self.client.post(reverse("cart_add", args=[product.pk]))
        self.token = self.client.get(reverse("cart")).context["checkoutToken"]

    def start(self):
        reply = stripe_reply({"id": "cs_test_payment", "url": "https://checkout.stripe.com/c/pay/payment"})
        with patch("sellerpanel.stripepay.requests.post", return_value=reply):
            return self.client.post(
                reverse("checkout"),
                {"checkoutToken": self.token, "listing": self.flat.publicId, "pay": "yes", **CARD},
            )

    def finish(self, **changes):
        data = {
            "id": "cs_test_payment",
            "payment_intent": "pi_test_payment",
            "mode": "payment",
            "status": "complete",
            "livemode": False,
            "payment_status": "paid",
            "amount_total": 24961,
            "currency": "sgd",
            "client_reference_id": self.token,
            "metadata": {"seller": str(self.seller.pk), "listing": str(self.flat.publicId)},
        }
        data.update(changes)
        with patch("sellerpanel.stripepay.requests.get", return_value=stripe_reply(data)):
            return self.client.get(reverse("stripe_return"), {"session_id": "cs_test_payment"})

    def test_pending_stores_session_snapshot(self):
        self.start()
        payment = Payment.objects.get()
        self.assertEqual(
            (payment.method, payment.status, payment.amount, payment.currency),
            ("stripe", "pending", Decimal("249.61"), "sgd"),
        )
        self.assertEqual(payment.providerSessionId, "cs_test_payment")
        self.assertEqual(payment.snapshot["listingId"], self.flat.pk)
        self.assertEqual(Decimal(payment.snapshot["groups"][0]["items"][0]["price"]), Decimal("49.00"))
        self.assertFalse(Order.objects.exists())
        self.assertIsNone(payment.paidAt)

    def test_paid_return_original_snapshot(self):
        self.start()
        self.client.post(reverse("cart_remove", args=[self.service.pk]))
        self.client.get(reverse("cart"))
        Product.objects.filter(pk=self.package.pk).update(
            name="Changed", price=999, durationDays=1, kind="addon", isActive=False
        )
        Product.objects.filter(pk=self.service.pk).update(price=1, isActive=False)
        self.finish()
        self.finish()
        payment = Payment.objects.get()
        package, service = payment.orders.order_by("pk")
        self.assertEqual((payment.status, payment.paymentIntentId), ("paid", "pi_test_payment"))
        self.assertIsNotNone(payment.paidAt)
        self.assertEqual((package.total, service.total), (Decimal("53.41"), Decimal("196.20")))
        item = package.items.get()
        self.assertEqual(
            (item.name, item.kind, item.durationDays, item.listing),
            ("Boost 30 days", "package", 30, self.flat),
        )
        self.assertEqual(self.client.session["cart"], [self.package.pk])
        self.assertEqual(Order.objects.count(), 2)
        self.assertFalse(payment.needsAttention)

    def test_paid_return_survives_missing(self):
        self.start()
        session = self.client.session
        for key in ("cart", "checkoutToken", "cartSnapshot", "cartListing"):
            session.pop(key, None)
        session.save()
        self.finish()
        self.assertEqual(Payment.objects.get().orders.count(), 2)

    def test_deleted_product_keeps_paid(self):
        self.start()
        self.service.delete()
        self.finish()
        payment = Payment.objects.get()
        self.assertEqual((payment.status, payment.needsAttention), ("paid", True))
        self.assertFalse(Order.objects.exists())
        self.client.force_login(makeStaff("staffpayments@test.com"))
        response = self.client.get(reverse("admin_orders"))
        for check_text_1 in ("Payments needing attention", "cs_test_payment", "Pro photography"):
            self.assertContains(response, check_text_1)

    def test_order_database_failure_keeps(self):
        self.start()
        with patch("sellerpanel.billing.addOrders", side_effect=IntegrityError):
            self.finish()
        payment = Payment.objects.get()
        self.assertEqual((payment.status, payment.needsAttention), ("paid", True))
        self.assertIsNotNone(payment.paidAt)
        self.assertFalse(Order.objects.exists())
        self.finish()
        payment.refresh_from_db()
        self.assertFalse(payment.needsAttention)
        self.assertEqual(payment.orders.count(), 2)

    def test_deleted_listing_flags_paid(self):
        self.start()
        self.flat.delete()
        self.finish()
        self.assertTrue(Payment.objects.get().needsAttention)
        self.assertEqual(Payment.objects.get().status, "paid")
        self.assertFalse(Order.objects.exists())

    def test_return_recovers_session_id(self):
        self.start()
        Payment.objects.update(providerSessionId=None)
        self.finish()
        self.assertEqual(Payment.objects.get().providerSessionId, "cs_test_payment")
        self.assertEqual(Order.objects.count(), 2)

    def test_sold_hidden_reassigned_listing(self):
        self.start()
        other = makeUser("otherpayments@test.com", "seller")
        for changes in (
            {"status": "sold"},
            {"status": "hidden"},
            {"status": "live", "seller": other},
        ):
            with self.subTest(changes=changes):
                Listing.objects.filter(pk=self.flat.pk).update(**changes)
                self.finish()
                payment = Payment.objects.get()
                self.assertTrue(payment.needsAttention)
                self.assertFalse(payment.orders.exists())
                self.assertIn("listing", payment.snapshot["attentionReason"])

    def test_suspended_seller_blocks_place(self):
        self.start()

        def suspend(*args, **kwargs):
            self.seller.is_active = False
            self.seller.save(update_fields=["is_active"])
            return stripe_reply(
                {
                    "id": "cs_test_payment",
                    "payment_intent": "pi_test_payment",
                    "mode": "payment",
                    "status": "complete",
                    "livemode": False,
                    "payment_status": "paid",
                    "amount_total": 24961,
                    "currency": "sgd",
                    "client_reference_id": self.token,
                    "metadata": {"seller": str(self.seller.pk), "listing": str(self.flat.publicId)},
                }
            )

        with patch("sellerpanel.stripepay.requests.get", side_effect=suspend):
            self.client.get(reverse("stripe_return"), {"session_id": "cs_test_payment"})
        payment = Payment.objects.get()
        self.assertTrue(payment.needsAttention)
        self.assertFalse(payment.orders.exists())
        self.assertIn("active seller", payment.snapshot["attentionReason"])

    def test_package_bought_stripe_open(self):
        self.start()
        for product in (self.package, makeProduct("Top spot", price=299, days=None)):
            with self.subTest(product=product.name):
                other = makeOrder(self.seller, product, "completed", self.flat)
                self.finish()
                payment = Payment.objects.get()
                self.assertTrue(payment.needsAttention)
                self.assertFalse(payment.orders.exists())
                self.assertIn("already", payment.snapshot["attentionReason"])
                other.delete()

    def test_snapshot_package_tier_survives(self):
        self.start()
        Product.objects.filter(pk=self.package.pk).update(tier=3)
        self.finish()
        self.assertEqual(Payment.objects.get().orders.count(), 2)

    def test_return_unplaced_refund_not(self):
        self.start()
        Payment.objects.update(status="refunded", needsAttention=False)
        self.finish()
        payment = Payment.objects.get()
        self.assertEqual((payment.status, payment.needsAttention), ("refunded", False))
        self.assertFalse(Order.objects.exists())

    def test_session_intent_mismatch_blocks(self):
        self.start()
        Payment.objects.update(providerSessionId="cs_other")
        self.finish()
        Payment.objects.update(providerSessionId="cs_test_payment")
        for intent in (None, [], "bad"):
            self.finish(payment_intent=intent)
        self.assertFalse(Order.objects.exists())
        self.assertEqual(Payment.objects.get().status, "pending")

    def test_paid_return_repeat_start(self):
        self.start()
        self.start()
        self.finish()
        self.finish()
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Order.objects.count(), 2)
        payment = Payment.objects.get()
        order = payment.orders.first()
        self.assertContains(self.client.get(reverse("receipt", args=[order.pk])), "Stripe (test) · Paid")
        self.client.force_login(makeStaff("staffreceipt@test.com"))
        self.assertContains(self.client.get(reverse("admin_order", args=[order.pk])), "Stripe (test) · Paid")

    @override_settings(STRIPE_SECRET_KEY="")
    def test_demo_stores_brand_last(self):
        with patch("sellerpanel.stripepay.requests.post") as post:
            self.client.post(
                reverse("checkout"),
                {"checkoutToken": self.token, "listing": self.flat.publicId, "pay": "yes", **CARD},
            )
        post.assert_not_called()
        payment = Payment.objects.get()
        self.assertEqual(
            (payment.method, payment.status, payment.cardBrand, payment.cardLast4),
            ("demo", "paid", "Visa", "4242"),
        )
        self.assertIsNone(payment.providerSessionId)
        self.assertEqual(payment.paymentIntentId, "")
        self.assertNotIn("4242424242424242", str(payment.snapshot))
        self.assertNotIn("cvc", str(payment.snapshot))
        self.assertEqual(payment.orders.count(), 2)
        self.assertContains(
            self.client.get(reverse("receipt", args=[payment.orders.first().pk])),
            "Visa ending 4242",
        )


@override_settings(STRIPE_SECRET_KEY="sk_test_transactions")
class PaymentTransactionTests(TransactionTestCase):

    def test_checkout_verification_http_run(self):
        seller = makeUser("transaction@test.com", "seller")
        product = makeProduct("Floor plan", "addon", 40, None)
        self.client.force_login(seller)
        self.client.post(reverse("cart_add", args=[product.pk]))
        token = self.client.get(reverse("cart")).context["checkoutToken"]

        def start(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(Payment.objects.get().status, "pending")
            return stripe_reply(
                {
                    "id": "cs_test_transaction",
                    "url": "https://checkout.stripe.com/c/pay/transaction",
                }
            )

        with patch("sellerpanel.stripepay.requests.post", side_effect=start):
            self.client.post(reverse("checkout"), {"checkoutToken": token, "pay": "yes"})

        def finish(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            return stripe_reply(
                {
                    "id": "cs_test_transaction",
                    "payment_intent": "pi_test_transaction",
                    "mode": "payment",
                    "status": "complete",
                    "livemode": False,
                    "payment_status": "paid",
                    "amount_total": 4360,
                    "currency": "sgd",
                    "client_reference_id": token,
                    "metadata": {"seller": str(seller.pk)},
                }
            )

        with patch("sellerpanel.stripepay.requests.get", side_effect=finish):
            self.client.get(reverse("stripe_return"), {"session_id": "cs_test_transaction"})
        self.assertEqual(Order.objects.get().payment.status, "paid")


@override_settings(STRIPE_SECRET_KEY="sk_test_progress")
class SellerPaymentProgressTests(TestCase):

    def setUp(self):
        caches["resources"].clear()
        self.seller = makeUser("progress@test.com", "seller")
        self.other = makeUser("otherprogress@test.com", "seller")
        self.product = makeProduct("Floor plan", "addon", 40, None)
        self.client.force_login(self.seller)
        provider = patch(
            "sellerpanel.stripepay.requests.get",
            side_effect=AssertionError("Unexpected provider check"),
        )
        self.providerGet = provider.start()
        self.addCleanup(provider.stop)

    def makePayment(self, **changes):
        number = Payment.objects.count() + 1
        data = {
            "seller": self.seller,
            "method": "stripe",
            "amount": "43.60",
            "checkoutToken": f"progress-{number}",
            "providerSessionId": f"cs_test_progress_{number}",
            "snapshot": payCopy(cartGroups([self.product]), None),
        }
        return Payment.objects.create(**{**data, **changes})

    def providerReply(self, payment, **changes):
        data = {
            "id": payment.providerSessionId,
            "livemode": False,
            "mode": "payment",
            "client_reference_id": payment.checkoutToken,
            "amount_total": 4360,
            "currency": "sgd",
            "metadata": {"seller": str(payment.seller_id)},
            "status": "complete",
            "payment_status": "paid",
            "payment_intent": "pi_test_progress",
        }
        self.providerGet.side_effect = None
        self.providerGet.return_value = stripe_reply({**data, **changes})

    def test_section_hidden_no_unplaced(self):
        self.assertNotContains(self.client.get(reverse("my_orders")), "Payments in progress")
        self.providerGet.assert_not_called()

    def test_pending_shows_no_secrets(self):
        payment = self.makePayment(
            paymentIntentId="pi_private",
            snapshot={
                **payCopy(cartGroups([self.product]), None),
                "attentionReason": "private diagnostic",
            },
        )
        Product.objects.filter(pk=self.product.pk).update(name="Changed catalogue name")
        response = self.client.get(reverse("my_orders"))
        for text in (
            "Payments in progress",
            "Floor plan",
            "$43.60",
            "Total with GST",
            payment.reference,
            timezone.localtime(payment.createdAt).strftime("%-d %b %Y"),
            "We’re waiting for the payment provider and will check again automatically.",
            "Check again",
            "Contact support",
        ):
            self.assertContains(response, text)
        self.assertContains(response, reverse("check_payment", args=[payment.pk]))
        self.assertContains(response, f"?topic=orders&amp;payment={payment.pk}#contact-support")
        for text in (
            payment.providerSessionId,
            payment.paymentIntentId,
            payment.checkoutToken,
            "private diagnostic",
            "sk_test_progress",
            "Changed catalogue name",
        ):
            self.assertNotContains(response, text)
        self.providerGet.assert_not_called()

    def test_paid_unplaced_staff_recovery(self):
        self.makePayment(status="paid", needsAttention=True, createdAt=timezone.now() - timedelta(days=31))
        response = self.client.get(reverse("my_orders"))
        self.assertContains(
            response,
            "Sorry, you’ve paid but your order wasn’t created. Our team knows and will fix it or refund you.",
        )
        self.assertNotContains(response, "Check again")

    def test_failed_payment_not_charged(self):
        self.makePayment(status="failed")
        response = self.client.get(reverse("my_orders"))
        self.assertContains(response, "This payment didn’t go through. Nothing was charged.")
        self.assertNotContains(response, "Check again")

    def test_old_failed_refunded_placed(self):
        self.makePayment(status="failed", createdAt=timezone.now() - timedelta(days=31))
        self.makePayment(status="refunded")
        payment = self.makePayment(status="paid")
        order = makeOrder(self.seller, self.product)
        order.payment = payment
        order.save(update_fields=["payment"])
        self.assertNotContains(self.client.get(reverse("my_orders")), "Payments in progress")

    def test_check_creates_one_order(self):
        payment = self.makePayment()
        untouched = self.makePayment()
        self.providerReply(payment)
        url = reverse("check_payment", args=[payment.pk])
        response = self.client.post(url)
        order = Order.objects.get(payment=payment)
        self.assertRedirects(response, reverse("receipt", args=[order.pk]))
        self.assertIn(
            f"Thank you. Order #{order.pk} has been placed.",
            [str(message) for message in get_messages(response.wsgi_request)],
        )
        self.assertRedirects(self.client.post(url), reverse("receipt", args=[order.pk]))
        self.assertEqual(payment.orders.count(), 1)
        self.providerGet.assert_called_once()
        payment.refresh_from_db()
        untouched.refresh_from_db()
        self.assertEqual((payment.status, payment.needsAttention), ("paid", False))
        self.assertEqual(untouched.status, "pending")
        self.assertFalse(untouched.orders.exists())

    def test_check_blocks_place_unverified(self):
        payment = self.makePayment()
        self.providerReply(payment, amount_total=1)
        self.assertRedirects(
            self.client.post(reverse("check_payment", args=[payment.pk])), reverse("my_orders")
        )
        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertFalse(Order.objects.exists())

    def test_expired_becomes_failed_missing(self):
        payment = self.makePayment()
        self.providerReply(payment, status="expired", payment_status="unpaid", payment_intent=None)
        response = self.client.post(reverse("check_payment", args=[payment.pk]), follow=True)
        self.assertContains(response, "This payment didn’t go through. Nothing was charged.")
        payment.refresh_from_db()
        self.assertEqual(payment.status, "failed")
        self.assertFalse(Order.objects.exists())

    def test_check_rate_limit_applies(self):
        first = self.makePayment()
        second = self.makePayment()
        self.providerReply(first, status="open", payment_status="unpaid", payment_intent=None)
        self.client.post(reverse("check_payment", args=[first.pk]))
        for payment in (first, second):
            response = self.client.post(reverse("check_payment", args=[payment.pk]), follow=True)
            self.assertContains(response, "Please wait a minute before checking again.")
        self.providerGet.assert_called_once()
        self.assertFalse(Order.objects.exists())

    def test_other_blocks_see_check(self):
        payment = self.makePayment()
        self.client.force_login(self.other)
        response = self.client.get(reverse("my_orders"))
        self.assertNotContains(response, "Payments in progress")
        self.assertNotContains(response, payment.reference)
        self.assertEqual(self.client.post(reverse("check_payment", args=[payment.pk])).status_code, 404)
        self.assertEqual(
            self.client.get(reverse("support"), {"topic": "orders", "payment": payment.pk}).status_code,
            404,
        )
        self.providerGet.assert_not_called()

    def test_check_requires_post_csrf(self):
        payment = self.makePayment()
        url = reverse("check_payment", args=[payment.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.seller)
        response = client.get(reverse("my_orders"))
        self.assertContains(response, "csrfmiddlewaretoken")
        self.assertEqual(client.post(url).status_code, 403)
        self.providerReply(payment)
        response = client.post(url, {"csrfmiddlewaretoken": client.cookies["csrftoken"].value})
        self.assertRedirects(response, reverse("receipt", args=[payment.orders.get().pk]))

    def test_support_form_prefills_saves(self):
        payment = self.makePayment()
        response = self.client.get(reverse("support"), {"topic": "orders", "payment": payment.pk})
        form = response.context["ticketForm"]
        self.assertEqual(form["topic"].value(), "orders")
        self.assertEqual(form["message"].value(), f"Please help me with payment {payment.reference}.")
        self.client.post(reverse("support"), {"topic": "orders", "message": form["message"].value()})
        self.assertEqual(Ticket.objects.get().message, form["message"].value())


@override_settings(STRIPE_SECRET_KEY="sk_test_command")
class ReconcileCommandTests(TestCase):

    def setUp(self):
        self.seller = makeUser("command@test.com", "seller")
        product = makeProduct("Floor plan", "addon", 40, None)
        self.payment = Payment.objects.create(
            seller=self.seller,
            method="stripe",
            status="pending",
            amount=40,
            checkoutToken="command-checkout",
            providerSessionId="cs_test_command",
            snapshot=payCopy(
                [{"subtotal": 40, "gst": 0, "total": 40, "status": "paid", "items": [product]}],
                None,
            ),
            createdAt=timezone.now() - timedelta(minutes=31),
        )
        self.reply = stripe_reply(
            {
                "id": "cs_test_command",
                "livemode": False,
                "mode": "payment",
                "client_reference_id": "command-checkout",
                "amount_total": 4000,
                "currency": "sgd",
                "metadata": {"seller": str(self.seller.pk)},
                "status": "complete",
                "payment_status": "paid",
                "payment_intent": "pi_test_command",
            }
        )

    def test_default_runs_once_retries(self):
        output = StringIO()
        with (
            patch("sellerpanel.stripepay.requests.get", return_value=self.reply) as get,
            patch("sellerpanel.management.commands.reconcile_payments.time.sleep") as sleep,
        ):
            call_command("reconcile_payments", stdout=output)
            call_command("reconcile_payments", stdout=output)
        get.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(self.payment.orders.count(), 1)
        self.assertIn("Checked 1 stale pending Stripe payments.", output.getvalue())
        self.assertIn("Checked 0 stale pending Stripe payments.", output.getvalue())

    def test_loop_sleeps_between_runs(self):
        output = StringIO()
        with (
            patch("sellerpanel.stripepay.requests.get", return_value=self.reply) as get,
            patch(
                "sellerpanel.management.commands.reconcile_payments.time.sleep",
                side_effect=[None, KeyboardInterrupt],
            ) as sleep,
        ):
            call_command("reconcile_payments", "--loop", "10", stdout=output)
        self.assertEqual(sleep.call_args_list, [((10.0,),), ((10.0,),)])
        get.assert_called_once()
        self.assertEqual(self.payment.orders.count(), 1)
        self.assertIn("Checked 0 stale pending Stripe payments.", output.getvalue())
        self.assertIn("Payment reconciliation stopped.", output.getvalue())

    def test_invalid_loop_intervals_fail(self):
        with patch("sellerpanel.stripepay.requests.get") as get:
            for value in ("0", "-1", "nan", "inf", "invalid"):
                with self.subTest(value=value), self.assertRaises(CommandError):
                    call_command("reconcile_payments", "--loop", value, stdout=StringIO())
        get.assert_not_called()
