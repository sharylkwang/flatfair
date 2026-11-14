import csv
from datetime import timedelta
from importlib import import_module
import io
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from adminpanel.access import PAGES, canOpen, menu, staffRole
from adminpanel.models import AuditLog, Ticket
from storefront.models import (
    Collection,
    Offer,
    Product,
    Profile,
    SavedListing,
    ViewingBooking,
    ViewingSlot,
)
from storefront.tests.helpers import makeListing, makeOrder, makeProduct, makeStaff, makeUser


class CustomerTests(TestCase):

    def setUp(self):
        self.admin = makeStaff("admin@test.com")
        self.seller = makeUser("asdf@test.com", "seller", name="Tan")
        self.buyer = makeUser("qwer@test.com", name="Priya")
        self.client.force_login(self.admin)

    def test_search_filters_role_name(self):
        makeListing(self.seller)
        people = self.client.get(reverse("admin_customers")).context["people"]
        self.assertEqual([p.email for p in people], ["qwer@test.com", "asdf@test.com"])
        seller = [p for p in people if p.pk == self.seller.pk][0]
        self.assertEqual(seller.listingCount, 1)
        found = self.client.get(reverse("admin_customers"), {"role": "seller"}).context["people"]
        self.assertEqual([p.pk for p in found], [self.seller.pk])
        found = self.client.get(reverse("admin_customers"), {"q": "priya"}).context["people"]
        self.assertEqual([p.pk for p in found], [self.buyer.pk])

    def test_contact_edit_cleans_email(self):
        url = reverse("admin_customer", args=[self.buyer.pk])
        self.client.post(url, {"first_name": "Priya Nair", "email": "NEW@test.com"})
        self.buyer.refresh_from_db()
        self.assertEqual(
            (self.buyer.first_name, self.buyer.email, self.buyer.username),
            ("Priya Nair", "new@test.com", "new@test.com"),
        )
        response = self.client.post(url, {"first_name": "Priya", "email": "asdf@test.com"})
        self.assertIn("email", response.context["form"].errors)
        self.assertTrue(AuditLog.objects.filter(action="customer edited").exists())

    def test_upgraded_buyer_seller_filters(self):
        self.client.force_login(self.buyer)
        self.client.post(reverse("start_selling"))
        self.buyer.profile.refresh_from_db()
        self.assertEqual(self.buyer.profile.role, "buyer")
        self.assertTrue(self.buyer.profile.canSell)
        flat = makeListing(self.buyer)
        order = makeOrder(self.buyer, makeProduct(), listing=flat)
        self.client.force_login(self.admin)
        for role in ("seller", "buyer"):
            response = self.client.get(reverse("admin_customers"), {"role": role})
            person = next((person for person in response.context["people"] if person.pk == self.buyer.pk))
            self.assertEqual((person.listingCount, person.orderCount), (1, 1))
            self.assertContains(response, "Buyer, Seller")
            response = self.client.get(reverse("admin_export_customers"), {"role": role})
            rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
            row = next((row for row in rows if row["Email"] == self.buyer.email))
            self.assertEqual(row["Role"], "Buyer, Seller")
        response = self.client.get(reverse("admin_customer", args=[self.buyer.pk]))
        self.assertTrue(response.context["isSeller"])
        self.assertContains(response, "<h2>Listings</h2>")
        self.assertContains(response, "<h2>Orders</h2>")
        self.assertContains(response, f"Order #{order.pk}")
        url = reverse("admin_customer_suspend", args=[self.buyer.pk])
        response = self.client.post(url, {"action": "suspend", "reason": "Reports checked"}, follow=True)
        flat.refresh_from_db()
        self.assertEqual(flat.status, "hidden")
        self.assertContains(response, "Priya is suspended. 1 listing hidden.")
        self.assertEqual(
            AuditLog.objects.get(action="account suspended").detail,
            "Reports checked (1 listings hidden)",
        )
        response = self.client.post(url, {"action": "reactivate", "reason": "Account checked"}, follow=True)
        flat.refresh_from_db()
        self.assertEqual(flat.status, "review")
        self.assertContains(response, "Priya is active again. 1 listing back in review.")
        self.assertEqual(
            AuditLog.objects.get(action="account reactivated").detail,
            "Account checked (1 listings sent to review)",
        )

    def test_seller_capability_historical(self):
        for source in ("capability", "listing", "order"):
            with self.subTest(source=source):
                person = makeUser(f"{source}@test.com")
                if source == "capability":
                    Profile.objects.filter(user=person).update(canSell=True)
                elif source == "listing":
                    makeListing(person, status="hidden")
                else:
                    makeOrder(person, makeProduct())
                    person.profile.delete()
                response = self.client.get(reverse("admin_customers"), {"role": "seller"})
                self.assertIn(person.pk, [person.pk for person in response.context["people"]])
                response = self.client.get(reverse("admin_customer", args=[person.pk]))
                self.assertTrue(response.context["isSeller"])
                self.assertContains(response, "<h2>Listings</h2>")
                self.assertContains(response, "<h2>Orders</h2>")

    def test_buyer_history_identifies_buyers(self):
        flat = makeListing(self.seller)
        slot = ViewingSlot.objects.create(listing=flat, startsAt=timezone.now() + timedelta(days=1))
        for source in ("offer", "booking", "save"):
            with self.subTest(source=source):
                person = makeUser(f"{source}@test.com", "seller")
                Profile.objects.filter(user=person).update(canBuy=False)
                if source == "offer":
                    Offer.objects.create(listing=flat, buyer=person, amount=500000)
                    Offer.objects.create(
                        listing=makeListing(self.seller, status="hidden"),
                        buyer=person,
                        amount=500000,
                    )
                elif source == "booking":
                    ViewingBooking.objects.create(slot=slot, buyer=person)
                else:
                    collection = Collection.objects.create(user=person, name="Homes")
                    SavedListing.objects.create(collection=collection, listing=flat)
                response = self.client.get(reverse("admin_customers"), {"role": "buyer"})
                self.assertEqual([row.pk for row in response.context["people"]].count(person.pk), 1)
                response = self.client.get(reverse("admin_export_customers"), {"role": "buyer"})
                rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
                self.assertEqual(
                    next((row for row in rows if row["Email"] == person.email))["Role"],
                    "Buyer, Seller",
                )

    def test_suspension_requires_reason(self):
        live = makeListing(self.seller)
        url = reverse("admin_customer_suspend", args=[self.seller.pk])
        self.client.post(url, {"action": "suspend", "reason": ""})
        self.seller.refresh_from_db()
        self.assertTrue(self.seller.is_active)
        self.client.post(url, {"action": "suspend", "reason": "Scam reports"})
        self.seller.refresh_from_db()
        live.refresh_from_db()
        self.assertFalse(self.seller.is_active)
        self.assertEqual(live.status, "hidden")
        self.client.post(url, {"action": "reactivate", "reason": "Account checked"})
        live.refresh_from_db()
        self.assertEqual(live.status, "review")
        self.assertEqual(
            AuditLog.objects.filter(target__startswith=f"Customer #{self.seller.pk} ").count(), 2
        )

    def test_admin_can_suspend_customers(self):
        self.client.force_login(makeStaff("admin2@test.com", "Admin"))
        response = self.client.get(reverse("admin_customer", args=[self.seller.pk]))
        self.assertTrue(response.context["canSuspend"])
        self.client.post(
            reverse("admin_customer_suspend", args=[self.seller.pk]),
            {"action": "suspend", "reason": "asdf"},
        )
        self.seller.refresh_from_db()
        self.assertFalse(self.seller.is_active)

    def test_admins_require_reasons_suspend(self):
        url = reverse("admin_customer_suspend", args=[self.buyer.pk])
        for user in (self.admin, makeStaff("reason-admin2@test.com", "Admin")):
            self.client.force_login(user)
            for action, active in (("suspend", True), ("reactivate", False)):
                self.buyer.is_active = active
                self.buyer.save()
                before = AuditLog.objects.count()
                for reason in ("", " " * 5, "x" * 201):
                    self.client.post(url, {"action": action, "reason": reason})
                    self.buyer.refresh_from_db()
                    self.assertEqual(self.buyer.is_active, active)
                    self.assertEqual(AuditLog.objects.count(), before)
                self.client.post(url, {"action": action, "reason": "Checked account"})
                self.buyer.refresh_from_db()
                self.assertEqual(self.buyer.is_active, not active)
                self.assertTrue(AuditLog.objects.filter(actor=user, detail="Checked account").exists())
                if not active:
                    self.buyer.is_active = False
                    self.buyer.save()
                    response = self.client.get(reverse("admin_customer", args=[self.buyer.pk]))
                    self.assertContains(response, 'name="reason"', count=1)
                    self.assertContains(response, 'maxlength="200" required')

    def test_reactivation_restores_newest(self):
        older = makeListing(self.seller, status="hidden", createdAt=timezone.now() - timedelta(days=1))
        newest = makeListing(self.seller, status="hidden")
        self.seller.is_active = False
        self.seller.save()
        response = self.client.post(
            reverse("admin_customer_suspend", args=[self.seller.pk]),
            {"action": "reactivate", "reason": "Account checked"},
            follow=True,
        )
        older.refresh_from_db()
        newest.refresh_from_db()
        self.assertEqual((older.status, newest.status), ("hidden", "review"))
        self.assertIsNotNone(newest.reviewSince)
        self.assertContains(response, "Other listings remain hidden.")
        self.assertContains(response, "This seller already has an open flat.")

    def test_reactivation_keeps_hidden_flats(self):
        for status in ("live", "review"):
            with self.subTest(status=status):
                self.seller.is_active = False
                self.seller.save()
                openFlat = makeListing(self.seller, status=status)
                hidden = makeListing(self.seller, status="hidden")
                self.client.post(
                    reverse("admin_customer_suspend", args=[self.seller.pk]),
                    {"action": "reactivate", "reason": "Account checked"},
                )
                hidden.refresh_from_db()
                self.assertEqual(hidden.status, "hidden")
                self.assertEqual(self.seller.listings.filter(status__in=("live", "review")).count(), 1)
                openFlat.delete()
                hidden.delete()

    def test_staff_urls_invalid_actions(self):
        self.assertRedirects(
            self.client.get(reverse("admin_customer", args=[self.admin.pk])),
            reverse("admin_customers"),
        )
        response = self.client.post(
            reverse("admin_customer_suspend", args=[self.seller.pk]), {"action": "asdf"}
        )
        self.assertEqual(response.status_code, 400)

    def test_detail_shows_listings_offers(self):
        flat = makeListing(self.seller)
        hidden = makeListing(self.seller, status="hidden")
        Offer.objects.create(listing=flat, buyer=self.buyer, amount=500000)
        slot = ViewingSlot.objects.create(listing=flat, startsAt=timezone.now() + timedelta(days=1))
        ViewingBooking.objects.create(slot=slot, buyer=self.buyer)
        Ticket.objects.create(customer=self.buyer, email="qwer@test.com", topic="other", message="asdf")
        response = self.client.get(reverse("admin_customer", args=[self.seller.pk]))
        public = {f.pk: f.isPublic for f in response.context["listings"]}
        self.assertEqual(public, {flat.pk: True, hidden.pk: False})
        self.assertEqual(response.context["liveCount"], 1)
        response = self.client.get(reverse("admin_customer", args=[self.buyer.pk]))
        self.assertEqual(
            (
                len(response.context["offers"]),
                len(response.context["bookings"]),
                len(response.context["tickets"]),
            ),
            (1, 1, 1),
        )
        self.assertEqual(
            response.context["customerMailto"],
            "mailto:qwer@test.com?subject=Your%20FlatFair%20account",
        )

    def test_buyer_detail_omits_seller(self):
        response = self.client.get(reverse("admin_customer", args=[self.buyer.pk]))
        self.assertFalse(response.context["isSeller"])
        for check_text_1 in ("<h2>Listings</h2>", "<h2>Orders</h2>", "listings are hidden"):
            self.assertNotContains(response, check_text_1)
        response = self.client.post(
            reverse("admin_customer_suspend", args=[self.buyer.pk]),
            {"action": "suspend", "reason": "Spam"},
            follow=True,
        )
        self.assertContains(response, "Priya is suspended and can no longer sign in.")
        response = self.client.get(reverse("admin_customer", args=[self.seller.pk]))
        self.assertContains(response, "<h2>Listings</h2>")

    def test_contact_edit_handles_email(self):
        url = reverse("admin_customer", args=[self.buyer.pk])
        with patch("adminpanel.forms.emailTaken", return_value=False):
            response = self.client.post(url, {"first_name": "Priya", "email": self.seller.email})
        self.assertEqual(response.status_code, 200)
        self.assertIn("email", response.context["form"].errors)
        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.email, "qwer@test.com")
        self.assertFalse(AuditLog.objects.filter(action="customer edited").exists())


