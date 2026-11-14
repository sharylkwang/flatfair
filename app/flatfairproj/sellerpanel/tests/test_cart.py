from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
import requests

from adminpanel.forms import ProductForm
from adminpanel.models import Ticket
from sellerpanel.billing import cartGroups
from sellerpanel.forms import CardForm, luhnValid
from sellerpanel.stripepay import MAX_PRODUCT_PRICE, MAX_PURCHASE_TOTAL, checkSums
from sellerpanel.tests.helpers import CARD, stripe_reply
from storefront.models import Order, Payment, Product, Review
from storefront.tests.helpers import makeListing, makeOrder, makeProduct, makeUser


@override_settings(STRIPE_SECRET_KEY="")
class CartTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.flat = makeListing(self.seller)
        self.boost = makeProduct("Boost 30 days", price=49, days=30)
        self.top = makeProduct("Top spot", price=299, days=None)
        self.photos = makeProduct("Pro photography", "addon", 180, None)
        self.client.force_login(self.seller)

    def add(self, product, **data):
        return self.client.post(reverse("cart_add", args=[product.pk]), data)

    def test_aggregate_overflow_refused(self):
        Product.objects.filter(pk__in=[self.boost.pk, self.photos.pk]).update(price=50000000)
        self.add(self.boost)
        self.add(self.photos)
        token = self.client.get(reverse("cart")).context["checkoutToken"]
        for key in ("", "sk_test_fake"):
            with (
                self.subTest(stripe=bool(key)),
                override_settings(STRIPE_SECRET_KEY=key),
                patch("sellerpanel.billing.newSession") as stripe,
            ):
                response = self.client.post(
                    reverse("checkout"),
                    {"checkoutToken": token, "listing": self.flat.publicId, "pay": "yes", **CARD},
                    follow=True,
                )
                self.assertContains(response, "Your cart total including GST is too high.")
                stripe.assert_not_called()
                self.assertFalse(Payment.objects.exists())
                self.assertFalse(Order.objects.exists())

    def test_gst_inclusive_boundary_allows(self):
        item = SimpleNamespace(price=Decimal("91743119.26"), kind="addon", durationDays=None)
        groups = cartGroups([item])
        self.assertEqual(groups[0]["total"], MAX_PURCHASE_TOTAL)
        checkSums(groups)
        item.price += Decimal("0.01")
        with self.assertRaisesMessage(ValidationError, "Your cart total including GST is too high."):
            checkSums(cartGroups([item]))

    def test_largest_whole_dollar_price(self):
        Product.objects.filter(pk=self.photos.pk).update(price=MAX_PRODUCT_PRICE)
        self.add(self.photos)
        token = self.client.get(reverse("cart")).context["checkoutToken"]
        self.client.post(reverse("checkout"), {"checkoutToken": token, "pay": "yes", **CARD})
        self.assertEqual(Payment.objects.get().amount, Decimal("99999999.71"))
        self.assertEqual(Order.objects.get().total, Decimal("99999999.71"))

    def test_product_form_caps_price(self):
        field = ProductForm.base_fields["price"]
        self.assertEqual(field.clean(str(MAX_PRODUCT_PRICE)), MAX_PRODUCT_PRICE)
        with self.assertRaisesMessage(ValidationError, "That price is too high once GST is added."):
            field.clean(str(MAX_PRODUCT_PRICE + 1))

    def test_totals_package_service_gst(self):
        self.add(self.boost)
        self.add(self.photos)
        response = self.client.get(reverse("cart"))
        self.assertEqual(
            [item.name for item in response.context["items"]], ["Boost 30 days", "Pro photography"]
        )
        self.assertEqual(response.context["subtotal"], Decimal("229"))
        self.assertEqual(response.context["gst"], Decimal("20.61"))
        self.assertEqual(response.context["selectedFlat"], self.flat)

    def test_adding_another_package_replaces(self):
        self.add(self.boost)
        self.add(self.top)
        self.assertEqual(self.client.session["cart"], [self.top.pk])
        self.client.post(reverse("cart_remove", args=[self.top.pk]))
        self.assertEqual(self.client.session["cart"], [])

    def test_lower_tier_inactive_packages(self):
        makeOrder(self.seller, self.top, listing=self.flat)
        self.add(self.boost, listing=str(self.flat.publicId))
        self.assertEqual(self.client.session.get("cart", []), [])
        self.boost.isActive = False
        self.boost.save()
        self.add(self.boost)
        self.assertEqual(self.client.session.get("cart", []), [])

    def test_checkout_confirms_payment(self):
        self.add(self.boost)
        token = self.client.get(reverse("cart")).context["checkoutToken"]
        response = self.client.post(
            reverse("checkout"), {"checkoutToken": token, "listing": self.flat.publicId}
        )
        self.assertTemplateUsed(response, "sellerpanel/payment.html")
        self.assertEqual(response.context["total"], Decimal("53.41"))
        self.assertFalse(Order.objects.exists())
        response = self.client.post(
            reverse("checkout"),
            {"checkoutToken": token, "listing": self.flat.publicId, "pay": "yes", **CARD},
        )
        order = Order.objects.get()
        self.assertEqual(response.url, reverse("my_orders") + f"#order-{order.pk}")
        self.assertEqual((order.total, order.items.get().listing), (Decimal("53.41"), self.flat))
        self.assertEqual(order.status, "completed")
        self.assertEqual(self.client.session["cart"], [])

    def test_repeating_paid_checkout_not(self):
        self.add(self.boost)
        token = self.client.get(reverse("cart")).context["checkoutToken"]
        data = {"checkoutToken": token, "listing": self.flat.publicId, "pay": "yes", **CARD}
        self.client.post(reverse("checkout"), data)
        self.client.post(reverse("checkout"), data)
        self.assertEqual(Order.objects.count(), 1)

    def test_price_change_requires(self):
        self.add(self.boost)
        token = self.client.get(reverse("cart")).context["checkoutToken"]
        Product.objects.filter(pk=self.boost.pk).update(price=59)
        response = self.client.post(
            reverse("checkout"),
            {"checkoutToken": token, "listing": self.flat.publicId, "pay": "yes", **CARD},
        )
        self.assertContains(response, "Your cart has changed. Please check the new total.")
        self.assertFalse(Order.objects.exists())

    def test_package_blocks_checked_out(self):
        self.flat.status = "sold"
        self.flat.save()
        self.add(self.boost)
        token = self.client.get(reverse("cart")).context["checkoutToken"]
        self.client.post(reverse("checkout"), {"checkoutToken": token, "pay": "yes", **CARD})
        self.assertFalse(Order.objects.exists())

    def test_service_blocks_invalid_listing(self):
        self.add(self.photos)
        token = self.client.get(reverse("cart")).context["checkoutToken"]
        self.client.post(
            reverse("checkout"), {"checkoutToken": token, "listing": "asdf", "pay": "yes", **CARD}
        )
        self.assertFalse(Order.objects.exists())
        self.client.post(reverse("checkout"), {"checkoutToken": token, "pay": "yes", **CARD})
        self.assertIsNone(Order.objects.get().items.get().listing)
        self.assertEqual(Order.objects.get().status, "paid")

    def test_invalid_checkout_token_blocks(self):
        response = self.client.post(reverse("checkout"), {"checkoutToken": "asdf"})
        self.assertContains(response, "Your cart has changed.")
        self.assertFalse(Order.objects.exists())

    def test_suggestions_exclude_purchased(self):
        plan = makeProduct("Floor plan", "addon", 40, None)
        makeOrder(self.seller, plan, status="completed")
        self.add(self.photos)
        response = self.client.get(reverse("cart"))
        self.assertEqual(list(response.context["suggested"]), [])
        self.assertFalse(response.context["needsFlat"])
        self.add(self.boost)
        self.assertTrue(self.client.get(reverse("cart")).context["needsFlat"])

    def test_page_takes_demo_card(self):
        self.add(self.photos)
        response = self.client.get(reverse("cart"))
        self.assertFalse(response.context["stripeOn"])
        for name in response.context["card"].fields:
            with self.subTest(field=name):
                self.assertContains(response, f'name="{name}"', count=1)
        self.assertContains(response, 'name="pay" value="yes"', count=1)
        self.assertContains(response, "$196.20", count=2)
        data = {"checkoutToken": response.context["checkoutToken"], "pay": "yes", **CARD}
        self.client.post(reverse("checkout"), data)
        self.assertEqual(Order.objects.get().status, "paid")

    def test_mixed_creates_package(self):
        self.add(self.boost)
        self.add(self.photos)
        token = self.client.get(reverse("cart")).context["checkoutToken"]
        data = {"checkoutToken": token, "listing": self.flat.publicId}
        response = self.client.post(reverse("checkout"), data)
        self.assertTrue(response.context["splitOrder"])
        self.assertContains(response, "two orders")
        self.client.post(reverse("checkout"), {**data, "pay": "yes", **CARD})
        self.client.post(reverse("checkout"), {**data, "pay": "yes", **CARD})
        package, service = Order.objects.order_by("pk")
        self.assertEqual(
            (package.status, package.total, package.items.get().kind),
            ("completed", Decimal("53.41"), "package"),
        )
        self.assertEqual(
            (service.status, service.total, service.items.get().kind),
            ("paid", Decimal("196.20"), "addon"),
        )
        self.assertTrue(service.canRefund())

    def test_service_checkout_not_show(self):
        self.add(self.photos)
        token = self.client.get(reverse("cart")).context["checkoutToken"]
        response = self.client.post(reverse("checkout"), {"checkoutToken": token})
        self.assertFalse(response.context["splitOrder"])
        self.assertNotContains(response, "two orders")

    def test_invalid_card_rejected_valid(self):
        self.add(self.boost)
        token = self.client.get(reverse("cart")).context["checkoutToken"]
        data = {"checkoutToken": token, "listing": self.flat.publicId, "pay": "yes", **CARD}
        data["number"] = "4242 4242 4242 4241"
        response = self.client.post(reverse("checkout"), data)
        self.assertTemplateUsed(response, "sellerpanel/payment.html")
        self.assertContains(response, "Please check for a typo")
        self.assertFalse(Order.objects.exists())
        data["number"] = "5555 5555 5555 4444"
        response = self.client.post(reverse("checkout"), data, follow=True)
        self.assertEqual(Order.objects.count(), 1)
        self.assertContains(response, "Mastercard ending 4444")


