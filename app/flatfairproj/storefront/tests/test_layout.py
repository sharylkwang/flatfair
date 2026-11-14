from datetime import timedelta
from html.parser import HTMLParser
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from adminpanel.access import menu
from adminpanel.models import Ticket
from .helpers import (
    makeListing,
    makeOrder,
    makeProduct,
    makeStaff,
    makeUser,
    CLEAN,
    FAIR,
    FLAT,
    ROOMS,
    jpeg,
)
from sellerpanel.tests.helpers import CompletedPhotoQueueMixin
from storefront.models import (
    Collection,
    Listing,
    ListingPhoto,
    Offer,
    SavedListing,
    ViewingBooking,
    ViewingSlot,
    ActivityNotice,
    hasRole,
    Profile,
)
from storefront.presentation import readyPics
from storefront.templatetags.shop_tags import saved_count


class CalmMarkup(HTMLParser):

    def __init__(self, html):
        super().__init__()
        self.tasks = 0
        self.images = []
        self.details = []
        self.inputs = []
        self.forms = 0
        self.nestedForms = False
        self.ids = []
        self.saveTargets = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.append(attrs["id"])
        if tag == "input" and attrs.get("name") == "next" and ("#save-" in attrs.get("value", "")):
            self.saveTargets.append(attrs["value"])
        if attrs.get("data-ui") == "task-card":
            self.tasks += 1
        if tag == "img":
            self.images.append(attrs)
        if tag == "details":
            self.details.append(attrs)
        if tag == "input":
            self.inputs.append(attrs)
        if tag == "form":
            self.nestedForms = self.nestedForms or self.forms > 0
            self.forms += 1

    def handle_endtag(self, tag):
        if tag == "form":
            self.forms -= 1