class CatalogueTests(TestCase):

    def setUp(self):
        self.admin = makeStaff("admin@test.com")
        self.client.force_login(self.admin)

    def test_sold_count_excludes_refunded(self):
        product = makeProduct("Floor plan", "addon", 40, None)
        makeOrder(makeUser("asdf@test.com", "seller"), product, status="completed")
        makeOrder(makeUser("qwer@test.com", "seller"), product, status="refunded")
        products = self.client.get(reverse("admin_catalogue")).context["products"]
        self.assertEqual(products[0].sold, 1)

    def test_product_creation_requires(self):
        data = {
            "name": "Boost 60 days",
            "kind": "package",
            "price": "99",
            "description": "",
            "durationDays": "60",
            "isActive": "on",
        }
        response = self.client.post(reverse("admin_product_new"), data)
        self.assertIn("tier", response.context["form"].errors)
        response = self.client.post(reverse("admin_product_new"), {**data, "tier": "2"})
        self.assertIn("must last 90 days", str(response.context["form"].errors))
        self.assertFalse(Product.objects.exists())
        self.client.post(
            reverse("admin_product_new"),
            {**data, "name": "Boost 90 days", "durationDays": "90", "tier": "2"},
        )
        self.assertEqual((Product.objects.get().durationDays, Product.objects.get().tier), (90, 2))
        self.client.post(
            reverse("admin_product_new"), {**data, "name": "Staging", "kind": "addon", "tier": "3"}
        )
        self.assertEqual(Product.objects.get(name="Staging").durationDays, None)
        self.assertEqual(Product.objects.get(name="Staging").tier, 0)

    def test_product_edit_keeps_kind(self):
        product = makeProduct("Top spot", price=299, days=None)
        url = reverse("admin_product_edit", args=[product.pk])
        response = self.client.get(url)
        self.assertEqual(response.context["form"].packageType, "Top spot")
        self.client.post(
            url,
            {
                "name": "asdf",
                "kind": "addon",
                "price": "350",
                "description": "qwer",
                "isActive": "on",
            },
        )
        product.refresh_from_db()
        self.assertEqual(
            (product.name, product.kind, product.price, product.tier), ("asdf", "package", 350, 3)
        )
        response = self.client.post(url, {"price": "99999999", "description": ""})
        self.assertIn("price", response.context["form"].errors)

    def test_hiding_product_idempotent(self):
        product = makeProduct()
        url = reverse("admin_product_toggle", args=[product.pk])
        self.client.post(url, {"show": "0"})
        product.refresh_from_db()
        self.assertFalse(product.isActive)
        self.client.post(url, {"show": "0"})
        product.refresh_from_db()
        self.assertFalse(product.isActive)
        self.assertEqual(AuditLog.objects.filter(action="product hidden").count(), 1)

    def test_invalid_visibility_input_keeps(self):
        product = makeProduct()
        url = reverse("admin_product_toggle", args=[product.pk])
        for data in ({}, {"show": "invalid"}):
            with self.subTest(data=data):
                self.client.post(url, data)
                product.refresh_from_db()
                self.assertTrue(product.isActive)
        self.assertFalse(AuditLog.objects.filter(action="product hidden").exists())