@override_settings(STRIPE_SECRET_KEY="sk_test_asdf")
class StripeTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.flat = makeListing(self.seller)
        self.boost = makeProduct("Boost 30 days", price=49, days=30)
        self.photos = makeProduct("Pro photography", "addon", 180, None)
        self.client.force_login(self.seller)
        self.client.post(reverse("cart_add", args=[self.boost.pk]))
        self.client.post(reverse("cart_add", args=[self.photos.pk]))
        self.token = self.client.get(reverse("cart")).context["checkoutToken"]

    def pay(self):
        data = {"checkoutToken": self.token, "listing": self.flat.publicId, "pay": "yes", **CARD}
        payment = Payment.objects.filter(checkoutToken=self.token).first()
        sessionId = (
            payment.providerSessionId
            if payment and payment.providerSessionId
            else f"cs_test_{Payment.objects.count() + 1}"
        )
        reply = stripe_reply({"id": sessionId, "url": f"https://checkout.stripe.com/c/pay/{sessionId}"})
        with patch("sellerpanel.stripepay.requests.post", return_value=reply) as post:
            response = self.client.post(reverse("checkout"), data)
        return (response, post)

    def back(self, **changes):
        session = {
            "id": "cs_test_1",
            "payment_intent": "pi_test_1",
            "mode": "payment",
            "status": "complete",
            "livemode": False,
            "payment_status": "paid",
            "amount_total": 24961,
            "currency": "sgd",
            "client_reference_id": self.token,
            "metadata": {"seller": str(self.seller.pk), "listing": str(self.flat.publicId)},
        }
        session.update(changes)
        with patch("sellerpanel.stripepay.requests.get", return_value=stripe_reply(session)):
            return self.client.get(reverse("stripe_return") + "?session_id=cs_test_1")

    def test_cart_page_offers_button(self):
        response = self.client.get(reverse("cart"))
        self.assertTrue(response.context["stripeOn"])
        self.assertContains(response, "Pay with Stripe (test)")
        self.assertNotContains(response, 'name="cvc"')

    def test_payment_page_shows_test(self):
        data = {"checkoutToken": self.token, "listing": self.flat.publicId}
        response = self.client.post(reverse("checkout"), data)
        self.assertContains(response, "Pay with Stripe (test)")
        self.assertContains(response, "4242 4242 4242 4242")

    def test_session_cent_amounts_tax(self):
        response, post = self.pay()
        self.assertEqual(response.url, "https://checkout.stripe.com/c/pay/cs_test_1")
        sent = post.call_args.kwargs["data"]
        self.assertEqual(sent["line_items[0][price_data][unit_amount]"], 4900)
        self.assertEqual(sent["line_items[1][price_data][unit_amount]"], 18000)
        self.assertEqual(sent["line_items[2][price_data][unit_amount]"], 2061)
        self.assertEqual(sent["client_reference_id"], self.token)
        self.assertIn("{CHECKOUT_SESSION_ID}", sent["success_url"])
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer sk_test_asdf")
        self.assertFalse(Order.objects.exists())

    def test_paid_return_creates_split(self):
        self.pay()
        response = self.back()
        self.back()
        package, service = Order.objects.order_by("pk")
        self.assertEqual(response.url, reverse("my_orders") + f"#order-{package.pk}")
        self.assertEqual((package.total, service.total), (Decimal("53.41"), Decimal("196.20")))
        self.assertEqual(package.items.get().listing, self.flat)
        self.assertEqual(Order.objects.count(), 2)
        self.assertEqual(self.client.session["cart"], [])

    def test_unpaid_wrong_amount_seller(self):
        self.pay()
        self.back(payment_status="unpaid")
        self.back(amount_total=100)
        self.back(metadata={"seller": "999", "listing": str(self.flat.publicId)})
        self.assertFalse(Order.objects.exists())
        self.assertEqual(len(self.client.session["cart"]), 2)

    def test_timeout_shows_error_missing(self):
        with patch("sellerpanel.stripepay.requests.post", side_effect=requests.Timeout):
            data = {
                "checkoutToken": self.token,
                "listing": self.flat.publicId,
                "pay": "yes",
                **CARD,
            }
            response = self.client.post(reverse("checkout"), data, follow=True)
        self.assertContains(response, "Sorry, we couldn’t reach Stripe just now.")
        self.assertFalse(Order.objects.exists())
        self.assertEqual(len(self.client.session["cart"]), 2)

    @override_settings(STRIPE_SECRET_KEY="sk_live_asdf")
    def test_live_key_demo_payment(self):
        data = {"checkoutToken": self.token, "listing": self.flat.publicId, "pay": "yes", **CARD}
        with patch("sellerpanel.stripepay.requests.post") as post:
            self.client.post(reverse("checkout"), data)
        post.assert_not_called()
        self.assertEqual(Order.objects.count(), 2)

    def test_idempotency_key_reused_until(self):
        first, firstPost = self.pay()
        second, secondPost = self.pay()
        self.assertEqual(first.url, second.url)
        firstKey = firstPost.call_args.kwargs["headers"]["Idempotency-Key"]
        secondKey = secondPost.call_args.kwargs["headers"]["Idempotency-Key"]
        self.assertTrue(firstKey)
        self.assertEqual(firstKey, secondKey)
        self.client.post(reverse("cart_remove", args=[self.photos.pk]))
        self.token = self.client.get(reverse("cart")).context["checkoutToken"]
        _, thirdPost = self.pay()
        self.assertNotEqual(firstKey, thirdPost.call_args.kwargs["headers"]["Idempotency-Key"])
        self.assertFalse(Order.objects.exists())

    def test_malformed_session_response(self):
        data = {"checkoutToken": self.token, "listing": self.flat.publicId, "pay": "yes"}
        for payload in (None, [], "error", {"url": []}, {"url": 123}):
            with self.subTest(payload=payload):
                with patch("sellerpanel.stripepay.requests.post", return_value=stripe_reply(payload)):
                    response = self.client.post(reverse("checkout"), data)
                self.assertRedirects(response, reverse("cart"))
        self.assertFalse(Order.objects.exists())

    def test_return_blocks_non_object(self):
        for metadata in (["seller"], "seller", 123):
            with self.subTest(metadata=metadata):
                self.assertRedirects(self.back(metadata=metadata), reverse("cart"))
        self.assertFalse(Order.objects.exists())

    def test_return_blocks_mismatched_non(self):
        changes = (
            {"id": "cs_test_other"},
            {"mode": "subscription"},
            {"status": "open"},
            {"livemode": True},
            {"currency": "usd"},
            {"client_reference_id": [self.token]},
        )
        for change in changes:
            with self.subTest(change=change):
                self.assertRedirects(self.back(**change), reverse("cart"))
                self.assertFalse(Order.objects.exists())

    def test_inactive_seller_blocks_start(self):
        self.seller.is_active = False
        with patch("sellerpanel.billing.lock_user", return_value=self.seller):
            with patch("sellerpanel.stripepay.requests.post") as post:
                response = self.client.post(
                    reverse("checkout"),
                    {"checkoutToken": self.token, "listing": self.flat.publicId, "pay": "yes"},
                )
        self.assertEqual(response.status_code, 403)
        post.assert_not_called()
        self.assertFalse(Order.objects.exists())


class OrderTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.flat = makeListing(self.seller)
        self.addon = makeProduct("Floor plan", "addon", 40, None)
        self.client.force_login(self.seller)

    def test_counts_filters_spending_exclude(self):
        makeOrder(self.seller, self.addon, "paid")
        makeOrder(self.seller, self.addon, "completed")
        makeOrder(self.seller, self.addon, "refunded")
        response = self.client.get(reverse("my_orders"))
        self.assertEqual(
            response.context["orderCounts"], {"all": 3, "active": 1, "completed": 1, "refunded": 1}
        )
        self.assertEqual(response.context["spent"], Decimal("80.00"))
        active = self.client.get(reverse("my_orders"), {"status": "active"}).context["orders"]
        self.assertEqual([order.status for order in active], ["paid"])

    def test_active_package_status_receipt(self):
        order = makeOrder(self.seller, makeProduct(), listing=self.flat)
        response = self.client.get(reverse("my_orders"))
        self.assertTrue(response.context["orders"][0].items.all()[0].packageStatus.startswith("Active"))
        self.assertEqual(response.context["plans"][0]["order"], order)
        self.assertContains(response, '<h2 id="plans-heading">Active packages</h2>', html=True)
        self.assertContains(response, "Blk 123 Yishun · ends ")
        for check_text_1 in ("$550,000", "<progress", "Rate this package"):
            self.assertNotContains(response, check_text_1)
        self.assertEqual(self.client.get(reverse("receipt", args=[order.pk])).status_code, 200)
        self.client.force_login(makeUser("qwer@test.com", "seller"))
        self.assertEqual(self.client.get(reverse("receipt", args=[order.pk])).status_code, 404)

    def test_service_rated_completion_rating(self):
        item = makeOrder(self.seller, self.addon, "paid").items.get()
        self.client.post(reverse("rate_item", args=[item.pk]), {"rating": "5"})
        self.assertFalse(Review.objects.exists())
        item.order.status = "completed"
        item.order.save()
        self.client.post(reverse("rate_item", args=[item.pk]), {"rating": "5", "comment": "Nice plan"})
        self.client.post(reverse("rate_item", args=[item.pk]), {"rating": "4"})
        review = Review.objects.get()
        self.assertEqual((review.rating, review.comment, review.seller), (4, "", self.seller))

    def test_invalid_rating_get_request(self):
        item = makeOrder(self.seller, self.addon, "completed").items.get()
        self.client.post(reverse("rate_item", args=[item.pk]), {"rating": "9"})
        self.assertFalse(Review.objects.exists())
        self.assertEqual(self.client.get(reverse("rate_item", args=[item.pk])).status_code, 405)

    def test_package_rating_requires_waiting(self):
        order = makeOrder(self.seller, makeProduct(), "completed", listing=self.flat)
        item = order.items.get()
        response = self.client.get(reverse("receipt", args=[order.pk]))
        self.assertEqual((response.context["rateable"], response.context["waiting"]), ([], [item]))
        self.client.post(reverse("rate_item", args=[item.pk]), {"rating": "5"})
        self.assertFalse(Review.objects.exists())
        Order.objects.filter(pk=order.pk).update(createdAt=timezone.now() - timedelta(days=8))
        self.assertEqual(self.client.get(reverse("receipt", args=[order.pk])).context["rateable"], [item])
        self.client.post(reverse("rate_item", args=[item.pk]), {"rating": "5"})
        self.assertEqual(Review.objects.get().orderItem, item)

    def test_receipt_shows_customer_date(self):
        self.seller.first_name, self.seller.last_name = ("Tan", "Mei Ling")
        self.seller.save()
        order = makeOrder(self.seller, self.addon, "paid")
        response = self.client.get(reverse("receipt", args=[order.pk]))
        self.assertContains(response, timezone.localtime(order.createdAt).strftime("%Y"))
        self.assertContains(response, "FlatFair (IS2108 student project, demo only)")
        self.assertContains(response, "Tan Mei Ling<br>asdf@test.com")
        self.assertContains(response, "topic=refund&amp;order={}".format(order.pk))
        order.status = "processing"
        order.save()
        self.assertNotContains(self.client.get(reverse("receipt", args=[order.pk])), "Request a refund")

    def test_unresolved_refund_ticket(self):
        order = makeOrder(self.seller, self.addon, "paid")
        ticket = Ticket.objects.create(
            customer=self.seller,
            email="asdf@test.com",
            topic="refund",
            message="Please",
            order=order,
        )
        response = self.client.get(reverse("receipt", args=[order.pk]))
        self.assertNotContains(response, "Request a refund")
        self.assertContains(response, reverse("my_ticket", args=[ticket.pk]))
        ticket.status = "resolved"
        ticket.save()
        self.assertContains(self.client.get(reverse("receipt", args=[order.pk])), "Request a refund")
        response = self.client.get(reverse("my_orders"))
        row = response.content.decode().split(f'id="order-{order.pk}"', 1)[1].split("</article>", 1)[0]
        self.assertEqual(row.count("<a "), 1)
        self.assertIn("View receipt", row)
        self.assertNotIn("<details", row)


class DowngradeTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.flat = makeListing(self.seller)
        self.client.force_login(self.seller)

    def add(self, product):
        response = self.client.post(
            reverse("cart_add", args=[product.pk]),
            {"listing": str(self.flat.publicId)},
            follow=True,
        )
        return " ".join((str(m) for m in response.context["messages"]))

    def test_lower_package_rejected_higher(self):
        makeOrder(self.seller, makeProduct("Boost 90 days", price=149, days=90), listing=self.flat)
        text = self.add(makeProduct("Boost 30 days"))
        self.assertIn("already has the Boost 90 days running", text)

    def test_active_package_blocks_purchased(self):
        boost = makeProduct("Boost 30 days")
        makeOrder(self.seller, boost, listing=self.flat)
        self.assertIn("is already active on this flat until", self.add(boost))

    def test_higher_permanent_package(self):
        makeOrder(self.seller, makeProduct("Boost 30 days"), listing=self.flat)
        self.assertIn("added to your cart", self.add(makeProduct("Top spot", price=299, days=None)))

    def test_implicit_selected_flat_blocks(self):
        makeOrder(
            self.seller,
            makeProduct("Boost 90 days", price=149, days=90),
            "completed",
            listing=self.flat,
        )
        boost = makeProduct("Boost 30 days")
        response = self.client.post(reverse("cart_add", args=[boost.pk]), follow=True)
        text = " ".join((str(m) for m in response.context["messages"]))
        self.assertIn("already has the Boost 90 days running", text)
        self.assertEqual(self.client.session.get("cart", []), [])
        response = self.client.get(reverse("packages"))
        self.assertEqual(response.context["selectedFlat"], self.flat)
        self.assertEqual(
            response.context["runningNotes"],
            ["Running on Blk 123 Yishun: Boost 90 days, 89 days left"],
        )
        self.assertNotContains(response, "<progress")


