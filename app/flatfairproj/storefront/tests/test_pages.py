from django.contrib.staticfiles import finders
from .helpers import jpeg
from storefront.templatetags.estate_tags import asking_gap
from storefront.templatetags.estate_tags import evidence_scale
from storefront.templatetags.estate_tags import flat_label
from storefront.templatetags.estate_tags import money_short
from storefront.templatetags.estate_tags import price_gap
from storefront.templatetags.estate_tags import range_money
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.base import Message
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from adminpanel.models import Decision, Ticket, TicketMessage
from .helpers import makeListing, makeOrder, makeProduct, makeStaff, makeUser
from storefront.models import (
    ActivityNotice,
    Collection,
    Offer,
    Review,
    SavedListing,
    Sale,
    ListingPhoto,
    ViewingBooking,
    ViewingSlot,
)


class PageTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller", name="Tan")
        self.buyer = makeUser("qwer@test.com", name="Priya")
        self.admin = makeStaff("admin@test.com")
        self.flat = makeListing(self.seller)
        self.held = makeListing(self.seller, status="hidden", askingPrice=700000)
        offer = Offer.objects.create(
            respondedAt=timezone.now(),
            listing=self.flat,
            buyer=self.buyer,
            amount=520000,
            status="countered",
            counterAmount=540000,
        )
        Offer.objects.create(listing=self.flat, buyer=makeUser("zxcv@test.com"), amount=500000)
        slot = ViewingSlot.objects.create(listing=self.flat, startsAt=timezone.now() + timedelta(days=1))
        ViewingBooking.objects.create(slot=slot, buyer=self.buyer)
        collection = Collection.objects.create(user=self.buyer, name="asdf")
        SavedListing.objects.create(collection=collection, listing=self.flat)
        self.product = makeProduct("Boost 30 days")
        self.addon = makeProduct("Floor plan", "addon", 40, None)
        self.order = makeOrder(self.seller, self.product, listing=self.flat)
        done = makeOrder(self.seller, self.addon, status="completed")
        Review.objects.create(seller=self.seller, orderItem=done.items.get(), rating=4, comment="Good")
        self.ticket = Ticket.objects.create(
            customer=self.seller,
            email="asdf@test.com",
            topic="orders",
            message="asdf",
            order=self.order,
        )
        TicketMessage.objects.create(ticket=self.ticket, author=self.admin, body="qwer", fromStaff=True)
        Decision.objects.create(
            listing=self.held,
            staff=self.admin,
            decision="documents",
            reason="Send papers",
            askingPrice=700000,
        )
        ActivityNotice.objects.create(
            user=self.buyer,
            kind="offer_countered",
            text="New price",
            url="/my-offers#offer-" + str(offer.pk),
        )

    def check(self, names):
        for name in names:
            if isinstance(name, tuple):
                url = reverse(name[0], args=name[1:])
            else:
                url = reverse(name)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)

    @patch("vision.templatetags.vision_tags.weights_ok", return_value=True)
    @patch("storefront.address.nearest_mrt", return_value={"name": "Yishun", "metres": 500})
    def test_flat_glance_cards_keep(self, mrt, weights):
        ListingPhoto.objects.create(listing=self.flat, room="living_room", image="listings/living.jpg")
        Sale.objects.all().delete()
        for band, price in (("07 TO 09", 550000), ("10 TO 12", 600000)):
            for number in range(5):
                Sale.objects.create(
                    month=timezone.localdate().replace(day=1),
                    town=self.flat.town,
                    flatType=self.flat.flatType,
                    block=str(200 + number),
                    street="YISHUN RING RD",
                    storeyRange=band,
                    floorAreaSqm=self.flat.floorAreaSqm,
                    flatModel=self.flat.flatModel,
                    leaseCommenceYear=self.flat.leaseCommenceYear,
                    price=price,
                )
        response = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        content = response.content.decode()
        fold = '<details class="fold glance-fold'
        for name, expected in (
            (
                "sales",
                (
                    "Recent sales nearby",
                    "Median $600k · 5 sales",
                    "glance-sale-chart",
                    "Yishun Ring Rd",
                    "Lease from",
                    "$600,000",
                ),
            ),
            (
                "floors",
                (
                    'id="floor-prices"',
                    "High floors +$50k",
                    "This flat",
                    "$550,000",
                    "$600,000",
                    "5 sold",
                ),
            ),
            (
                "plan",
                (
                    'id="floor-plan"',
                    "4-room · 92 sqm",
                    "glance-plan-thumbnail",
                    "data-lightbox",
                    "typical layout",
                ),
            ),
            (
                "location",
                ("500 m · Yishun", "500 <small>m away", "Singapore map", "Other flats in Yishun"),
            ),
            (
                "shop",
                (
                    'id="shop-the-look"',
                    "Checking photos",
                    "Checking the photos",
                    "Refresh to see items",
                    "vision-content",
                ),
            ),
        ):
            with self.subTest(section=name):
                self.assertContains(response, f"{fold} glance-{name}", count=1)
                start = content.index(f"{fold} glance-{name}")
                end = content.find(fold, start + 1)
                section = content[start : end if end != -1 else len(content)]
                self.assertNotIn(" open", section.split(">", 1)[0])
                self.assertIn('<svg class="ui-icon"', section.split("</summary>", 1)[0])
                for text in expected:
                    self.assertIn(text, section)
        for check_text_1 in (
            'class="listing-gallery"',
            'id="listing-worth"',
            'class="listing-action-area"',
            "$550,000",
        ):
            self.assertContains(response, check_text_1)

    def test_public_render_link_payment(self):
        flat = ("listing_detail", self.flat.publicId)
        self.check(
            [
                "home",
                "listings",
                "valuation",
                "packages",
                "support",
                "signin",
                "register",
                "forgot_password",
                flat,
            ]
        )
        self.assertContains(self.client.get(reverse("home")), reverse("terms"))
        self.assertContains(self.client.get(reverse("terms")), "Fees, payments and refunds")

    def test_buyer_render_saved_offers(self):
        self.client.force_login(self.buyer)
        flat = ("listing_detail", self.flat.publicId)
        self.check(
            [
                "home",
                "listings",
                "saved",
                "my_offers",
                "notifications",
                "support",
                "my_messages",
                "packages",
                flat,
            ]
        )

    def test_buyer_offers_viewings_appear(self):
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("my_offers"))
        self.assertContains(response, 'id="whats-new-heading"')
        content = response.content.decode()
        self.assertLess(content.index('id="your-offers"'), content.index('id="whats-new-heading"'))

    def test_browse_valuation_seller_restore(self):
        response = self.client.get(reverse("listings"))
        self.assertNotContains(response, 'class="page-hero')
        self.assertContains(response, "<h1>Flats for sale</h1>", html=True)
        self.assertEqual(response.context["total"], 1)
        self.assertContains(response, '<p class="result-count">1 flat</p>', html=True)
        response = self.client.get(reverse("valuation"))
        self.assertNotContains(response, 'class="page-hero')
        self.assertContains(response, "<h1>Check the price of any HDB flat</h1>", html=True)
        self.assertContains(response, "Selling? See what yours is worth. Buying? See if a price is fair.")
        self.client.force_login(self.seller)
        response = self.client.get(reverse("edit_listing", args=[self.held.publicId]))
        self.assertContains(response, 'data-tier="task" data-compact')
        self.assertContains(response, 'class="page-hero-image"')
        self.client.force_login(makeUser("new-seller@test.com", "seller"))
        response = self.client.get(reverse("sell"))
        self.assertContains(response, 'data-tier="task" data-compact')
        self.assertContains(response, 'class="page-hero-image"')
        self.assertNotContains(response, 'class="journey hero-steps"')

    def test_staff_overview_opens_plain(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_overview"))
        self.assertContains(response, "Here is what needs action now.")
        self.assertNotContains(response, 'class="page-hero-image"')
        self.assertNotContains(response, "storefront/photos/staff-overview.jpg")

    def test_text_banner_compact_layout(self):
        response = self.client.get(reverse("terms"))
        self.assertContains(response, 'class="page-hero page-hero-text"')
        self.assertNotContains(response, "data-photo-surface")
        self.assertNotContains(response, 'class="page-hero-image"')

    def test_empty_browse_banner_keeps(self):
        response = self.client.get(reverse("listings"), {"q": "no such flat"})
        self.assertContains(response, "<h1>Flats for sale</h1>", html=True)
        self.assertNotContains(response, 'class="result-count"')
        self.assertContains(response, "No flats match these filters.")

    def test_seller_phone_menu_labels(self):
        self.client.force_login(self.seller)
        response = self.client.get(reverse("seller_viewings"))
        phone = response.content.decode().split('class="phone-primary"', 1)[1].split("</nav>", 1)[0]
        self.assertIn(reverse("seller_viewings"), phone)
        self.assertIn("<span>Offers &amp; viewings</span>", phone)
        self.assertNotContains(response, "data-tab-bar")

    def test_seller_render_duplicate_sell(self):
        self.client.force_login(self.seller)
        pages = [
            "dashboard",
            "my_listings",
            "seller_viewings",
            "cart",
            "my_orders",
            "notifications",
            "packages",
        ]
        pages += [
            ("receipt", self.order.pk),
            ("edit_listing", self.held.publicId),
            ("my_ticket", self.ticket.pk),
        ]
        self.check(pages + ["my_messages", ("listing_detail", self.held.publicId)])
        self.assertRedirects(self.client.get(reverse("sell")), reverse("my_listings"))

    def test_staff_detail_views_render(self):
        self.client.force_login(self.admin)
        pages = [
            "admin_overview",
            "admin_queue",
            "admin_listings",
            "admin_orders",
            "admin_customers",
            "admin_catalogue",
        ]
        pages += [
            "admin_tickets",
            "admin_reviews",
            "admin_staff",
            "admin_audit",
            "admin_account",
            "admin_price_model",
        ]
        pages += [
            ("admin_order", self.order.pk),
            ("admin_customer", self.seller.pk),
            ("admin_ticket", self.ticket.pk),
        ]
        pages += [
            ("admin_product_edit", self.product.pk),
            "admin_product_new",
            ("listing_detail", self.held.publicId),
        ]
        self.check(pages)

    def test_staff_sidebar_outer_aside(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_customer", args=[self.seller.pk]))
        self.assertContains(response, '<aside class="staff-menu" aria-label="Admin navigation">')
        self.assertContains(response, '<nav class="side" aria-label="Admin pages">')
        self.assertNotContains(response, '<details class="staff-menu"')
        self.assertContains(response, reverse("admin_listings"))
        self.assertContains(response, reverse("admin_orders"))

    def test_staff_search_help_visible(self):
        self.client.force_login(self.admin)
        for name, helpText in (
            ("orders", "Order number, customer email or name"),
            ("listings", "Block or postal code"),
        ):
            with self.subTest(page=name):
                response = self.client.get(reverse("admin_" + name))
                self.assertContains(response, 'class="staff-page-head"')
                self.assertContains(
                    response, 'class="panel filter-bar staff-filters staff-filters--' + name + '"'
                )
                self.assertContains(response, 'class="mini-field staff-search"')
                self.assertContains(response, 'aria-describedby="' + name + '-search-help"')
                self.assertContains(
                    response,
                    '<small id="' + name + '-search-help">' + helpText + "</small>",
                    html=True,
                )
                self.assertNotContains(response, 'placeholder="' + helpText + '"')

    def test_staff_listing_rows_view(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_listings"))
        self.assertNotContains(response, "More actions")
        self.assertNotContains(response, 'name="action" value="takedown"')
        self.assertContains(response, reverse("admin_listing", args=[self.flat.pk]))
        response = self.client.get(reverse("admin_listing", args=[self.flat.pk]))
        self.assertContains(response, 'name="action" value="takedown"')
        self.assertContains(response, 'name="action" value="review"')
        self.assertContains(response, reverse("admin_listing_edit", args=[self.flat.pk]))
        response = self.client.get(reverse("admin_listing", args=[self.held.pk]))
        self.assertContains(response, 'name="action" value="relist"')

    def test_payment_links_refund_terms(self):
        self.flat.status = "sold"
        self.flat.save()
        self.held.status = "review"
        self.held.save()
        self.client.force_login(self.seller)
        session = self.client.session
        session["cart"] = [self.product.pk, self.addon.pk]
        session.save()
        token = self.client.get(reverse("cart")).context["checkoutToken"]
        response = self.client.post(
            reverse("checkout"), {"checkoutToken": token, "listing": self.held.publicId}
        )
        self.assertTemplateUsed(response, "sellerpanel/payment.html")
        self.assertContains(response, reverse("terms") + "#refunds")


class FlashMessageTests(SimpleTestCase):

    def test_errors_warnings_alert_roles(self):
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        for level, tag in ((messages.ERROR, "error"), (messages.WARNING, "warning")):
            with self.subTest(level=level):
                content = render_to_string(
                    "storefront/base.html",
                    {"messages": [Message(level, "Check your details")]},
                    request=request,
                )
                self.assertIn(f'<div class="page-notice flash {tag}" role="alert">', content)
                self.assertNotIn(f'class="toast flash {tag}"', content)
                toast = content.split(f'class="page-notice flash {tag}"', 1)[1].split("</div>", 1)[0]
                self.assertIn('<svg class="ui-icon"', toast)
                self.assertIn('aria-hidden="true"', toast)
                self.assertNotIn("⚠", toast)
                self.assertInHTML("<span>Check your details</span>", toast)
                self.assertIn(
                    'class="notice-close" data-dismiss-toast aria-label="Dismiss this message"',
                    toast,
                )
                self.assertNotIn(">Dismiss<", toast)

    def test_success_info_status_roles(self):
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        for level, tag in ((messages.SUCCESS, "success"), (messages.INFO, "info")):
            with self.subTest(level=level):
                content = render_to_string(
                    "storefront/base.html", {"messages": [Message(level, "Saved")]}, request=request
                )
                self.assertIn(f'class="toast flash {tag}" role="status"', content)
                self.assertNotIn("page-notice", content)
                self.assertIn(
                    'class="toast-close" data-dismiss-toast aria-label="Dismiss this message"',
                    content,
                )


class ErrorPageTests(TestCase):

    def test_unknown_route_custom_not(self):
        with self.assertLogs("django.request", "WARNING"):
            response = self.client.get("/no-such-page")
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "404.html")

    @override_settings(DEBUG=True)
    def test_debug_not_found_requires(self):
        with self.assertLogs("django.request", "WARNING"):
            self.assertTemplateUsed(self.client.get("/no-such-page"), "404.html")
            self.assertTemplateNotUsed(self.client.get("/no-such-page?debug404=1"), "404.html")

    def test_private_media_path_traversal(self):
        with self.assertLogs("django.request", "WARNING"):
            self.assertEqual(self.client.get("/media/demo/a.jpg").status_code, 404)
            self.assertEqual(self.client.get("/media/../settings.py").status_code, 404)
        self.assertEqual(self.client.get(reverse("favicon")).status_code, 302)


class CssBalanceTests(TestCase):

    def test_site_stylesheet_balanced_braces(self):
        text = (Path(settings.BASE_DIR) / "assets" / "static" / "storefront" / "site.css").read_text()
        self.assertEqual(text.count("{"), text.count("}"))

    def test_home_loads_site_stylesheet(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, "storefront/site.css")


class SettingsTests(TestCase):

    def test_sqlite_connections_allow_twenty(self):
        self.assertEqual(settings.DATABASES["default"]["OPTIONS"]["timeout"], 20)


class LightboxLinkParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.count = 0

    def handle_starttag(self, tag, attrs):
        if tag == "a" and any((name == "data-lightbox" for name, value in attrs)):
            self.count += 1


class HomeTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.flat = makeListing(self.seller, town="BEDOK")
        makeListing(makeUser("review@test.com", "seller"), town="BEDOK", status="review")

    def test_counts_live_flats_towns(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total"], 1)
        self.assertEqual(response.context["liveTowns"], [{"town": "BEDOK", "count": 1}])
        self.assertNotIn("mapDots", response.context)
        self.assertContains(
            response,
            'href="/listings?town=BEDOK" aria-label="Bedok, 1 flat" data-caption=',
            count=1,
        )

    def test_town_filter_redirects_listings(self):
        response = self.client.get(reverse("home"), {"town": "BEDOK"})
        self.assertRedirects(response, reverse("listings") + "?town=BEDOK")

    def test_keyword_search_redirects(self):
        response = self.client.get(reverse("home"), {"q": "760123"}, follow=True)
        self.assertEqual(response.redirect_chain, [(reverse("listings") + "?q=760123", 302)])
        self.assertEqual([flat.pk for flat in response.context["listings"]], [self.flat.pk])
        self.assertContains(self.client.get(reverse("home")), 'name="q" maxlength="60"')

    def test_valuation_query_prefills(self):
        response = self.client.get(
            reverse("valuation"), {"town": "BEDOK", "flatType": "4 ROOM", "storey": "12"}
        )
        form = response.context["form"]
        self.assertFalse(form.is_bound)
        self.assertEqual((form["town"].value(), form["storey"].value()), ("BEDOK", "12"))
        self.assertNotIn("lo", response.context)

    def test_seller_links_sell_form(self):
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get(reverse("home")).context["listUrl"], reverse("sell"))


class FlatPageTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.buyer = makeUser("qwer@test.com")
        self.flat = makeListing(self.seller)
        self.url = reverse("listing_detail", args=[self.flat.publicId])

    def test_public_shows_price_band(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.context["lo"], response.context["est"]), (500000, 550000))
        self.assertTrue(response.context["canTransact"])
        self.assertFalse(response.context["isBuyer"])

    def test_buyer_includes_active_offer(self):
        self.client.force_login(self.buyer)
        offer = Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=500000)
        response = self.client.get(self.url)
        self.assertTrue(response.context["isBuyer"])
        self.assertEqual(response.context["myOffer"], offer)
        self.assertEqual(response.context["offer95"], 522000)

    def test_missing_estimate_no_typical(self):
        self.flat.estimatedPrice = None
        self.flat.save()
        self.assertIsNone(self.client.get(self.url).context["est"])

    def test_inactive_seller_redirects_home(self):
        self.seller.is_active = False
        self.seller.save()
        self.assertRedirects(self.client.get(self.url), reverse("home"))

    def test_room_photo_access_follows(self):
        photo = ListingPhoto(listing=self.flat, room="kitchen")
        photo.image.save("kitchen.jpg", jpeg())
        url = reverse("listing_photo", args=[self.flat.publicId, "kitchen"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.flat.status = "review"
        self.flat.save()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("listing_photo", args=[self.flat.publicId, "view"])).status_code,
            404,
        )

    def test_direct_listing_media_url(self):
        self.assertEqual(
            self.client.get("/media/listings/" + str(self.flat.publicId) + "/kitchen.jpg").status_code,
            404,
        )

    def test_view_count_excludes_repeats(self):
        self.client.get(self.url)
        self.client.get(self.url)
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.viewCount, 1)
        self.client.force_login(self.seller)
        self.client.get(self.url)
        self.client.force_login(makeStaff("admin@test.com"))
        self.client.get(self.url)
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.viewCount, 1)
        self.client.force_login(self.buyer)
        self.client.get(self.url)
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.viewCount, 2)

    def test_seller_declared_intent_details(self):
        self.assertNotContains(self.client.get(self.url), "seller-declared")
        self.flat.intentToSellDate = timezone.localdate() - timedelta(days=20)
        self.flat.save()
        response = self.client.get(self.url)
        self.assertContains(
            response,
            f"Intent to Sell · {self.flat.intentToSellDate.day} {self.flat.intentToSellDate:%b %Y} · seller-declared</span></summary>",
        )
        self.assertContains(
            response,
            f"Registered {self.flat.intentToSellDate.day} {self.flat.intentToSellDate:%b %Y}.",
        )
        self.assertContains(response, "FlatFair hasn’t verified the HDB Intent to Sell registration.")
        for fields in (
            {"intentToSellDate": timezone.localdate() - timedelta(days=400)},
            {"status": "sold"},
        ):
            with self.subTest(fields=fields):
                self.flat.intentToSellDate = timezone.localdate() - timedelta(days=20)
                for field, value in fields.items():
                    setattr(self.flat, field, value)
                self.flat.save()
                if self.flat.status == "sold":
                    self.client.force_login(self.seller)
                response = self.client.get(self.url)
                self.assertNotContains(response, "seller-declared")
                self.assertNotContains(
                    response, "FlatFair hasn’t verified the HDB Intent to Sell registration."
                )

    def test_photos_include_scoped_lightbox(self):
        for room in ["living_room", "kitchen"]:
            photo = ListingPhoto(listing=self.flat, room=room)
            photo.image.save(room + ".jpg", jpeg())
        response = self.client.get(self.url)
        self.assertContains(response, "storefront/lightbox.js", count=1)
        self.assertContains(response, "data-lightbox-group", count=2)
        links = LightboxLinkParser()
        links.feed(response.content.decode())
        self.assertEqual(links.count, 5)
        self.assertContains(response, 'href="#photo-2"', count=2)
        self.assertNotContains(self.client.get(reverse("home")), "lightbox.js")
        self.assertIsNotNone(finders.find("storefront/lightbox.js"))

    def test_shows_nearest_mrt_walk(self):
        response = self.client.get(self.url)
        for check_text_2 in (
            "11 min walk to Yishun MRT",
            'title="North-South Line">NS13</abbr>',
            "About 860 m on foot to Exit A",
        ):
            self.assertContains(response, check_text_2)