class AdminAccessTests(TestCase):

    def setUp(self):
        self.admin = makeStaff("admin@test.com")
        self.client.force_login(self.admin)

    def test_active_staff_member_missing(self):
        self.admin.groups.clear()
        self.assertEqual(staffRole(self.admin), "Admin")
        for name in [*PAGES, "admin_account", "admin_payments"]:
            with self.subTest(page=name):
                self.assertTrue(canOpen(self.admin, name))
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        self.assertEqual(len(menu(self.admin)), len(PAGES))

    def test_customers_guests_blocks_open(self):
        customer = makeUser("customer@test.com")
        self.client.force_login(customer)
        for name in [*PAGES, "admin_account", "admin_payments"]:
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 403)
                self.assertNotContains(response, "Not part of your role", status_code=403)
        self.client.logout()
        for name in PAGES:
            self.assertEqual(self.client.get(reverse(name)).status_code, 302)
        self.admin.is_active = False
        self.assertIsNone(staffRole(self.admin))

    def test_removed_switch_role_urls(self):
        for url in ("/staff/view", f"/staff/staff/{self.admin.pk}/role"):
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(url, {"view": "support"}).status_code, 404)

    def test_menu_groups_closed_more(self):
        response = self.client.get(reverse("admin_overview"))
        self.assertEqual(
            [
                (group["label"], [item["label"] for item in group["items"]])
                for group in response.context["menuGroups"]
            ],
            [
                ("To do", ["Overview", "Review queue", "Orders", "Support tickets"]),
                ("Customers", ["Customers", "Deals", "Reviews", "Community"]),
                ("Catalogue & settings", ["Listings", "Catalogue", "Admins"]),
            ],
        )
        self.assertEqual(
            [item["label"] for item in response.context["moreMenu"]],
            ["Price check accuracy", "Audit log"],
        )
        html = response.content.decode()
        self.assertIn('<details class="staff-nav-more">', html)
        self.assertNotIn('<details class="staff-nav-more" open', html)
        self.assertNotIn("Admin | Support", html)
        self.assertNotIn("admin_switch_view", html)