class CardTests(TestCase):

    def check(self, **changes):
        data = dict(CARD)
        data.update(changes)
        form = CardForm(data)
        form.is_valid()
        return form

    def test_luhn_allows_valid_test(self):
        self.assertTrue(luhnValid("4242424242424242"))
        self.assertTrue(luhnValid("5555555555554444"))
        self.assertTrue(luhnValid("378282246310005"))
        self.assertFalse(luhnValid("4242424242424241"))

    def test_form_identifies_visa_mastercard(self):
        self.assertEqual(self.check().cleaned_data["brand"], "Visa")
        self.assertEqual(self.check(number="5555 5555 5555 4444").cleaned_data["brand"], "Mastercard")
        self.assertEqual(self.check(number="2223 0031 2200 3222").cleaned_data["brand"], "Mastercard")
        form = self.check(number="3782 822463 10005", cvc="1234")
        self.assertEqual(form.cleaned_data["brand"], "American Express")
        self.assertEqual(form.cleaned_data["last4"], "0005")

    def test_form_blocks_bad_checksum(self):
        self.assertIn("typo", self.check(number="4242 4242 4242 4241").errors["number"][0])
        self.assertIn("We take", self.check(number="6011 1111 1111 1117").errors["number"][0])
        self.assertIn("digits", self.check(number="4242-abcd").errors["number"][0])

    def test_form_blocks_expired_month(self):
        self.assertIn("expired", self.check(expiry="01/20").errors["expiry"][0])
        self.assertIn("01 to 12", self.check(expiry="13/30").errors["expiry"][0])
        self.assertIn("3-digit", self.check(cvc="12").errors["cvc"][0])
        self.assertIn("4-digit", self.check(number="3782 822463 10005", cvc="123").errors["cvc"][0])

    def test_declined_test_produces_form(self):
        form = self.check(number="4000 0000 0000 0002")
        self.assertIn("declined", form.non_field_errors()[0])

    def test_number_cvc_reject_unicode(self):
        self.assertIn("number", self.check(number="4" + "²" * 15).errors)
        self.assertIn("cvc", self.check(cvc="¹²³").errors)
