from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .helpers import makeListing, makeOrder, makeProduct, makeUser
from storefront.models import Order, Review


class PackageLayoutTests(TestCase):

    def setUp(self):
        self.seller = makeUser("packages@test.com", "seller")
        self.flat = makeListing(self.seller, block="326A", town="SENGKANG")
        self.boost = makeProduct("Boost 30 days", price=49, days=30)
        self.longBoost = makeProduct("Boost 90 days", price=149, days=90)
        self.top = makeProduct("Top spot", price=299, days=None)
        self.addon = makeProduct("Pro photography", "addon", 180, None)
        self.addon.description = "A photographer takes up to 20 edited photos of your flat. Download them from your orders within 3 working days."
        self.addon.save()

    def card(self, response, product):
        return response.content.decode().split(f'id="package-{product.pk}"', 1)[1].split("</article>", 1)[0]

    def test_four_plans_share_one(self):
        response = self.client.get(reverse("packages"))
        html = response.content.decode()
        comparison = html.split('<div class="packages-comparison">', 1)[1].split("</section>", 1)[0]
        self.assertEqual(comparison.count("<article"), 4)
        self.assertEqual(comparison.count('class="benefit-check"'), 12)
        self.assertIn('id="package-free"', comparison)
        self.assertContains(response, "Prices shown are before GST.")
        details = html.split('<details class="fold packages-details">', 1)[1].split("</details>", 1)[0]
        for product, gst in (
            (self.boost, "53.41"),
            (self.longBoost, "162.41"),
            (self.top, "325.91"),
        ):
            card = self.card(response, product)
            self.assertIn(f"${product.price}", card)
            self.assertNotIn("with GST", card)
            self.assertIn("Create seller account", card)
            row = details.split(f'data-product="{product.pk}"', 1)[1].split("</tr>", 1)[0]
            self.assertIn(f"${gst} with GST", row)
        self.assertIn("Shown first on the home page", self.card(response, self.top))
        self.assertIn("Until sold", self.card(response, self.top))
        self.assertNotContains(response, 'class="packages-status"')

    def test_seller_status_combines_flat(self):
        now = timezone.now().replace(hour=20, minute=0, second=0, microsecond=0)
        with patch("django.utils.timezone.now", return_value=now):
            order = makeOrder(self.seller, self.longBoost, listing=self.flat)
            Order.objects.filter(pk=order.pk).update(createdAt=now - timedelta(days=12))
            makeOrder(self.seller, self.top, listing=self.flat)
            self.client.force_login(self.seller)
            session = self.client.session
            session["cart"] = [self.addon.pk]
            session.save()
            response = self.client.get(reverse("packages"))
        self.assertContains(response, 'class="packages-status"', count=1)
        for check_text_1 in ("Your flat: Blk 326A Sengkang", "Change flat", "View cart (1)"):
            self.assertContains(response, check_text_1)
        self.assertNotContains(response, "Running on")
        self.assertIn("Included in Top spot", self.card(response, self.boost))
        for product in (self.longBoost, self.top):
            self.assertNotIn("Add to cart", self.card(response, product))
        self.assertIn('class="pkg-status is-active">Active until sold</p>', self.card(response, self.top))
        self.assertIn(
            'class="pkg-status is-active">Active, 78 days left</p>',
            self.card(response, self.longBoost),
        )
        details = (
            response.content.decode()
            .split('<details class="fold packages-details">', 1)[1]
            .split("</details>", 1)[0]
        )
        row = details.split(f'data-product="{self.longBoost.pk}"', 1)[1].split("</tr>", 1)[0]
        self.assertIn("active until " + timezone.localtime(now + timedelta(days=78)).strftime("%-d %b"), row)
        self.assertNotContains(response, "Active package details")

    def test_service_description_rating(self):
        order = makeOrder(self.seller, self.addon, listing=self.flat)
        Review.objects.create(
            seller=self.seller, orderItem=order.items.get(), rating=4, comment="Helpful photos."
        )
        response = self.client.get(reverse("packages"))
        card = self.card(response, self.addon)
        self.assertNotIn("3 working days", card)
        self.assertNotIn("edited photos", card)
        self.assertIn("4.0 (1)", card)
        self.assertIn("Create seller account", card)
        details = (
            response.content.decode()
            .split('<details class="fold packages-details">', 1)[1]
            .split("</details>", 1)[0]
        )
        self.assertIn("Details and GST prices", details.split("</summary>", 1)[0])
        row = details.split(f'data-product="{self.addon.pk}"', 1)[1].split("</tr>", 1)[0]
        self.assertIn(self.addon.description, row)
        self.assertIn("$196.20 with GST", row)

    def test_reviews_questions_closed_rows(self):
        for number in range(6):
            order = makeOrder(self.seller, self.boost, listing=self.flat)
            Review.objects.create(
                seller=self.seller,
                orderItem=order.items.get(),
                rating=4,
                comment=f"Seller review {number}.",
            )
        response = self.client.get(reverse("packages"))
        html = response.content.decode()
        reviews = html.split('<details class="fold reviews-section">', 1)[1].split("</details>", 1)[0]
        self.assertIn("What sellers say", reviews.split("</summary>", 1)[0])
        self.assertIn("6 reviews", reviews.split("</summary>", 1)[0])
        self.assertIn('id="reviews"', reviews)
        self.assertEqual(reviews.count("<blockquote>"), 6)
        self.assertContains(response, "4.0 (6)")
        for number in range(6):
            self.assertContains(response, f"Seller review {number}.", count=1)
        page = html.split('<div class="packages-page">', 1)[1].split("</section>\n  </div>", 1)[0]
        self.assertNotRegex(page, "<details\\b[^>]*\\bopen\\b")
        faq = page.split('<details class="fold service-faq">', 1)[1]
        self.assertIn("Common questions", faq.split("</summary>", 1)[0])
        self.assertEqual(faq.count("<dt>"), 6)
        self.assertNotIn("<details", faq)
        self.assertIn("Free listings get offers, viewings and checks too.", faq)
        self.assertIn("no money is taken", faq)

    def test_buyer_start_selling_missing(self):
        buyer = makeUser("packagebuyer@test.com", "buyer")
        self.client.force_login(buyer)
        response = self.client.get(reverse("packages"))
        self.assertContains(response, "Start selling")
        self.assertContains(response, reverse("start_selling"))
        self.assertNotContains(response, 'class="packages-status"')
        self.assertNotContains(response, "Add to cart")
        self.assertNotContains(response, 'action="' + reverse("cart_add", args=[self.boost.pk]) + '"')

    def test_seller_purchase_cart_state(self):
        self.client.force_login(self.seller)
        response = self.client.get(reverse("packages"))
        card = self.card(response, self.boost)
        self.assertIn(reverse("cart_add", args=[self.boost.pk]), card)
        self.assertIn(f'name="listing" value="{self.flat.publicId}"', card)
        self.assertIn('name="next" value="cart"', card)
        self.assertEqual(card.count("Add to cart"), 1)
        added = self.client.post(
            reverse("cart_add", args=[self.addon.pk]),
            {"listing": self.flat.publicId, "next": "cart"},
        )
        self.assertRedirects(added, reverse("cart"))
        session = self.client.session
        session["cart"] = [self.boost.pk]
        session.save()
        response = self.client.get(reverse("packages"))
        card = self.card(response, self.boost)
        self.assertIn("In your cart", card)
        self.assertNotIn("Add to cart", card)

    def test_buyer_who_enabled_selling(self):
        buyer = makeUser("sellingbuyer@test.com", "buyer")
        buyer.profile.canSell = True
        buyer.profile.save(update_fields=["canSell"])
        flat = makeListing(buyer)
        self.client.force_login(buyer)
        response = self.client.get(reverse("packages"))
        self.assertTrue(response.context["isSeller"])
        self.assertEqual(response.context["selectedFlat"], flat)
        self.assertContains(response, 'class="packages-status"')
        card = self.card(response, self.boost)
        self.assertIn(f'name="listing" value="{flat.publicId}"', card)
        self.assertIn("Add to cart", card)
        self.assertNotContains(response, "Packages are for sellers.")