class AdminManagementTests(TestCase):

    def setUp(self):
        self.admin = makeStaff("admin@test.com")
        self.other = makeStaff("admin2@test.com")
        self.client.force_login(self.admin)

    def test_add_cleans_email_audits(self):
        response = self.client.post(
            reverse("admin_staff"),
            {"fullName": "Lim Jia Hui", "email": "New@Test.com", "password": "Qwer-5678!zx"},
        )
        self.assertRedirects(response, reverse("admin_staff"))
        member = User.objects.get(username="new@test.com")
        self.assertTrue(member.is_staff)
        self.assertEqual(staffRole(member), "Admin")
        self.assertTrue(
            AuditLog.objects.filter(action="staff added", target=member.email, detail="Admin").exists()
        )

    def test_add_blocks_duplicate_weak(self):
        data = {"fullName": "New Admin", "email": self.other.email, "password": "Qwer-5678!zx"}
        self.assertIn("email", self.client.post(reverse("admin_staff"), data).context["form"].errors)
        data.update(email="new@test.com", password="password")
        self.assertIn("password", self.client.post(reverse("admin_staff"), data).context["form"].errors)
        self.assertEqual(User.objects.filter(is_staff=True).count(), 2)

    def test_page_status_removal_missing(self):
        response = self.client.get(reverse("admin_staff"))
        for text in ("Admins", "Add an admin", "Remove access"):
            self.assertContains(response, text)
        for text in ('name="role"', "Who can open what", "Admin | Support", "Staff &amp; roles"):
            self.assertNotContains(response, text)
        self.assertNotContains(response, reverse("admin_staff_remove", args=[self.admin.pk]))
        self.assertContains(response, reverse("admin_staff_remove", args=[self.other.pk]))

    def test_remove_other_disables_access(self):
        self.other.is_superuser = True
        self.other.save(update_fields=["is_superuser"])
        entry = AuditLog.objects.create(actor=self.other, action="staff added", target="old@test.com")
        self.client.post(reverse("admin_staff_remove", args=[self.other.pk]))
        self.other.refresh_from_db()
        self.assertFalse(self.other.is_staff)
        self.assertFalse(self.other.is_active)
        self.assertEqual(AuditLog.objects.get(pk=entry.pk).actor_id, self.other.pk)
        self.assertTrue(AuditLog.objects.filter(action="staff removed", target=self.other.email).exists())
        self.client.post(
            reverse("admin_customer_suspend", args=[self.other.pk]),
            {"action": "reactivate", "reason": "Restore customer sign-in"},
        )
        self.other.refresh_from_db()
        self.assertTrue(self.other.is_active)
        self.assertFalse(self.other.is_staff)
        self.assertFalse(self.other.has_perm("auth.change_user"))
        self.assertFalse(self.other.is_superuser)

    def test_blocks_remove_self_last(self):
        self.other.is_active = False
        self.other.save(update_fields=["is_active"])
        self.client.post(reverse("admin_staff_remove", args=[self.admin.pk]))
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_staff)
        self.assertTrue(self.admin.is_active)
        self.assertFalse(AuditLog.objects.filter(action="staff removed").exists())

    def test_removal_creation_recheck(self):

        def revokeAccess(user, page):
            allowed = canOpen(user, page)
            User.objects.filter(pk=user.pk).update(is_staff=False)
            return allowed

        for url, data in (
            (reverse("admin_staff_remove", args=[self.other.pk]), {}),
            (
                reverse("admin_staff"),
                {"fullName": "New", "email": "new@test.com", "password": "Qwer-5678!zx"},
            ),
        ):
            User.objects.filter(pk=self.admin.pk).update(is_staff=True)
            with patch("adminpanel.access.canOpen", side_effect=revokeAccess):
                self.assertEqual(self.client.post(url, data).status_code, 403)
        self.other.refresh_from_db()
        self.assertTrue(self.other.is_staff)
        self.assertFalse(User.objects.filter(email="new@test.com").exists())
        self.assertFalse(AuditLog.objects.exists())


class AdminMigrationTests(TestCase):

    def test_old_group_becomes_demo(self):
        legacy = makeStaff("legacy@test.com", "Support")
        demo = makeStaff("support@flatfair.sg", "Support")
        entry = AuditLog.objects.create(actor=demo, action="ticket resolved", target="Ticket #1")
        migration = import_module("adminpanel.migrations.0007_staff")
        migration.old_0011_unifyAdmins(apps, None)
        legacy.refresh_from_db()
        demo.refresh_from_db()
        self.assertTrue(legacy.is_staff)
        self.assertTrue(legacy.is_active)
        self.assertTrue(legacy.groups.filter(name="Admin").exists())
        self.assertFalse(Group.objects.filter(name="Support").exists())
        self.assertFalse(demo.is_staff)
        self.assertFalse(demo.is_active)
        self.assertEqual(AuditLog.objects.get(pk=entry.pk).actor_id, demo.pk)
        migration.old_0011_unifyAdmins(apps, None)
        self.assertEqual(User.objects.filter(pk=demo.pk).count(), 1)