class CalmUiTests(TestCase):

    def setUp(self):
        self.buyer = makeUser("buyer-calm@test.com")
        self.seller = makeUser("seller-calm@test.com", "seller")
        self.admin = makeStaff("admin-calm@test.com")
        self.otherAdmin = makeStaff("admin2-calm@test.com", "Admin")
        self.flat = makeListing(self.seller)
        self.waiting = makeListing(makeUser("waiting-calm@test.com", "seller"), block="124", status="review")
        for flat in (self.flat, self.waiting):
            ListingPhoto.objects.create(listing=flat, room="living_room", image="listings/calm-living.jpg")
        collection = Collection.objects.create(user=self.buyer, name="Shortlist")
        SavedListing.objects.create(collection=collection, listing=self.flat)
        Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=500000)
        slot = ViewingSlot.objects.create(listing=self.flat, startsAt=timezone.now() + timedelta(days=1))
        ViewingBooking.objects.create(slot=slot, buyer=self.buyer)
        self.order = makeOrder(
            self.seller, makeProduct("Calm floor plan", "addon", 40, None), listing=self.flat
        )
        Ticket.objects.create(
            customer=self.buyer, email=self.buyer.email, message="Please help me", topic="other"
        )
        recommendation = patch("sellerpanel.views.showTip", return_value=None)
        recommendation.start()
        self.addCleanup(recommendation.stop)

    def test_buyer_home_save_menus(self):
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("home"))
        self.assertEqual(response.context["buyerSaved"][0].pk, self.flat.pk)
        self.assertIn(self.flat.pk, [flat.pk for flat in response.context["buyerSuggestions"]])
        markup = CalmMarkup(response.content.decode())
        self.assertEqual(len(markup.ids), len(set(markup.ids)))
        anchors = {urlsplit(target).fragment for target in markup.saveTargets}
        self.assertEqual(len(anchors), 3)
        for anchor in anchors:
            self.assertEqual(markup.ids.count(anchor), 1)

    def test_buyer_home_saved_flats(self):
        Offer.objects.filter(listing=self.flat, buyer=self.buyer).update(
            status="accepted", respondedAt=timezone.now()
        )
        makeOrder(self.seller, makeProduct(), listing=self.flat)
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("home"))
        flat = response.context["buyerSaved"][0]
        self.assertEqual(flat.acceptedCount, 1)
        self.assertEqual(flat.adTag, "Sponsored")
        self.assertTrue(flat.fairPrice)
        self.assertContains(response, "Under offer")

    def test_role_landing_at_most(self):
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("home"))
        tasks = response.content.decode().count('<article class="buyer-task">')
        self.assertGreater(tasks, 0)
        self.assertLessEqual(tasks, 3)
        self.assertEqual(
            response.content.decode()
            .split('class="buyer-tasks"', 1)[1]
            .split("</section>", 1)[0]
            .count("<a "),
            tasks,
        )
        self.assertTemplateNotUsed(response, "storefront/ui/summary_strip.html")
        for user, name in (
            (self.seller, "dashboard"),
            (self.admin, "admin_overview"),
            (self.otherAdmin, "admin_overview"),
        ):
            with self.subTest(role=user.email):
                self.client.force_login(user)
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                markup = CalmMarkup(response.content.decode())
                if name == "admin_overview":
                    self.assertContains(response, 'class="staff-row todo-line todo-line--')
                    self.assertTemplateNotUsed(response, "storefront/ui/task_card.html")
                    self.assertTemplateNotUsed(response, "storefront/ui/summary_strip.html")
                else:
                    self.assertGreater(markup.tasks, 0)
                    self.assertLessEqual(markup.tasks, 3)
                    self.assertTemplateUsed(response, "storefront/ui/task_card.html")
                    self.assertTemplateUsed(response, "storefront/ui/summary_strip.html")
                self.assertFalse(markup.nestedForms)
                self.assertTrue(all(("alt" in img for img in markup.images)))
                if user != self.buyer:
                    more = [
                        item for item in markup.details if item.get("id") in ("seller-more", "staff-more")
                    ]
                    self.assertEqual(len(more), 1)
                    self.assertNotIn("open", more[0])
                    self.assertContains(response, "<summary>More numbers</summary>", html=True)

    def test_flat_lists_share_photo(self):
        for name in ("saved", "my_offers"):
            with self.subTest(page=name):
                self.client.force_login(self.buyer)
                with CaptureQueriesContext(connection) as queries:
                    response = self.client.get(reverse(name))
                self.assertContains(response, 'class="buyer-flat')
                self.assertContains(response, "/photos/living_room")
                markup = CalmMarkup(response.content.decode())
                self.assertFalse(markup.nestedForms)
                self.assertFalse(
                    [item for item in markup.details if "calm-row-details" in item.get("class", "")]
                )
                photoQueries = [
                    query["sql"]
                    for query in queries
                    if 'FROM "storefront_listingphoto"' in query["sql"]
                    and 'WHERE "storefront_listingphoto"."listing_id" IN' in query["sql"]
                ]
                self.assertLessEqual(len(photoQueries), 1)
        for user, name in ((self.admin, "admin_queue"), (self.admin, "admin_listings")):
            with self.subTest(page=name):
                self.client.force_login(user)
                with CaptureQueriesContext(connection) as queries:
                    response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateNotUsed(response, "storefront/ui/photo_row.html")
                self.assertTemplateUsed(response, "storefront/ui/thumbnail.html")
                self.assertContains(response, "/photos/living_room")
                markup = CalmMarkup(response.content.decode())
                self.assertFalse(markup.nestedForms)
                self.assertFalse(
                    [item for item in markup.details if "calm-row-details" in item.get("class", "")]
                )
                photoQueries = [
                    query["sql"]
                    for query in queries
                    if 'FROM "storefront_listingphoto"' in query["sql"]
                    and 'WHERE "storefront_listingphoto"."listing_id" IN' in query["sql"]
                ]
                self.assertLessEqual(len(photoQueries), 1)

    def test_seller_lists_one_row(self):
        self.client.force_login(self.seller)
        for name in ("my_listings", "seller_viewings", "my_orders"):
            with self.subTest(page=name):
                with CaptureQueriesContext(connection) as queries:
                    response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "sellerpanel/status_badge.html")
                self.assertTemplateNotUsed(response, "storefront/ui/photo_row.html")
                markup = CalmMarkup(response.content.decode())
                self.assertFalse(markup.nestedForms)
                self.assertTrue(all(("alt" in img for img in markup.images)))
                self.assertNotContains(response, "<summary>Details")
                photoQueries = [
                    query["sql"]
                    for query in queries
                    if 'FROM "storefront_listingphoto"' in query["sql"]
                    and 'WHERE "storefront_listingphoto"."listing_id" IN' in query["sql"]
                ]
                self.assertLessEqual(len(photoQueries), 1)
                if name != "my_orders":
                    self.assertContains(response, "/photos/living_room")

    def test_queue_selection_remains(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_queue"))
        markup = CalmMarkup(response.content.decode())
        selected = [item for item in markup.inputs if item.get("name") == "selected"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["form"], "bulk-approve")
        self.assertEqual(selected[0]["value"], str(self.waiting.pk))
        self.assertContains(response, f"Select listing {self.waiting.pk} for approval")
        for field in (f"revision-{self.waiting.pk}", f"status-{self.waiting.pk}"):
            self.assertTrue(
                any(
                    (
                        item.get("name") == field and item.get("form") == "bulk-approve"
                        for item in markup.inputs
                    )
                )
            )

    def test_empty_lists_shared_illustration(self):
        Offer.objects.all().delete()
        ViewingBooking.objects.all().delete()
        SavedListing.objects.all().delete()
        self.client.force_login(self.buyer)
        for name, heading, style in (
            ("saved", "This list is empty", "btn btn-brand"),
            ("my_offers", "No offers or viewings yet", "btn btn-brand btn-sm"),
        ):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertContains(response, '<span class="empty-icon" aria-hidden="true">', count=1)
                self.assertContains(response, f"<h3>{heading}</h3>", html=True)
                self.assertContains(
                    response,
                    f'<a class="{style}" href="' + reverse("listings") + '">Browse flats</a>',
                    count=1,
                    html=True,
                )

    def test_photo_preparation_one_query(self):
        first = Listing.objects.get(pk=self.flat.pk)
        duplicate = Listing.objects.get(pk=self.flat.pk)
        second = Listing.objects.get(pk=self.waiting.pk)
        with self.assertNumQueries(1):
            readyPics([first, duplicate, second, None])
        with self.assertNumQueries(0):
            for flat in (first, duplicate, second):
                self.assertIsNotNone(flat.coverPhoto())
            readyPics([first, duplicate, second])


class PhoneParser(HTMLParser):

    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.primary = []
        self.more = []
        self.footer = []
        self.target = None
        self.moreAttrs = None
        self.toggle = None
        self.switch = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        self.stack.append((tag, classes))
        if "phone-more" in classes:
            self.moreAttrs = attrs
        if "phone-menu-toggle" in classes:
            self.toggle = attrs
        if "phone-mode-switch" in classes:
            self.switch = True
        if tag == "a":
            self.target = {"href": attrs["href"], "label": "", "current": attrs.get("aria-current")}
            for name, group in (
                ("phone-primary", self.primary),
                ("phone-more", self.more),
                ("phone-footer", self.footer),
            ):
                if any((name in classes for _, classes in self.stack)):
                    group.append(self.target)
        if tag in ("input", "meta", "link", "img", "br"):
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag == "a":
            self.target = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.target and (not any(("unread-badge" in classes for _, classes in self.stack))):
            self.target["label"] += data.strip()


class MobileChromeTests(TestCase):

    def checkPhone(self, page, expected):
        response = self.client.get(reverse(page))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">',
            html=True,
        )
        self.assertNotContains(response, 'class="tab-bar"')
        self.assertNotContains(response, "data-tab-bar")
        phone = PhoneParser(response.content.decode())
        self.assertEqual([row["label"] for row in phone.primary], [label for label, route in expected])
        self.assertEqual(
            [row["href"] for row in phone.primary], [reverse(route) for label, route in expected]
        )
        self.assertEqual(phone.toggle["aria-controls"], "phone-sheet")
        self.assertEqual(phone.toggle["aria-expanded"], "false")
        self.assertContains(response, '<details class="phone-menu" data-phone-menu>')
        self.assertEqual(
            [row["label"] for row in phone.footer],
            ["FlatFair", "Support", "Terms and privacy", "Community", "Photo credits"],
        )
        self.assertContains(response, 'class="wrap footer-desktop"')
        if phone.moreAttrs is not None:
            self.assertNotIn("open", phone.moreAttrs)
            routes = [row["href"] for row in phone.primary + phone.more]
            self.assertEqual(len(routes), len(set(routes)))
        return (response, phone)

    def test_guest_menu_compact_footer(self):
        response, phone = self.checkPhone(
            "home",
            (
                ("Browse flats", "listings"),
                ("Price check", "valuation"),
                ("Photo search", "lens"),
                ("Community", "community"),
            ),
        )
        self.assertIsNone(phone.moreAttrs)
        self.assertFalse(phone.switch)
        sheet = response.content.decode().split('id="phone-sheet"', 1)[1].split("</details>", 1)[0]
        self.assertEqual(sheet.count("btn btn-brand"), 1)
        self.assertIn("List your flat", sheet)
        self.assertIn(reverse("signin"), sheet)
        self.assertNotIn(">Register<", sheet)

    def test_buyer_primary_more_unread(self):
        buyer = makeUser("mobile-buyer@test.com")
        ActivityNotice.objects.create(
            user=buyer, kind="offer_reply", text="Seller replied", url=reverse("my_offers")
        )
        self.client.force_login(buyer)
        response, phone = self.checkPhone(
            "saved",
            (
                ("Browse flats", "listings"),
                ("Saved", "saved"),
                ("Offers & viewings", "my_offers"),
                ("Price check", "valuation"),
                ("Notifications", "notifications"),
            ),
        )
        self.assertEqual([row["label"] for row in phone.primary if row["current"] == "page"], ["Saved"])
        self.assertEqual(
            [row["label"] for row in phone.more],
            ["Photo search", "Community", "My messages", "Support", "Account", "Start selling"],
        )
        self.assertFalse(phone.switch)
        self.assertContains(
            response,
            '<span class="unread-badge">1<span class="visually-hidden"> unread</span></span>',
            html=True,
        )
        self.assertContains(response, 'class="phone-menu-row" type="submit"')

    def test_seller_primary_more_mode(self):
        self.client.force_login(makeUser("mobile-seller@test.com", "seller"))
        response, phone = self.checkPhone(
            "dashboard",
            (
                ("Dashboard", "dashboard"),
                ("My listings", "my_listings"),
                ("Offers & viewings", "seller_viewings"),
                ("Orders", "my_orders"),
                ("Packages", "packages"),
            ),
        )
        self.assertTrue(phone.switch)
        self.assertIsNotNone(phone.moreAttrs)
        self.assertEqual([row["label"] for row in phone.primary if row["current"] == "page"], ["Dashboard"])
        self.assertIn("Account", [row["label"] for row in phone.more])

    def test_dual_role_buyer_menu(self):
        seller = makeUser("mobile-both@test.com", "seller")
        self.client.force_login(seller)
        self.client.post(reverse("switch_mode"), {"mode": "buyer"})
        response, phone = self.checkPhone(
            "home",
            (
                ("Browse flats", "listings"),
                ("Saved", "saved"),
                ("Offers & viewings", "my_offers"),
                ("Price check", "valuation"),
                ("Notifications", "notifications"),
            ),
        )
        self.assertTrue(phone.switch)
        self.assertNotIn(reverse("start_selling"), [row["href"] for row in phone.more])

    def test_admin_phone_menu_groups(self):
        staff = makeStaff("mobile-admin@test.com")
        self.client.force_login(staff)
        expected = [(item["label"], item["page"]) for item in menu(staff)[:4]]
        response, phone = self.checkPhone("admin_overview", expected)
        self.assertEqual(
            [row["href"] for row in phone.more],
            [reverse(item["page"]) for item in menu(staff)[4:]]
            + [reverse("home"), reverse("notifications"), reverse("admin_account")],
        )
        self.assertFalse(phone.switch)
        self.assertNotContains(response, "phone-staff-switch")
        sheet = response.content.decode().split('id="phone-sheet"', 1)[1]
        for label in ("To do", "Customers", "Catalogue &amp; settings"):
            self.assertIn(f"<h2>{label}</h2>", sheet)

    def test_admin_phone_menu_also(self):
        staff = makeStaff("mobile-browsing-admin@test.com")
        self.client.force_login(staff)
        expected = [(item["label"], item["page"]) for item in menu(staff)[:4]]
        response, phone = self.checkPhone("home", expected)
        pages = {row["href"] for row in phone.primary + phone.more}
        self.assertTrue(all((reverse(item["page"]) in pages for item in menu(staff))))
        self.assertNotContains(response, "phone-staff-switch")

    def test_not_found_no_bottom(self):
        response = self.client.get("/missing-mobile-page/")
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "data-tab-bar", status_code=404)
        self.assertContains(response, 'class="wrap phone-footer"', status_code=404)


