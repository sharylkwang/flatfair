import csv
from datetime import timedelta
import io
from unittest.mock import patch

from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from adminpanel.models import AuditLog, Decision, Ticket, TicketMessage
from adminpanel.reports import auditRows
from adminpanel.views import flatWeeks, REPLY_CHOICES
from forum.models import Post, Report
from storefront.models import ListingPhoto, Payment, Review
from storefront.tests.helpers import (
    PASSWORD,
    makeListing,
    makeOrder,
    makeProduct,
    makeStaff,
    makeUser,
    CLEAN,
)
from storefront.tests.helpers import RoleMarkup


class OverviewTests(TestCase):

    def setUp(self):
        self.admin = makeStaff("admin@test.com", name="Lim")
        self.seller = makeUser("asdf@test.com", "seller")
        self.flat = makeListing(self.seller, status="review")
        makeOrder(self.seller, makeProduct("Floor plan", "addon", 40, None))
        Ticket.objects.create(email="qwer@test.com", topic="other", message="asdf")

    def test_admin_dashboard_shows_four(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_overview"))
        content = response.content.decode()
        markup = RoleMarkup(content)
        self.assertEqual(markup.defaultKpis, 0)
        self.assertNotContains(response, "page-hero")
        self.assertNotContains(response, "<table")
        self.assertEqual(len(markup.moreSections), 1)
        self.assertNotIn("open", markup.moreSections[0])
        before, more = content.split('id="staff-more"', 1)
        self.assertEqual(before.split('class="dash-kpis"', 1)[1].split("</dl>", 1)[0].count("<dt>"), 4)
        self.assertEqual(before.count('<figure class="chart">'), 4)
        for title in (
            "New listings each week",
            "Sales each week",
            "What sells best",
            "Listings by status",
        ):
            self.assertIn(f"<h2>{title}</h2>", before)
        self.assertEqual(before.count('class="chart-cols"'), 2)
        self.assertIn("Week of", before)
        self.assertIn("Recent orders", before)
        for fact in ("Sales so far (before GST)", "Orders with an extra service"):
            self.assertIn(fact, more)
        self.assertNotIn("<figure", more)
        shares = response.context["statusShares"]
        self.assertEqual([part["key"] for part in shares], ["review"])
        self.assertEqual(shares[0]["share"], 100.0)
        self.assertEqual(len(response.context["salesWeeks"]), 8)
        self.assertEqual(response.context["month"]["orders"], 1)
        self.assertEqual(
            response.context["reasonCounts"], {"price": 0, "photos": 0, "other": 1, "documents": 0}
        )
        self.assertIn(f'href="{reverse('admin_queue')}?reason=other">1 held for another reason</a>', before)
        for reason in ("price", "photos"):
            self.assertNotIn(f'href="{reverse('admin_queue')}?reason={reason}"', before)

    def test_ranks_seven_tasks_by(self):
        self.client.force_login(self.admin)
        for number in range(3):
            Ticket.objects.create(email=f"ticket{number}@test.com", topic="other", message="Help")
        response = self.client.get(reverse("admin_overview"))
        actions = response.context["startActions"] + response.context["alsoWaiting"]
        self.assertEqual(len(actions), 7)
        self.assertEqual([action["count"] for action in actions], [4, 1, 1, 0, 0, 0, 0])
        self.assertEqual(
            [action["title"] for action in actions[:3]],
            ["Open tickets", "Review queue", "Orders to act on"],
        )
        self.assertEqual(
            [action["title"] for action in actions[3:]],
            ["Refunds waiting", "Reported posts", "New reviews", "Stuck deals"],
        )
        self.assertNotContains(response, "Also waiting")
        self.assertEqual(
            [action["title"] for action in response.context["todoActions"]],
            ["Open tickets", "Review queue", "Orders to act on"],
        )
        self.assertContains(response, 'class="staff-row todo-line todo-line--', count=3)
        self.assertContains(response, "todo-line--soon", count=3)
        self.assertNotContains(response, "?tab=")
        self.assertEqual(response.context["revenue"], 40)

    def test_task_ties_oldest_fixed(self):
        from adminpanel.views import hotTasks
        from storefront.models import Listing, Order

        now = timezone.now() - timedelta(days=3)
        Ticket.objects.update(createdAt=now)
        self.assertEqual(hotTasks([])["startActions"][0]["title"], "Open tickets")
        Listing.objects.update(createdAt=now)
        Order.objects.update(createdAt=now)
        self.assertEqual(
            [action["title"] for action in hotTasks([])["startActions"]],
            ["Review queue", "Orders to act on", "Open tickets"],
        )
        Ticket.objects.update(status="resolved")
        Listing.objects.update(status="live")
        Order.objects.update(status="completed")
        actions = hotTasks([])
        self.assertEqual(len(actions["startActions"]), 3)
        self.assertTrue(
            all((action["count"] == 0 for action in actions["startActions"] + actions["alsoWaiting"]))
        )

    def test_counts_include_refunds_recent(self):
        product = makeProduct()
        for age in (0, 29, 31):
            item = makeOrder(self.seller, product, status="completed").items.get()
            Review.objects.create(
                seller=self.seller,
                orderItem=item,
                rating=2,
                createdAt=timezone.now() - timedelta(days=age),
            )
        post = Post.objects.create(author=self.seller, title="Reported", body="Question")
        Report.objects.create(reporter=self.seller, post=post, reason="spam")
        Report.objects.create(reporter=makeUser("reporter@test.com"), post=post, reason="rude")
        hidden = Post.objects.create(author=self.seller, body="Hidden", hidden=True)
        Report.objects.create(reporter=self.seller, post=hidden, reason="spam")
        order = makeOrder(self.seller, makeProduct("Refund service", "addon", 40, None))
        Ticket.objects.create(
            customer=self.seller,
            email=self.seller.email,
            topic="refund",
            message="Refund",
            order=order,
        )
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_overview"))
        counts = {
            action["title"]: action["count"]
            for action in response.context["startActions"] + response.context["alsoWaiting"]
        }
        self.assertEqual(counts["New reviews"], 2)
        self.assertEqual(counts["Reported posts"], 1)
        self.assertEqual(counts["Refunds waiting"], 1)
        self.assertEqual(counts["Open tickets"], 2)

    def test_admin_attention_includes(self):
        makeOrder(self.seller, makeProduct("Photography", "addon", 50, None), status="processing")
        Payment.objects.create(
            seller=self.seller,
            method="stripe",
            status="paid",
            amount=40,
            checkoutToken="overview-attention",
            needsAttention=True,
        )
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_overview"))
        self.assertContains(response, "1 to start · 1 in progress")
        self.assertContains(response, "Payments to check")
        self.assertNotContains(response, "All tasks")
        self.assertNotContains(response, "Oldest in the queue")
        self.assertContains(response, reverse("admin_payments"))
        counts = {
            item["page"]: item["count"] for group in response.context["menuGroups"] for item in group["items"]
        }
        self.assertEqual(counts["admin_orders"], 3)
        self.assertContains(
            self.client.get(reverse("admin_orders")),
            f'href="{reverse('admin_payments')}">Check payments</a>',
        )
        for text in ("Moderation", "Listings to check", "Staff panel", "Staff overview"):
            self.assertNotContains(response, text)
        self.assertContains(response, "Review queue")


class AuditTests(TestCase):

    def setUp(self):
        self.admin = makeStaff("admin@test.com", password=PASSWORD)
        self.other = makeStaff("zxcv@test.com")
        self.seller = makeUser("asdf@test.com", "seller", name="Tan")
        self.client.force_login(self.admin)

    def test_entries_link_orders_customers(self):
        AuditLog.objects.create(actor=self.admin, action="order status", target="Order #5")
        AuditLog.objects.create(
            actor=self.other,
            action="account suspended",
            target=f"Customer #{self.seller.pk} (asdf@test.com)",
        )
        AuditLog.objects.create(actor=self.admin, action="export", target="orders")
        entries = {e.action: e for e in self.client.get(reverse("admin_audit")).context["entries"]}
        self.assertEqual(entries["order status"].recordUrl, reverse("admin_order", args=[5]))
        self.assertEqual(
            (entries["order status"].recordKind, entries["order status"].recordLabel),
            ("Order", "#5"),
        )
        self.assertEqual(entries["account suspended"].recordLabel, "Tan")
        self.assertEqual(entries["export"].recordLabel, "Order list (CSV)")
        self.assertEqual(entries["export"].actionLabel, "Downloaded a list")

    def test_record_links_weekly_counts(self):
        for number in range(5):
            person = makeUser(f"user{number}@test.com", "seller")
            flat = makeListing(person)
            AuditLog.objects.create(
                actor=self.admin, action="account suspended", target=f"Customer #{person.pk} (x)"
            )
            AuditLog.objects.create(
                actor=self.admin, action="listing taken down", target=f"Listing #{flat.pk} (y)"
            )
        entries = list(AuditLog.objects.all())
        with self.assertNumQueries(3):
            auditRows(entries)
        self.assertTrue(all((entry.recordUrl for entry in entries)))
        with self.assertNumQueries(1):
            weeks = flatWeeks(timezone.localtime())
        self.assertEqual((len(weeks), weeks[-1]["count"]), (8, 5))

    def test_filters_accept_valid_actor(self):
        AuditLog.objects.create(actor=self.admin, action="staff added", target="a@test.com")
        AuditLog.objects.create(actor=self.other, action="product added", target="asdf")
        self.assertEqual(
            len(self.client.get(reverse("admin_audit"), {"action": "staff added"}).context["entries"]),
            1,
        )
        response = self.client.get(reverse("admin_audit"), {"actor": self.other.pk})
        self.assertEqual([e.action for e in response.context["entries"]], ["product added"])
        response = self.client.get(reverse("admin_audit"), {"actor": "asdf"})
        self.assertEqual(len(response.context["entries"]), 2)
        self.assertTrue(response.context["filtered"])

    def test_password_change_requires_old(self):
        url = reverse("admin_account")
        response = self.client.post(
            url,
            {
                "old_password": "wrong",
                "new_password1": "Zxcv-0987!as",
                "new_password2": "Zxcv-0987!as",
            },
        )
        self.assertIn("old_password", response.context["form"].errors)
        self.client.post(
            url,
            {
                "old_password": PASSWORD,
                "new_password1": "Zxcv-0987!as",
                "new_password2": "Zxcv-0987!as",
            },
        )
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password("Zxcv-0987!as"))
        self.assertIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.get(url).context["recent"][0].action, "password changed")

    def test_password_change_enforces(self):
        url = reverse("admin_account")
        self.assertContains(self.client.get(url), "data-rule=")
        response = self.client.post(
            url, {"old_password": PASSWORD, "new_password1": PASSWORD, "new_password2": PASSWORD}
        )
        self.assertIn("last 3 passwords", response.context["form"].errors["new_password2"][0])
        self.client.post(
            url,
            {
                "old_password": PASSWORD,
                "new_password1": "Zxcv-0987!as",
                "new_password2": "Zxcv-0987!as",
            },
        )
        self.assertEqual(mail.outbox[0].subject, "Your FlatFair password was changed")
        self.assertEqual(self.admin.passwordHistory.count(), 1)

    def test_long_customer_email_keeps(self):
        email = "a" * 60 + "@" + "b" * 60 + ".com"
        self.seller.email, self.seller.username = (email, email)
        self.seller.save()
        self.client.post(
            reverse("admin_customer_suspend", args=[self.seller.pk]),
            {"action": "suspend", "reason": "Scam reports"},
        )
        entry = AuditLog.objects.get(action="account suspended")
        self.assertLessEqual(len(entry.target), 80)
        self.assertEqual(auditRows([entry])[0].recordUrl, reverse("admin_customer", args=[self.seller.pk]))


