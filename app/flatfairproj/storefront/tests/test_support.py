from datetime import datetime, timedelta
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from adminpanel.models import Ticket, TicketMessage
from storefront.templatetags.estate_tags import support_unread, latest_notices, notice_unread
from storefront.models import (
    ActivityNotice,
    Offer,
    ViewingSlot,
    ViewingAsk,
    Collection,
    Product,
    SavedListing,
)
from storefront.notices import notify, offer_note, visit_note
from .helpers import makeListing, makeOrder, makeProduct, makeStaff, makeUser
from storefront.tests.helpers import makeListing, makeStaff, makeUser, makeOrder
from django.core import mail
from accounts.models import EmailCheck


class SupportTests(TestCase):

    def setUp(self):
        cache.clear()
        self.seller = makeUser("asdf@test.com", "seller")

    def test_guest_creates_ticket_cleans(self):
        response = self.client.post(
            reverse("support"),
            {"email": "zxcv@test.com", "topic": "account", "message": "Hi\r\nthere"},
        )
        self.assertRedirects(response, reverse("support") + "#contact-support")
        ticket = Ticket.objects.get()
        self.assertEqual(
            (ticket.email, ticket.customer, ticket.message), ("zxcv@test.com", None, "Hi\nthere")
        )

    def test_requires_topic_creating_ticket(self):
        response = self.client.post(reverse("support"), {"email": "zxcv@test.com", "message": "asdf"})
        self.assertIn("topic", response.context["ticketForm"].errors)
        self.assertFalse(Ticket.objects.exists())

    def test_submission_rate_limit_caps(self):
        for i in range(7):
            self.client.post(
                reverse("support"),
                {"email": "zxcv@test.com", "topic": "other", "message": f"asdf {i}"},
            )
        self.assertEqual(Ticket.objects.count(), 5)

    def test_customer_links_own_order(self):
        order = makeOrder(self.seller, makeProduct("Floor plan", "addon", 40, None))
        other = makeOrder(makeUser("qwer@test.com", "seller"), makeProduct())
        self.client.force_login(self.seller)
        page = self.client.get(reverse("support"), {"topic": "refund", "order": order.pk})
        self.assertEqual(page.context["ticketForm"].linkedOrder(), order)
        response = self.client.post(
            reverse("support"), {"topic": "refund", "message": "asdf", "order": order.pk}
        )
        ticket = Ticket.objects.get()
        self.assertRedirects(response, reverse("my_ticket", args=[ticket.pk]))
        self.assertEqual((ticket.customer, ticket.email, ticket.order), (self.seller, "asdf@test.com", order))
        self.client.post(reverse("support"), {"topic": "refund", "message": "qwer", "order": other.pk})
        self.assertIsNone(Ticket.objects.get(message="qwer").order)

    def test_staff_internal_ticket_pages(self):
        self.client.force_login(makeStaff("admin@test.com"))
        response = self.client.get(reverse("support"))
        self.assertIsNone(response.context["ticketForm"])
        self.assertRedirects(self.client.get(reverse("my_messages")), reverse("admin_tickets"))

    def test_refund_requests_reject_packages(self):
        package = makeOrder(self.seller, makeProduct(), "completed")
        started = makeOrder(self.seller, makeProduct("Floor plan", "addon", 40, None), "processing")
        self.client.force_login(self.seller)
        for order in (package, started):
            response = self.client.post(
                reverse("support"), {"topic": "refund", "message": "asdf", "order": order.pk}
            )
            self.assertIn("can't be refunded", response.context["ticketForm"].errors["topic"][0])
        self.assertFalse(Ticket.objects.exists())
        self.client.post(reverse("support"), {"topic": "orders", "message": "asdf", "order": package.pk})
        self.assertEqual(Ticket.objects.get().order, package)
        page = self.client.get(reverse("my_orders"))
        self.assertNotContains(page, "topic=refund&amp;order={}".format(package.pk))

    def test_empty_submission_keeps_required(self):
        response = self.client.post(reverse("support"), {})
        self.assertEqual(response.status_code, 200)
        form = response.context["ticketForm"]
        self.assertTrue(form.is_bound)
        self.assertEqual(set(form.errors), {"email", "topic", "message"})
        self.assertFalse(Ticket.objects.exists())


class MessageTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.staff = makeStaff("admin@test.com", "Admin")
        self.ticket = Ticket.objects.create(
            customer=self.seller, email="asdf@test.com", topic="other", message="asdf"
        )
        self.client.force_login(self.seller)

    def test_opening_ticket_marks_staff(self):
        TicketMessage.objects.create(ticket=self.ticket, author=self.staff, body="qwer", fromStaff=True)
        self.assertEqual(support_unread(self.seller), 1)
        rows = self.client.get(reverse("my_messages")).context["rows"]
        self.assertEqual((rows[0].lastBody, rows[0].unseen), ("qwer", 1))
        self.client.get(reverse("my_ticket", args=[self.ticket.pk]))
        self.assertEqual(support_unread(self.seller), 0)

    def test_customer_reply_reopens_resolved(self):
        self.ticket.status = "resolved"
        self.ticket.save()
        self.client.post(reverse("my_ticket", args=[self.ticket.pk]), {"body": "still broken"})
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, "open")
        self.assertEqual(self.ticket.replies.get().body, "still broken")
        self.assertFalse(self.ticket.replies.get().fromStaff)

    def test_customer_blocks_read_another(self):
        other = makeUser("qwer@test.com", "seller")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("my_ticket", args=[self.ticket.pk])).status_code, 404)
        self.assertEqual(len(self.client.get(reverse("my_messages")).context["rows"]), 0)

    def test_blank_ticket_reply_not(self):
        self.client.post(reverse("my_ticket", args=[self.ticket.pk]), {"body": ""})
        self.assertFalse(self.ticket.replies.exists())


class NoticeTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = makeUser("asdf@test.com")

    def test_count_unread_truncate_long(self):
        notify(self.user, "offer_reply", "The seller replied", "/my-offers#offer-1")
        notify(self.user.pk, "offer_declined", "x" * 300, "/my-offers#offer-2")
        self.assertEqual(notice_unread(self.user), 2)
        self.assertEqual(len(ActivityNotice.objects.get(kind="offer_declined").text), 200)
        self.assertEqual(len(latest_notices(self.user)), 2)

    def test_notifications_page_marks(self):
        notify(self.user, "offer_reply", "The seller replied", "/my-offers#offer-1")
        self.client.force_login(self.user)
        response = self.client.get(reverse("notifications"))
        self.assertEqual(response.context["items"][0]["title"], "Seller replied")
        self.assertTrue(response.context["items"][0]["new"])
        self.assertEqual(notice_unread(self.user), 0)
        self.assertContains(
            response, '<section class="notice-list" aria-label="Notifications, newest first">'
        )
        self.assertContains(response, 'class="notice-row is-unread" href="/my-offers#offer-1"')
        self.assertNotContains(response, ">Offers and viewings<")
        again = self.client.get(reverse("notifications"))
        self.assertContains(again, 'class="notice-row" href="/my-offers#offer-1"')

    def test_empty_notifications_show_one(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("notifications"))
        self.assertContains(response, "<h2>Nothing new</h2>", html=True)
        self.assertContains(response, 'class="empty-icon"', count=1)
        self.assertNotContains(response, "storefront/art/")
        self.assertContains(
            response,
            f'<a class="btn btn-brand" href="{reverse('listings')}">Browse flats</a>',
            html=True,
        )
        for number in range(12):
            notify(self.user, "offer_reply", f"Reply {number}", "/my-offers")
        response = self.client.get(reverse("notifications"))
        self.assertNotContains(response, "Show older")
        self.assertContains(response, 'class="notice-row', count=10)
        self.assertContains(response, "Notifications: Page 1 of 2")
        self.assertContains(response, '<a href="?page=2">Next</a>', html=True)
        self.assertContains(
            self.client.get(reverse("notifications"), {"page": 2}), 'class="notice-row', count=2
        )

    def test_notifications_ordered_newest(self):
        old = notify(self.user, "offer_reply", "old", "/a")
        ActivityNotice.objects.filter(pk=old.pk).update(createdAt=timezone.now() - timedelta(days=1))
        notify(self.user, "offer_reply", "new", "/b")
        self.client.force_login(self.user)
        items = self.client.get(reverse("notifications")).context["items"]
        self.assertEqual([item["text"] for item in items], ["new", "old"])

    def test_offer_viewing_include_prices(self):
        seller = makeUser("qwer@test.com", "seller")
        flat = makeListing(seller, block="225A", town="JURONG EAST")
        offer = Offer.objects.create(
            listing=flat,
            buyer=self.user,
            amount=520000,
            counterAmount=540000,
            status="countered",
            respondedAt=timezone.now(),
        )
        offer_note(offer, "offer_countered", "buyer")
        notice = ActivityNotice.objects.get(user=self.user)
        self.assertEqual(notice.text, "The seller is asking $540,000 for Blk 225A Jurong East")
        self.assertEqual(notice.url, reverse("my_offers") + f"#offer-{offer.pk}")
        moment = timezone.make_aware(datetime(2026, 3, 7, 19, 30))
        slot = ViewingSlot.objects.create(listing=flat, startsAt=moment)
        visit_note(slot, "viewing_booked", [seller.pk], "buyer")
        self.assertEqual(
            ActivityNotice.objects.get(user=seller).text,
            "A buyer asked to view Blk 225A Jurong East on Sat 7 Mar, 7:30pm",
        )
        self.assertEqual(ActivityNotice.objects.get(user=seller).get_kind_display(), "Viewing requested")

    def test_notification_bell_shows_unread(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("home"))
        self.assertContains(response, 'class="header-bell"')
        self.assertContains(response, 'aria-label="Notifications"')
        notify(self.user, "offer_reply", "The seller replied", "/my-offers#offer-1")
        response = self.client.get(reverse("home"))
        self.assertContains(response, 'aria-label="Notifications, 1 unread"')
        self.client.force_login(makeStaff("admin@test.com"))
        self.assertContains(self.client.get(reverse("home")), 'class="header-bell"')

    def test_account_menu_announces_new(self):
        ticket = Ticket.objects.create(customer=self.user, email="asdf@test.com", topic="other", message="Hi")
        TicketMessage.objects.create(ticket=ticket, body="Hello", fromStaff=True)
        self.client.force_login(self.user)
        response = self.client.get(reverse("home"))
        self.assertContains(response, 'Account menu, 1 new support reply"')
        self.assertContains(response, " new support reply</span>")
        self.assertNotContains(response, "new support replies")

    def test_community_notification_tab(self):
        notify(self.user, "offer_reply", "The seller replied", "/my-offers#offer-1")
        notify(
            self.user,
            "community_reply",
            "Qwer replied to your question",
            "/community/1?post=2#post-2",
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse("notifications") + "?show=community")
        self.assertEqual([item["title"] for item in response.context["items"]], ["New reply"])
        self.assertEqual(notice_unread(self.user), 1)
        self.assertContains(
            response, '<a href="?show=community" aria-current="page">Community</a>', html=True
        )
        self.client.get(reverse("notifications"))
        self.assertEqual(notice_unread(self.user), 0)


class AskViewingTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("ask-seller@test.com", "seller")
        cls.buyer = makeUser("ask-buyer@test.com", "buyer")
        cls.flat = makeListing(cls.seller)

    def test_buyer_times_once(self):
        self.client.force_login(self.buyer)
        page = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        self.assertContains(page, "Ask for a viewing time")
        response = self.client.post(reverse("ask_viewing", args=[self.flat.publicId]), follow=True)
        self.assertContains(response, "We’ve asked the seller for viewing times.")
        self.assertContains(response, "You’ve asked for viewing times.")
        notice = ActivityNotice.objects.get(user=self.seller, kind="viewing_asked")
        self.assertIn("A buyer would like to view", notice.text)
        response = self.client.post(reverse("ask_viewing", args=[self.flat.publicId]), follow=True)
        self.assertContains(response, "You’ve already asked.")
        self.assertEqual(ViewingAsk.objects.count(), 1)

    def test_adding_time_tells_everyone(self):
        ViewingAsk.objects.create(listing=self.flat, buyer=self.buyer)
        self.client.force_login(self.seller)
        when = timezone.localtime(timezone.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(
            reverse("activity_action"),
            {"action": "slot", "id": self.flat.publicId, "startsAt": when, "capacity": "2"},
            follow=True,
        )
        self.assertContains(response, "We’ve told the 1 buyer who asked.")
        self.assertTrue(ActivityNotice.objects.filter(user=self.buyer, kind="viewing_times_added").exists())
        self.assertIsNotNone(ViewingAsk.objects.get().answeredAt)

    def test_seller_guests_not_see(self):
        self.client.force_login(self.seller)
        self.assertNotContains(
            self.client.get(reverse("listing_detail", args=[self.flat.publicId])),
            "Ask for a viewing time",
        )
        self.client.logout()
        self.assertNotContains(
            self.client.get(reverse("listing_detail", args=[self.flat.publicId])),
            "Ask for a viewing time",
        )


class DecisionNoticeTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("told-seller@test.com", "seller")
        cls.buyer = makeUser("told-buyer@test.com", "buyer")
        cls.flat = makeListing(cls.seller, leaseCommenceYear=1985)

    def test_undoing_acceptance_tells_buyer(self):
        offer = Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=540000)
        self.client.force_login(self.seller)
        self.client.post(reverse("activity_action"), {"action": "accept", "id": offer.pk})
        self.client.post(reverse("activity_action"), {"action": "reopen", "id": offer.pk})
        notice = ActivityNotice.objects.get(user=self.buyer, kind="offer_reopened")
        self.assertIn("undid their acceptance", notice.text)

    def test_admin_edit_tells_seller(self):
        admin = makeStaff("told-admin@test.com")
        self.client.force_login(admin)
        data = {
            "revision": self.flat.revision,
            "askingPrice": "560000",
            "floorAreaSqm": "92",
            "storey": "8",
            "flatModel": "Model A",
        }
        self.client.post(reverse("admin_listing_edit", args=[self.flat.pk]), data)
        notice = ActivityNotice.objects.filter(user=self.seller, kind="listing_edited").first()
        self.assertIsNotNone(notice)
        self.assertIn("Our team changed the asking price", notice.text)


class SmallWinTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("win-seller@test.com", "seller")
        cls.buyer = makeUser("win-buyer@test.com", "buyer")
        cls.flat = makeListing(cls.seller)

    def test_confirmed_viewing_downloads_as(self):
        from storefront.models import ViewingBooking, ViewingSlot

        slot = ViewingSlot.objects.create(
            listing=self.flat, startsAt=timezone.now() + timedelta(days=2), capacity=2
        )
        booking = ViewingBooking.objects.create(slot=slot, buyer=self.buyer, status="confirmed")
        self.client.force_login(self.buyer)
        self.assertContains(
            self.client.get(reverse("listing_detail", args=[self.flat.publicId])), "Add to calendar"
        )
        response = self.client.get(reverse("booking_calendar", args=[booking.pk]))
        self.assertEqual(response["Content-Type"], "text/calendar; charset=utf-8")
        body = response.content.decode()
        self.assertIn("BEGIN:VEVENT", body)
        self.assertIn("SUMMARY:Flat viewing at Blk 123 Yishun", body)
        self.assertIn("\r\n", body)
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get(reverse("slot_calendar", args=[slot.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("booking_calendar", args=[booking.pk])).status_code, 404)

    def test_flat_page_share_links(self):
        page = self.client.get(reverse("listing_detail", args=[self.flat.publicId])).content.decode()
        self.assertIn("Share this flat:", page)
        self.assertIn("https://wa.me/?text=Have+a+look+at+this+flat+on+FlatFair", page)
        self.assertIn("mailto:?subject=A%20flat%20on%20FlatFair", page)

    def test_sellers_admin_land_where(self):
        from accounts.views import homeFor

        self.assertEqual(homeFor(self.seller), reverse("dashboard"))
        self.assertEqual(homeFor(self.buyer), reverse("home"))
        self.assertEqual(homeFor(makeStaff("win-admin@test.com")), reverse("admin_overview"))


@override_settings(
    NOTICE_MAIL_SKIP=("flatfair.sg",),
    NOTICE_MAIL_ASYNC=False,
    PUBLIC_URL="https://flatfair.example.com",
)
class MailerTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("mail-seller@test.com", "seller", name="Wei Ming")
        cls.buyer = makeUser("mail-buyer@test.com", "buyer", name="Priya")
        cls.flat = makeListing(cls.seller)

    def act(self, user, kind, pk, **data):
        self.client.force_login(user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse("activity_action"), {"action": kind, "id": pk, **data})

    def test_new_offer_emails_seller(self):
        self.act(self.buyer, "offer", self.flat.publicId, amount="540000")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(
            (message.to, message.subject),
            (["mail-seller@test.com"], "FlatFair: New offer on your flat"),
        )
        self.assertIn("New offer of $540,000 for Blk 123 Yishun", message.body)
        self.assertIn("https://flatfair.example.com/seller/viewings#offer-", message.body)

    def test_accepting_emails_buyer_tells(self):
        offer = Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=540000)
        fan = makeUser("mail-fan@test.com", "buyer", name="Jun Hao")
        SavedListing.objects.create(
            collection=Collection.objects.create(user=fan, name="List"), listing=self.flat
        )
        self.act(self.seller, "accept", offer.pk)
        subjects = {message.to[0]: message.subject for message in mail.outbox}
        self.assertEqual(subjects["mail-buyer@test.com"], "FlatFair: Your price is agreed")
        self.assertEqual(subjects["mail-fan@test.com"], "FlatFair: A flat you saved is under offer")
        self.assertTrue(ActivityNotice.objects.filter(user=fan, kind="saved_under_offer").exists())

    def test_price_drop_sale_reach(self):
        fan = makeUser("mail-fan@test.com", "buyer")
        SavedListing.objects.create(
            collection=Collection.objects.create(user=fan, name="List"), listing=self.flat
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.flat.askingPrice = 530000
            self.flat.save(update_fields=["askingPrice"])
        with self.captureOnCommitCallbacks(execute=True):
            self.flat.status = "sold"
            self.flat.save(update_fields=["status"])
        kinds = set(ActivityNotice.objects.filter(user=fan).values_list("kind", flat=True))
        self.assertTrue({"saved_price_drop", "saved_sold"} <= kinds)
        self.assertIn("is now $530,000", ActivityNotice.objects.get(user=fan, kind="saved_price_drop").text)

    def test_unconfirmed_demo_addresses_get(self):
        EmailCheck.objects.filter(user=self.seller).update(verifiedAt=None)
        self.act(self.buyer, "offer", self.flat.publicId, amount="540000")
        self.assertEqual(len(mail.outbox), 0)
        demo = makeUser("seller@flatfair.sg", "seller")
        other = makeListing(demo, postalCode="760125", block="125")
        self.act(self.buyer, "offer", other.publicId, amount="540000")
        self.assertEqual(len(mail.outbox), 0)

    def test_paying_sends_a_receipt(self):
        product = Product.objects.create(name="Floor plan", kind="addon", price=40, isActive=True)
        with self.captureOnCommitCallbacks(execute=True):
            order = makeOrder(self.seller, product)
        self.assertEqual(mail.outbox[-1].subject, f"FlatFair: Receipt for order #{order.pk}")
        self.assertIn("Total: $40.00", mail.outbox[-1].body)