class StoreHeaderTests(TestCase):

    def setUp(self):
        self.seller = makeUser("header-seller@test.com", "seller")
        self.buyer = makeUser("header-buyer@test.com")
        self.first = makeListing(self.seller)
        self.second = makeListing(makeUser("header-second@test.com", "seller"))
        hidden = makeListing(makeUser("header-hidden@test.com", "seller"), status="hidden")
        near = Collection.objects.create(user=self.buyer, name="Near MRT")
        big = Collection.objects.create(user=self.buyer, name="Big flats")
        for collection, flat in (
            (near, self.first),
            (big, self.first),
            (big, self.second),
            (big, hidden),
        ):
            SavedListing.objects.create(collection=collection, listing=flat)

    def test_saved_count_counts_public(self):
        self.assertEqual(saved_count(self.buyer), 2)
        self.assertEqual(saved_count(self.seller), 0)
        self.assertEqual(saved_count(self.client.get(reverse("home")).wsgi_request.user), 0)

    def test_buyer_search_saved_badge(self):
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("home"))
        header = response.content.decode().split('<header class="site-header', 1)[1].split("</header>", 1)[0]
        self.assertIn(
            f'<form class="header-search" method="get" action="{reverse('listings')}" role="search">',
            header,
        )
        self.assertIn('<input type="search" name="q" maxlength="60"', header)
        self.assertIn(f'data-search-suggestions="{reverse('search_suggestions')}"', header)
        self.assertIn('placeholder="Town, street or postal code"', header)
        self.assertIn('<button type="submit" aria-label="Search flats">', header)
        self.assertIn(
            f'<a class="header-saved" href="{reverse('saved')}" aria-label="Saved flats, 2">',
            header,
        )
        self.assertIn('<span aria-hidden="true">2</span>', header)
        for kind, label in (
            ("2%20ROOM", "2-room"),
            ("4%20ROOM", "4-room"),
            ("EXECUTIVE", "Executive"),
        ):
            self.assertIn(f'href="{reverse('listings')}?flatType={kind}"', header)
            self.assertIn(f'<span class="menu-row-label">{label}</span>', header)
        for route in (
            "listings",
            "valuation",
            "lens",
            "community",
            "my_offers",
            "saved",
            "notifications",
        ):
            self.assertIn(f'href="{reverse(route)}"', header)

    def test_guest_heart_goes_sign(self):
        header = (
            self.client.get(reverse("home"))
            .content.decode()
            .split('<header class="site-header', 1)[1]
            .split("</header>", 1)[0]
        )
        self.assertIn(f'<a class="header-saved" href="{reverse('signin')}?next={reverse('saved')}"', header)
        self.assertNotIn(
            '<span aria-hidden="true">',
            header.split('class="header-saved"', 1)[1].split("</a>", 1)[0],
        )
        self.assertIn(f'href="{reverse('packages')}"', header)
        self.assertContains(self.client.get(reverse("home")), '<html lang="en" class="shop-page home-calm">')
        for user in (self.seller, makeStaff("header-admin@test.com")):
            self.client.force_login(user)
            response = self.client.get(reverse("home"))
            self.assertNotContains(response, "header-search")
            self.assertNotContains(response, "header-saved")
            self.assertContains(response, '<html lang="en" class="work-header home-calm">')

    def test_browse_marks_flat_type(self):
        response = self.client.get(reverse("listings"), {"flatType": "4 ROOM"})
        header = response.content.decode().split('<header class="site-header', 1)[1].split("</header>", 1)[0]
        self.assertIn(
            f'<a class="nav-type" href="{reverse('listings')}?flatType=4%20ROOM" aria-current="true">',
            header,
        )
        self.assertEqual(
            header.split('aria-label="Primary navigation"', 1)[1].split("</nav>", 1)[0].count("aria-current"),
            1,
        )
        self.assertIn(
            f'href="{reverse('listings')}?flatType=4%20ROOM" aria-current="true">4-room</a>',
            response.content.decode().split('class="phone-types"', 1)[1],
        )
        response = self.client.get(reverse("listings"), {"q": "yishun"})
        search = response.content.decode().split('<form class="header-search"', 1)[1].split("</form>", 1)[0]
        self.assertIn('value="Yishun"', search)


