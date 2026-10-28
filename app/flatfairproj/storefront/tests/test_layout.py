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