class EmptyPackagesTests(TestCase):

    def test_extra_services_heading_hides(self):
        makeProduct("Boost 30 days", price=49, days=30)
        self.assertNotContains(self.client.get(reverse("packages")), "Extra services")
        makeProduct("Floor plan", "addon", 40, None)
        self.assertContains(self.client.get(reverse("packages")), "Extra services")


class PackageTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.flat = makeListing(self.seller)
        self.boost = makeProduct("Boost 30 days", price=49, days=30)
        self.top = makeProduct("Top spot", price=299, days=None)
        makeProduct("Floor plan", "addon", 40, None)

    def test_show_gst_prices_positions(self):
        response = self.client.get(reverse("packages"))
        packages = list(response.context["packages"])
        self.assertEqual([p.name for p in packages], ["Boost 30 days", "Top spot"])
        self.assertEqual(str(packages[0].includingGst), "53.41")
        self.assertEqual(packages[1].place, "First")
        self.assertEqual(len(response.context["addons"]), 1)
        self.assertFalse(response.context["isSeller"])

    def test_show_selected_flats_running(self):
        makeOrder(self.seller, self.top, listing=self.flat)
        self.client.force_login(self.seller)
        response = self.client.get(reverse("packages"), {"listing": self.flat.publicId})
        packages = {p.name: p for p in response.context["packages"]}
        self.assertEqual(response.context["selectedFlat"], self.flat)
        self.assertEqual(packages["Top spot"].runningPlan["name"], "Top spot")
        self.assertEqual(packages["Boost 30 days"].coveredBy, "Top spot")

    def test_ignore_other_sellers_invalid(self):
        other = makeListing(makeUser("qwer@test.com", "seller"))
        self.client.force_login(self.seller)
        response = self.client.get(reverse("packages"), {"listing": other.publicId})
        self.assertIsNone(response.context["selectedFlat"])
        response = self.client.get(reverse("packages"), {"listing": "asdf"})
        self.assertIsNone(response.context["selectedFlat"])

    def test_call_action_depends_on(self):
        self.client.force_login(self.seller)
        response = self.client.get(reverse("packages"))
        self.assertTrue(response.context["hasFlat"])
        self.assertContains(response, "View my listing", count=1)
        self.assertNotContains(response, "List my flat free")
        self.flat.status = "sold"
        self.flat.save()
        response = self.client.get(reverse("packages"))
        self.assertFalse(response.context["hasFlat"])
        self.assertContains(response, "List my flat free", count=1)
        self.assertContains(response, 'class="btn btn-sell btn-block"', count=1)