class ModeTests(CompletedPhotoQueueMixin, TestCase):

    def setUp(self):
        self.buyer = makeUser("buyer@test.com")
        self.seller = makeUser("seller@test.com", "seller")
        self.flat = makeListing(self.seller)

    def enableBoth(self, user):
        user.profile.canBuy = True
        user.profile.canSell = True
        user.profile.save(update_fields=["canBuy", "canSell"])
        self.client.force_login(user)

    def act(self, kind, identity, **data):
        return self.client.post(reverse("activity_action"), {"action": kind, "id": identity, **data})

    def test_signup_role_enables_buying(self):
        self.assertEqual((self.buyer.profile.canBuy, self.buyer.profile.canSell), (True, False))
        self.assertEqual((self.seller.profile.canBuy, self.seller.profile.canSell), (True, True))

    def test_start_selling_requires(self):
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("start_selling"))
        self.assertContains(response, "Start selling")
        self.buyer.profile.refresh_from_db()
        self.assertFalse(self.buyer.profile.canSell)
        self.assertNotIn("marketplaceMode", self.client.session)
        response = self.client.post(reverse("start_selling"))
        self.assertRedirects(response, reverse("sell"))
        self.buyer.profile.refresh_from_db()
        self.assertEqual((self.buyer.profile.canBuy, self.buyer.profile.canSell), (True, True))
        self.assertEqual(self.buyer.profile.role, "buyer")
        self.assertEqual(self.client.session["_auth_user_id"], str(self.buyer.pk))
        self.assertEqual(self.client.session["marketplaceMode"], "seller")
        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(self.client.get(reverse("saved")).status_code, 200)

    def test_buyer_enable_selling_publish(self):
        self.client.force_login(self.buyer)
        self.client.post(reverse("start_selling"))
        token = self.client.get(reverse("sell")).context["draftToken"]
        data = {**FLAT, "draftToken": token, **{f"photo_{room}": jpeg() for room in ROOMS}}
        with TemporaryDirectory() as mediaRoot, override_settings(MEDIA_ROOT=mediaRoot):
            with patch(
                "sellerpanel.photos.checkPics",
                side_effect=lambda photos: [dict(CLEAN)] * len(photos),
            ):
                with patch("sellerpanel.views.band", return_value=FAIR):
                    response = self.client.post(reverse("sell"), data)
        flat = Listing.objects.get(seller=self.buyer)
        self.assertRedirects(
            response,
            reverse("seller_viewings") + "?first=1#slot-form",
            fetch_redirect_response=False,
        )
        self.assertEqual(flat.status, "live")
        self.assertRedirects(self.client.get(reverse("sell")), reverse("my_listings"))
        self.client.post(reverse("sell"), {**FLAT, "draftToken": "second-flat"})
        self.assertEqual(Listing.objects.filter(seller=self.buyer).count(), 1)
        self.assertEqual(self.client.get(reverse("my_offers")).status_code, 200)

    def test_seller_buy_book_elsewhere(self):
        self.client.force_login(self.seller)
        response = self.client.get(reverse("home"))
        self.assertEqual(response.context["currentMode"], "seller")
        self.assertContains(response, 'name="mode" value="seller" aria-pressed="true"')
        self.assertRedirects(self.client.post(reverse("switch_mode"), {"mode": "buyer"}), reverse("listings"))
        otherFlat = makeListing(makeUser("other@test.com", "seller"))
        self.act("offer", otherFlat.publicId, amount="550000")
        self.assertTrue(Offer.objects.filter(buyer=self.seller, listing=otherFlat).exists())
        self.act("offer", self.flat.publicId, amount="550000")
        self.assertFalse(Offer.objects.filter(buyer=self.seller, listing=self.flat).exists())
        ownSlot = ViewingSlot.objects.create(listing=self.flat, startsAt=timezone.now() + timedelta(days=1))
        otherSlot = ViewingSlot.objects.create(listing=otherFlat, startsAt=timezone.now() + timedelta(days=2))
        self.act("book", ownSlot.pk)
        self.assertFalse(ViewingBooking.objects.filter(buyer=self.seller, slot=ownSlot).exists())
        self.act("book", otherSlot.pk)
        self.assertTrue(ViewingBooking.objects.filter(buyer=self.seller, slot=otherSlot).exists())
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_header_switch_phone_menus(self):
        self.enableBoth(self.buyer)
        response = self.client.get(reverse("home"))
        for check_text_1 in (
            "Buying or selling",
            'name="mode" value="buyer" aria-pressed="true"',
            "<span>Saved</span>",
        ):
            self.assertContains(response, check_text_1)
        self.assertNotContains(response, "<span>Orders</span>")
        self.assertRedirects(
            self.client.post(reverse("switch_mode"), {"mode": "seller"}), reverse("dashboard")
        )
        response = self.client.get(reverse("home"))
        self.assertEqual(response.context["currentMode"], "seller")
        self.assertContains(response, 'name="mode" value="seller" aria-pressed="true"')
        self.assertContains(response, "<span>Orders</span>")
        self.assertNotContains(response, "<span>Saved</span>")
        self.assertEqual(self.client.get(reverse("saved")).context["currentMode"], "seller")
        self.assertRedirects(self.client.post(reverse("switch_mode"), {"mode": "buyer"}), reverse("listings"))
        self.assertContains(self.client.get(reverse("home")), "<span>Saved</span>")

    def test_switch_above_mobile_navigation(self):
        for user in (self.buyer, self.seller):
            self.enableBoth(user)
            for mode in ("buyer", "seller"):
                with self.subTest(user=user.username, mode=mode):
                    self.client.post(reverse("switch_mode"), {"mode": mode})
                    response = self.client.get(reverse("home"))
                    header = (
                        response.content.decode()
                        .split('<header class="site-header', 1)[1]
                        .split("</header>", 1)[0]
                    )
                    self.assertEqual(header.count('class="mode-switch header-mode-switch"'), 1)
                    self.assertLess(
                        header.index("header-mode-switch"),
                        header.index('aria-label="Primary navigation"'),
                    )
                    self.assertLess(
                        header.index("header-mode-switch"),
                        header.index('<details class="header-account"'),
                    )
                    self.assertIn(f'name="mode" value="{mode}" aria-pressed="true"', header)
                    self.assertNotIn("Start selling", header)
                    self.assertNotIn("Start buying", header)

    def test_single_capability_accounts_keep(self):
        Profile.objects.filter(user=self.seller).update(canBuy=False)
        for user, link in ((self.buyer, "start_selling"), (self.seller, "start_buying")):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(reverse("home"))
                header = (
                    response.content.decode()
                    .split('<header class="site-header', 1)[1]
                    .split("</header>", 1)[0]
                )
                self.assertNotIn("header-mode-switch", header)
                account = header.split('<details class="header-account"', 1)[1]
                self.assertIn(f'href="{reverse(link)}"', account)
        self.client.logout()
        self.assertNotContains(self.client.get(reverse("home")), "header-mode-switch")

    def test_switch_requires_post_enabled(self):
        self.client.force_login(self.buyer)
        self.assertEqual(self.client.get(reverse("switch_mode")).status_code, 405)
        for mode in ("seller", "staff", ""):
            self.assertEqual(self.client.post(reverse("switch_mode"), {"mode": mode}).status_code, 403)
        self.buyer.profile.refresh_from_db()
        self.assertFalse(self.buyer.profile.canSell)
        self.assertNotIn("marketplaceMode", self.client.session)
        session = self.client.session
        session["marketplaceMode"] = "seller"
        session.save()
        self.assertEqual(self.client.get(reverse("home")).context["currentMode"], "buyer")

    def test_capability_prompts_replace(self):
        self.client.force_login(self.buyer)
        for page in ("home", "packages", "sell"):
            response = self.client.get(reverse(page))
            self.assertContains(response, "Start selling", status_code=403 if page == "sell" else 200)
            self.assertNotContains(response, "another email", status_code=403 if page == "sell" else 200)
        self.client.force_login(self.seller)
        otherFlat = makeListing(makeUser("other@test.com", "seller"))
        response = self.client.get(reverse("listing_detail", args=[otherFlat.publicId]))
        self.assertContains(response, "Send offer")
        self.assertNotContains(response, "Start buying")
        self.assertEqual(self.client.get(reverse("saved")).status_code, 200)

    def test_both_sides_share_notifications(
        self,
    ):
        self.enableBoth(self.buyer)
        ownFlat = makeListing(self.buyer)
        self.act("offer", self.flat.publicId, amount="550000")
        self.client.force_login(self.seller)
        self.client.post(reverse("start_buying"))
        self.act("offer", ownFlat.publicId, amount="550000")
        self.enableBoth(self.buyer)
        ownOffer = Offer.objects.get(buyer=self.buyer)
        self.act("withdraw", ownOffer.pk)
        self.act("reply", Offer.objects.get(listing=ownFlat).pk, reply="Thanks for your offer")
        order = makeOrder(self.buyer, makeProduct(), listing=ownFlat)
        ActivityNotice.objects.create(
            user=self.buyer, kind="offer_reply", text="Buying update", url=reverse("my_offers")
        )
        self.assertTrue(ActivityNotice.objects.filter(user=self.buyer, kind="offer_new").exists())
        for mode in ("buyer", "seller"):
            self.client.post(reverse("switch_mode"), {"mode": mode})
            for page in ("my_offers", "seller_viewings", "my_orders"):
                self.assertEqual(self.client.get(reverse(page)).status_code, 200)
            self.assertEqual(self.client.get(reverse("receipt", args=[order.pk])).status_code, 200)
            response = self.client.get(reverse("notifications"))
            self.assertContains(response, "Buying update")
            self.assertContains(response, "New offer")

    def test_staff_guests_suspended_accounts(self):
        for page in ("start_buying", "start_selling", "switch_mode", "new_seller_account"):
            self.assertEqual(self.client.post(reverse(page)).status_code, 302)
        self.assertFalse(hasRole(AnonymousUser(), "buyer"))
        staff = makeStaff("staff@test.com")
        Profile.objects.create(user=staff, role="seller", canBuy=True, canSell=True)
        self.client.force_login(staff)
        for page in ("start_buying", "start_selling", "switch_mode", "new_seller_account"):
            self.assertEqual(self.client.post(reverse(page), {"mode": "seller"}).status_code, 403)
        self.assertFalse(hasRole(staff, "buyer"))
        self.assertFalse(hasRole(staff, "seller"))
        self.assertNotContains(self.client.get(reverse("home")), "Buying or selling")
        self.enableBoth(self.buyer)
        self.buyer.is_active = False
        self.buyer.save(update_fields=["is_active"])
        self.assertFalse(hasRole(self.buyer, "buyer"))
        self.assertFalse(hasRole(self.buyer, "seller"))
        for page in ("start_buying", "start_selling", "switch_mode", "saved", "sell"):
            self.assertEqual(self.client.post(reverse(page), {"mode": "seller"}).status_code, 302)
        self.act("offer", self.flat.publicId, amount="550000")
        self.assertFalse(Offer.objects.filter(buyer=self.buyer).exists())