class PriceModelTests(TestCase):

    def setUp(self):
        self.admin = makeStaff("admin@test.com")
        self.seller = makeUser("asdf@test.com", "seller")
        self.client.force_login(self.admin)
        self.modelInfo = {
            "available": True,
            "version": "v5.3",
            "month": "2026-09",
            "mape": 4.3,
            "coverage": 71.1,
            "target": 80,
        }
        modelPatch = patch("adminpanel.views.modelInfo", side_effect=lambda: self.modelInfo.copy())
        modelPatch.start()
        self.addCleanup(modelPatch.stop)

    def test_accuracy_page_summarizes(self):
        flat = makeListing(self.seller, askingPrice=700000)
        Decision.objects.create(
            listing=flat,
            staff=self.admin,
            decision="approved",
            askingPrice=700000,
            bandLow=500000,
            bandHigh=600000,
        )
        Decision.objects.create(
            listing=flat,
            staff=self.admin,
            decision="rejected",
            askingPrice=700000,
            bandLow=500000,
            bandHigh=600000,
        )
        ListingPhoto.objects.create(listing=flat, room="kitchen", image="a.jpg", aiIrrelevant=0.9)
        response = self.client.get(reverse("admin_price_model"))
        self.assertEqual((response.context["approvedCount"], response.context["rejectedCount"]), (1, 1))
        self.assertEqual(response.context["overrideRate"], 50)
        self.assertEqual(response.context["photosChecked"], 1)
        self.assertEqual(response.context["liveDoubtful"], 1)
        self.assertEqual(response.context["liveFlats"][0].doubtful, 1)
        self.assertTrue(response.context["modelInfo"]["available"])
        self.assertEqual(response.context["modelInfo"]["target"], 80)
        self.assertContains(response, "before we used all sales to update the price check")
        self.assertContains(response, "Of flats held for price, share staff approved")

    def test_available_separates_below(self):
        response = self.client.get(reverse("admin_price_model"))
        self.assertContains(response, 'class="chip brand"><span class="dot"></span>Price check available')
        self.assertContains(response, '<span class="chip amber">Below target</span>', html=True)
        self.assertContains(
            response,
            '<dd class="coverage-review"><b>71.1%</b> of later sales fell inside it (target <b>80%</b>)</dd>',
            html=True,
        )
        self.assertContains(response, "Review price-held listings in the review queue")
        self.assertContains(response, "ask for a price accuracy review")
        self.assertNotContains(response, ">Working<")

    def test_coverage_at_above_target(self):
        for coverage in (80, 85):
            with self.subTest(coverage=coverage):
                self.modelInfo["coverage"] = coverage
                response = self.client.get(reverse("admin_price_model"))
                self.assertContains(response, "Price check available")
                self.assertContains(response, '<span class="chip good">Meets target</span>', html=True)
                self.assertNotContains(response, "Below target")
                self.assertNotContains(response, 'class="coverage-action"')

    def test_unavailable_not_imply_available(self):
        self.modelInfo = {"available": False, "version": "v5.3"}
        response = self.client.get(reverse("admin_price_model"))
        self.assertContains(response, "Price check unavailable")
        self.assertContains(response, "Result unavailable")
        self.assertContains(response, "Evaluation unavailable", count=2)
        self.assertNotContains(response, "Meets target")
        self.assertNotContains(response, 'class="coverage-review"')