class TagTests(TestCase):

    def test_range_money_rounds_nearest(self):
        for value, expected in (
            (612400, "$612,000"),
            (612500, "$613,000"),
            (1234500, "$1,235,000"),
            ("asdf", ""),
        ):
            with self.subTest(value=value):
                self.assertEqual(range_money(value), expected)

    def test_flat_labels_compact_money(self):
        self.assertEqual(flat_label("4 ROOM"), "4-room")
        self.assertEqual(flat_label("EXECUTIVE"), "Executive")
        self.assertEqual(money_short(950), "$950")
        self.assertEqual(money_short(612400), "$612k")
        self.assertEqual(money_short(1250000), "$1.25m")
        self.assertEqual(money_short("asdf"), "")

    def test_price_gap_describes_band(self):
        self.assertEqual(
            price_gap(650000, 500000, 550000, 600000),
            "The asking price is $100,000 above our estimate and outside the fair range.",
        )
        self.assertEqual(
            price_gap(530000, 500000, 550000, 600000, "Your offer"),
            "Your offer is $20,000 below our estimate and within the fair range.",
        )
        self.assertEqual(price_gap(550000, 500000, 550000, 600000), "The asking price matches our estimate.")
        self.assertEqual(asking_gap(540000, 550000), "Your offer is $10,000 below the asking price.")
        self.assertEqual(
            price_gap(563000, 500000, 550000, 600000, short=True),
            "$13,000 above our estimate, within the fair range",
        )
        self.assertEqual(
            price_gap(550000, 500000, 550000, 600000, short=True),
            "matches our estimate, within the fair range",
        )

    def test_evidence_scale_orders_band(self):
        scale = evidence_scale(500000, 550000, 600000)
        self.assertLess(scale["lo"], scale["est"])
        self.assertLess(scale["est"], scale["hi"])
        self.assertIsNone(evidence_scale(600000, 550000, 500000))
        self.assertIn("asking", evidence_scale(500000, 550000, 600000, 700000))