class CapabilityMigrationTests(TransactionTestCase):

    def test_seller_buying_upgrades(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        before = [("storefront", "0046_search")]
        after = [("storefront", "0051_deals")]
        executor.migrate(before)
        oldApps = executor.loader.project_state(before).apps
        users = oldApps.get_model("auth", "User")
        profiles = oldApps.get_model("storefront", "Profile")
        cases = (
            ("seller", "seller", False, True, False, True),
            ("buyerSeller", "buyer", False, True, False, True),
            ("both", "seller", True, True, False, True),
            ("suspendedSeller", "seller", False, True, False, False),
            ("buyer", "buyer", True, False, False, True),
            ("noSelling", "seller", False, False, False, True),
            ("staff", "seller", False, True, True, True),
            ("staffBuyer", "seller", True, True, True, True),
        )
        expected = {}
        for name, role, canBuy, canSell, staff, active in cases:
            user = users.objects.create(username=name, is_staff=staff, is_active=active)
            profiles.objects.create(user=user, role=role, canBuy=canBuy, canSell=canSell)
            expected[name] = (role, canBuy or (canSell and (not staff)), canSell, staff, active)
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        profiles = executor.loader.project_state(after).apps.get_model("storefront", "Profile")
        actual = {
            name: (role, canBuy, canSell, staff, active)
            for name, role, canBuy, canSell, staff, active in profiles.objects.values_list(
                "user__username", "role", "canBuy", "canSell", "user__is_staff", "user__is_active"
            )
        }
        self.assertEqual(actual, expected)
        executor.migrate(before)
        self.assertEqual(profiles.objects.filter(canBuy=True, user__is_staff=False).count(), 5)
        self.assertFalse(profiles.objects.get(user__username="staff").canBuy)

    def test_keeps_existing_modes_maps(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        before = [("storefront", "0031_checks")]
        after = [("storefront", "0042_profile")]
        executor.migrate(before)
        oldApps = executor.loader.project_state(before).apps
        oldUsers = oldApps.get_model("auth", "User")
        oldProfiles = oldApps.get_model("storefront", "Profile")
        for name, role, staff, active in (
            ("buyer", "buyer", False, True),
            ("seller", "seller", False, True),
            ("suspended", "buyer", False, False),
            ("staff", "seller", True, True),
        ):
            user = oldUsers.objects.create(username=name, is_staff=staff, is_active=active)
            oldProfiles.objects.create(user=user, role=role)
        executor = MigrationExecutor(connection)
        executor.migrate(after)
        profiles = executor.loader.project_state(after).apps.get_model("storefront", "Profile")
        self.assertEqual(
            list(
                profiles.objects.order_by("user__username").values_list(
                    "user__username", "role", "canBuy", "canSell"
                )
            ),
            [
                ("buyer", "buyer", True, False),
                ("seller", "seller", False, True),
                ("staff", "seller", False, False),
                ("suspended", "buyer", True, False),
            ],
        )