class EmptyOverviewTests(TestCase):

    def test_new_site_shows_plain(self):
        self.client.force_login(makeStaff("fresh-admin@test.com", name="Lim"))
        response = self.client.get(reverse("admin_overview"))
        for text in (
            "No new listings in the last few weeks.",
            "No listings yet.",
            "No sales in the last few weeks.",
            "No sales yet.",
        ):
            self.assertContains(response, text)
        self.assertNotContains(response, "chart-cols")
        self.assertNotContains(response, "chart-stack")

    def test_charts_return_once_there(self):
        self.client.force_login(makeStaff("busy-admin@test.com", name="Lim"))
        makeListing(makeUser("busy-seller@test.com", "seller"))
        response = self.client.get(reverse("admin_overview"))
        self.assertContains(response, "chart-cols")
        self.assertContains(response, "chart-stack")
        self.assertNotContains(response, "No listings yet.")


class StaffNextStepTests(TestCase):

    def setUp(self):
        self.admin = makeStaff("admin@test.com")
        self.seller = makeUser("seller@test.com", "seller")
        self.client.force_login(self.admin)

    def test_review_page_one_primary(self):
        flat = makeListing(self.seller, status="review")
        response = self.client.get(reverse("admin_queue_item", args=[flat.pk]), {"reason": "other"})
        decision = response.content.decode().split('class="staff-decide"', 1)[1].split("</form>", 1)[0]
        actions = decision.split('<div class="actions">', 1)[1]
        self.assertEqual(actions.count('class="btn btn-brand btn-sm"'), 1)
        self.assertIn('value="documents">Ask for documents</button>', actions)
        self.assertIn('class="btn btn-danger btn-sm"', actions)
        self.assertIn('data-ask="Reject this listing?', actions)
        self.assertIn('data-yes="Reject listing" data-no="Keep reviewing"', actions)
        self.assertIn('name="back" value="?reason=other"', decision)
        self.assertIn('<input name="reason" maxlength="200" required', decision)
        self.assertEqual(decision.count("formnovalidate"), 1)
        self.assertIn('value="approved" formnovalidate>Approve</button>', decision)
        self.assertContains(response, f'href="{reverse('admin_queue')}?reason=other"')
        live = makeListing(makeUser("live-seller@test.com", "seller"), block="125")
        self.assertRedirects(
            self.client.get(reverse("admin_queue_item", args=[live.pk])), reverse("admin_queue")
        )

    def test_out_date_photo_checks(self):
        flat = makeListing(self.seller, status="review")
        for room in ("kitchen", "living_room", "bedroom_1"):
            ListingPhoto.objects.create(
                listing=flat,
                room=room,
                image=room + ".jpg",
                modelVersion="old-model",
                aiIrrelevant=0.01,
                aiGenerated=0.01,
            )
        response = self.client.get(reverse("admin_queue_item", args=[flat.pk]))
        self.assertEqual(len(response.context["r"]["stalePhotos"]), 3)
        self.assertNotContains(response, "must be rerun before publication")
        self.assertContains(response, "The photo check is out of date for 3 photos:", count=1)
        self.assertContains(response, "then approve, ask the seller for new photos, or reject.", count=1)

    def test_queue_rows_one_view(self):
        priced = makeListing(self.seller, status="review", askingPrice=700000)
        photo = makeListing(
            makeUser("photo-seller@test.com", "seller"),
            block="124",
            status="review",
            askingPrice=700000,
        )
        ListingPhoto.objects.create(
            listing=photo,
            room="kitchen",
            image="room.jpg",
            modelVersion=CLEAN["modelVersion"],
            aiIrrelevant=0.01,
            aiGenerated=0.95,
        )
        html = self.client.get(reverse("admin_queue"), {"reason": "price", "per": "100"}).content.decode()
        row = html.split(f'id="queue-{priced.pk}"', 1)[1].split("</article>", 1)[0]
        self.assertLess(row.index("<h3>"), row.index("<img"))
        self.assertEqual(row.count('class="staff-state '), 0)
        self.assertNotIn("Price hold", row)
        everyReason = self.client.get(reverse("admin_queue"), {"per": "100"}).content.decode()
        mixedRow = everyReason.split(f'id="queue-{priced.pk}"', 1)[1].split("</article>", 1)[0]
        self.assertEqual(mixedRow.count('class="staff-state '), 1)
        self.assertIn("Price hold", mixedRow)
        self.assertIn("staff-state--price", mixedRow)
        photoRow = everyReason.split(f'id="queue-{photo.pk}"', 1)[1].split("</article>", 1)[0]
        self.assertIn("staff-state--photo", photoRow)
        self.assertIn("Photo hold", photoRow)
        self.assertNotIn("<details", row)
        self.assertIn(f'action="{reverse('admin_decide', args=[priced.pk])}"', row)
        self.assertIn('name="decision" value="approved">Approve <span class="visually-hidden">', row)
        self.assertIn('name="back" value="?reason=price&amp;per=100&amp;page=1"', row)
        self.assertIn(
            f'href="{reverse('admin_queue_item', args=[priced.pk])}?reason=price&amp;per=100&amp;page=1">Review listing <span',
            row,
        )
        self.assertIn('href="?reason=photos&amp;per=100"', html)
        html = self.client.get(reverse("admin_queue"), {"reason": "photos"}).content.decode()
        row = html.split(f'id="queue-{photo.pk}"', 1)[1].split("</article>", 1)[0]
        self.assertNotIn("<form", row)
        self.assertNotIn("Photo hold", row)
        self.assertEqual(row.count('class="btn '), 1)
        self.assertIn("Review listing", row)
        response = self.client.post(
            reverse("admin_decide", args=[priced.pk]),
            {
                "revision": priced.revision,
                "status": "review",
                "decision": "approved",
                "back": "?reason=price&page=1",
            },
        )
        self.assertRedirects(response, reverse("admin_queue") + "?reason=price&page=1")
        priced.refresh_from_db()
        self.assertEqual(priced.status, "live")

    def test_listing_rows_view_page(self):
        flat = makeListing(self.seller)
        html = self.client.get(reverse("admin_listings")).content.decode()
        row = html.split(f'id="listing-{flat.pk}"', 1)[1].split("</article>", 1)[0]
        self.assertEqual(row.count("<a "), 1)
        self.assertIn(f'href="{reverse('admin_listing', args=[flat.pk])}">View listing <span', row)
        self.assertIn("staff-state--good", row)
        self.assertNotIn("<details", row)
        self.assertNotIn("<form", row)
        page = self.client.get(reverse("admin_listing", args=[flat.pk]))
        self.assertContains(page, f'href="{reverse('admin_listing_edit', args=[flat.pk])}"')
        self.assertContains(page, 'data-yes="Take down listing" data-no="Keep it live"')
        self.assertContains(page, 'name="action" value="review"')
        url = reverse("admin_listing_action", args=[flat.pk])
        response = self.client.post(url, {"action": "takedown", "reason": "Duplicate", "next": "page"})
        self.assertRedirects(response, reverse("admin_listing", args=[flat.pk]))
        page = self.client.get(reverse("admin_listing", args=[flat.pk]))
        self.assertContains(page, "staff-state--closed")
        self.assertContains(page, 'name="action" value="relist"')
        self.assertRedirects(
            self.client.get(reverse("admin_listing", args=[flat.pk + 99])),
            reverse("admin_listings"),
        )

    def test_order_rows_show_one(self):
        product = makeProduct("Floor plan", "addon", 40, None)
        tones = {"paid": "good", "processing": "info", "completed": "good", "refunded": "closed"}
        for status in ("paid", "processing", "completed", "refunded"):
            with self.subTest(status=status):
                order = makeOrder(self.seller, product, status=status)
                response = self.client.get(reverse("admin_orders"), {"status": ""})
                row = response.content.decode().split(f'id="order-{order.pk}"', 1)[1].split("</tr>", 1)[0]
                self.assertEqual(row.count('class="btn '), 1)
                self.assertIn(f'href="{reverse('admin_order', args=[order.pk])}">View order <span', row)
                self.assertEqual(row.count('class="staff-state '), 1)
                self.assertIn(f"staff-state--{tones[status]}", row)
                self.assertNotIn("<form", row)
                self.assertNotIn("<img", row)
                detail = self.client.get(reverse("admin_order", args=[order.pk]))
                self.assertContains(detail, f"staff-state--{tones[status]}")
                if status == "paid":
                    for check_text_1 in (
                        'data-ask="Refund $40.00',
                        'data-yes="Refund order" data-no="Keep the order"',
                        'value="refunded"',
                    ):
                        self.assertContains(detail, check_text_1)

    def test_ticket_reply_first_three(self):
        ticket = Ticket.objects.create(
            customer=self.seller, email=self.seller.email, topic="other", message="Please help"
        )
        url = reverse("admin_ticket", args=[ticket.pk])
        for key, label, body in REPLY_CHOICES:
            with self.subTest(reply=key):
                response = self.client.get(url, {"reply": key})
                self.assertEqual(response.context["form"]["body"].value(), body)
                self.assertContains(response, label)
                html = response.content.decode()
                self.assertLess(html.index('id="ticket-reply-title"'), html.index('id="ticket-thread-title"'))
                self.assertEqual(html.count('href="?reply='), 3)
        response = self.client.get(url, {"reply": "unknown"})
        self.assertEqual(response.context["form"]["body"].value(), "")
        self.assertFalse(TicketMessage.objects.exists())
        response = self.client.post(url + "?reply=checking", {"body": ""})
        self.assertTrue(response.context["form"]["body"].errors)
        self.assertEqual(response.context["form"]["body"].value(), "")
        self.assertFalse(TicketMessage.objects.exists())

    def test_ticket_rows_open_actions(self):
        tones = {"open": "warn", "resolved": "good"}
        for status in ("open", "resolved"):
            with self.subTest(status=status):
                ticket = Ticket.objects.create(
                    email=self.seller.email, topic="other", message="Please help", status=status
                )
                response = self.client.get(reverse("admin_tickets"))
                row = (
                    response.content.decode()
                    .split(f'id="ticket-{ticket.pk}"', 1)[1]
                    .split("</article>", 1)[0]
                )
                self.assertEqual(row.count("<a "), 1)
                self.assertIn(f'href="{reverse('admin_ticket', args=[ticket.pk])}">Open ticket <span', row)
                self.assertNotIn("staff-state", row)
                self.assertNotIn("<form", row)
                self.assertNotIn("<details", row)
                detail = self.client.get(reverse("admin_ticket", args=[ticket.pk]))
                self.assertContains(response, f'<h2 id="tickets-{status}">{ticket.get_status_display()}')
                self.assertContains(detail, f"{ticket.get_status_display()}</span></h1>")
                self.assertContains(detail, f"staff-state--{tones[status]}")
                if status != "resolved":
                    self.assertContains(detail, 'value="resolve"')

    def test_hidden_text_separated_visible(self):
        makeListing(self.seller, status="review", askingPrice=700000)
        makeOrder(self.seller, makeProduct("Floor plan", "addon", 40, None))
        for name in (
            "admin_overview",
            "admin_queue",
            "admin_listings",
            "admin_orders",
            "admin_customers",
            "admin_catalogue",
            "admin_staff",
            "admin_audit",
            "admin_tickets",
            "admin_reviews",
        ):
            with self.subTest(page=name):
                html = (
                    self.client.get(reverse(name), {"status": ""})
                    .content.decode()
                    .split('class="admin-layout"', 1)[1]
                    .split("</main>", 1)[0]
                )
                self.assertNotRegex(html, '[A-Za-z0-9]<span class="visually-hidden">')
                self.assertNotIn('<span class="visually-hidden"> ', html)
                self.assertNotIn("calm-row-details", html)

    def test_empty_lists_offer_link(self):
        for name, message, target in (
            ("admin_queue", "Nothing to check.", "listings"),
            ("admin_orders", "No orders match.", "admin_orders"),
            ("admin_listings", "No flats match.", "admin_listings"),
            ("admin_customers", "No customers match.", "admin_customers"),
            ("admin_catalogue", "No products yet.", "admin_product_new"),
            ("admin_reviews", "No reviews yet.", "admin_reviews"),
            ("admin_audit", "Nothing logged yet.", "admin_overview"),
            ("admin_payments", "No payments need attention.", "admin_orders"),
            ("admin_community", "No reports waiting.", "community"),
        ):
            with self.subTest(page=name):
                params = {"q": "nobody"} if name == "admin_customers" else {}
                response = self.client.get(reverse(name), params)
                self.assertContains(response, message)
                self.assertContains(response, f'href="{reverse(target)}"')
                if name not in ("admin_audit", "admin_community"):
                    self.assertContains(response, '<span class="empty-icon" aria-hidden="true">', count=1)
                    self.assertNotContains(response, "empty-home.svg")
        response = self.client.get(reverse("admin_tickets"))
        self.assertContains(response, "See next steps", count=2)


class StaffListPaginationTests(TestCase):

    def setUp(self):
        self.client.force_login(makeStaff("paginationstaff@test.com"))
        self.seller = makeUser("paginationseller@test.com", "seller", name="Search Seller")
        self.product = makeProduct("Floor plan", "addon", 40, None)

    def exportIds(self, name, params):
        response = self.client.get(reverse(name), params)
        return [int(row[0]) for row in list(csv.reader(io.StringIO(response.content.decode())))[1:]]

    def test_orders_show_count_twenty(
        self,
    ):
        orders = [makeOrder(self.seller, self.product) for number in range(26)]
        params = {"q": "Search Seller", "status": "paid", "type": "service"}
        response = self.client.get(reverse("admin_orders"), params)
        self.assertContains(response, "26 orders, newest first")
        self.assertEqual(len(response.context["orders"]), 25)
        self.assertContains(response, "Page 1 of 2")
        self.assertContains(response, "q=Search+Seller&amp;status=paid&amp;type=service&amp;page=2")
        response = self.client.get(reverse("admin_orders"), {**params, "page": 2})
        self.assertEqual([order.pk for order in response.context["orders"]], [orders[0].pk])
        self.assertEqual(len(self.exportIds("admin_export_orders", {**params, "page": 2})), 26)
        self.assertNotIn("page=2", response.context["query"])

    def test_order_search_matches_number(self):
        order = makeOrder(self.seller, self.product)
        self.seller.last_name = "DistinctSurname"
        self.seller.save(update_fields=["last_name"])
        makeOrder(makeUser("unrelated@test.com", "seller"), self.product)
        for q in (
            str(order.pk),
            f"#{order.pk}",
            "paginationseller@",
            "Search Seller",
            "DistinctSurname",
        ):
            with self.subTest(q=q):
                params = {"q": q, "status": "paid"}
                response = self.client.get(reverse("admin_orders"), params)
                self.assertEqual([found.pk for found in response.context["orders"]], [order.pk])
                self.assertEqual(self.exportIds("admin_export_orders", params), [order.pk])
        response = self.client.get(reverse("admin_orders"), {"q": order.pk, "status": "completed"})
        self.assertContains(response, "0 orders, newest first")
        self.assertFalse(response.context["orders"])

    def test_customers_show_count_pages(self):
        for number in range(25):
            makeUser(f"paged{number}@test.com", "seller", name="Paged Customer")
        makeUser("pagedbuyer@test.com", "buyer", name="Paged Customer")
        params = {"q": "test.com", "role": "seller", "state": "active"}
        response = self.client.get(reverse("admin_customers"), params)
        self.assertContains(response, "26 customers, by name")
        self.assertEqual(len(response.context["people"]), 25)
        self.assertContains(response, "q=test.com&amp;role=seller&amp;state=active&amp;page=2")
        response = self.client.get(reverse("admin_customers"), {**params, "page": 2})
        self.assertEqual(len(response.context["people"]), 1)
        self.assertEqual(len(self.exportIds("admin_export_customers", {**params, "page": 2})), 26)
        self.assertNotIn("page=2", response.context["query"])
