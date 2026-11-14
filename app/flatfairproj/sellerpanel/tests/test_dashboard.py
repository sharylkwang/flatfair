from datetime import datetime, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from sellerpanel.plans import packsNow, plansNow, goodPacks, packState, sell_steps
from sellerpanel.stripepay import savings
from storefront.models import ActivityNotice, Offer, ViewingBooking, ViewingSlot
from storefront.presentation import helloFor
from storefront.tests.helpers import makeListing, makeOrder, makeProduct, makeUser
from storefront.tests.helpers import RoleMarkup


class DashboardTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller", name="Tan Mei Ling")
        self.buyer = makeUser("qwer@test.com")
        self.client.force_login(self.seller)

    def test_secondary_sections_start_closed(
        self,
    ):
        flat = makeListing(self.seller)
        package = makeOrder(self.seller, makeProduct("Boost 30 days"), listing=flat)
        service = makeOrder(self.seller, makeProduct("Floor plan", "addon", 40, None), listing=flat)
        ActivityNotice.objects.create(
            user=self.seller,
            kind="offer_new",
            text="A new offer for your flat",
            url=reverse("seller_viewings"),
        )
        response = self.client.get(reverse("dashboard"))
        content = response.content.decode()
        before, more = content.split('<details class="fold role-more" id="seller-more">', 1)
        self.assertIn("More numbers", more)
        for fact in (
            "selling-progress",
            "See optional packages",
            "Your selling steps",
            "Boost 30 days",
            reverse("receipt", args=[package.pk]),
            "All orders",
            "Floor plan",
            f"Order #{service.pk}",
            "an agent’s 2% fee plus GST",
            "spent here",
            "A new offer for your flat",
            reverse("notifications"),
            f"Your selling steps · {response.context['stepsDone']} of {len(response.context['steps'])} done",
            "My packages · 1 active",
            "My services · 1 purchased",
            "Savings so far · $0 vs an agent",
        ):
            with self.subTest(fact=fact):
                self.assertIn(fact, more)
        for marker in (
            'id="selling-steps"',
            'id="dashboard-packages"',
            'id="dashboard-services"',
            'id="dashboard-savings"',
        ):
            self.assertNotIn(marker, before)
        for step in response.context["steps"]:
            self.assertIn(step["text"], content)
        self.assertNotIn("an agent’s 2% fee plus GST", before)
        self.assertContains(response, reverse("my_listings"))

    def test_limits_default_kpis_three(self):
        makeListing(self.seller)
        response = self.client.get(reverse("dashboard"))
        markup = RoleMarkup(response.content.decode())
        self.assertEqual(markup.defaultKpis, 3)
        self.assertLessEqual(markup.defaultKpis, 3)
        self.assertEqual(len(markup.moreSections), 1)
        self.assertNotIn("open", markup.moreSections[0])

    def test_empty_no_main_flat(self):
        response = self.client.get(reverse("dashboard"))
        self.assertIsNone(response.context["mainFlat"])
        self.assertEqual(response.context["attentionRows"], [])
        self.assertEqual(response.context["stepsDone"], 0)
        self.assertTrue(response.context["dashboardStatus"].startswith("Listing is free."))

    def test_prioritizes_pending_offer(self):
        flat = makeListing(self.seller)
        Offer.objects.create(listing=flat, buyer=self.buyer, amount=500000)
        slot = ViewingSlot.objects.create(listing=flat, startsAt=timezone.now() + timedelta(days=1))
        ViewingBooking.objects.create(slot=slot, buyer=self.buyer)
        response = self.client.get(reverse("dashboard"))
        titles = [row["title"] for row in response.context["attentionRows"]]
        self.assertEqual(titles, ["1 offer to answer", "1 viewing to confirm"])
        self.assertEqual(response.context["pendingOffers"], 1)
        self.assertEqual(list(response.context["weekAgenda"]), [slot])
        self.assertEqual(response.context["mainFlat"], flat)

    def test_single_pending_offer_card(self):
        flat = makeListing(self.seller)
        offer = Offer.objects.create(listing=flat, buyer=self.buyer, amount=500000)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "$500,000 offer from")
        self.assertContains(response, f'href="{reverse('seller_viewings')}#offer-{offer.pk}"')
        Offer.objects.create(listing=flat, buyer=makeUser("zxcv@test.com"), amount=510000)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "2 offers to answer")
        self.assertNotContains(response, "offer from")

    def test_sold_listing_savings_warns(self):
        flat = makeListing(self.seller, askingPrice=1000000)
        makeListing(
            self.seller,
            block="9",
            askingPrice=500000,
            status="sold",
            createdAt=timezone.now() - timedelta(days=30),
        )
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["saved"], savings(500000)["agentFee"])
        self.assertEqual(response.context["rows"][0]["verdict"]["key"], "above")
        order = makeOrder(self.seller, makeProduct("Boost 30 days", price=49, days=30), listing=flat)
        order.createdAt = timezone.now() - timedelta(days=25)
        order.save(update_fields=["createdAt"])
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["plan"]["name"], "Boost 30 days")
        self.assertIn("Your package ends soon", [row["title"] for row in response.context["attentionRows"]])

    def test_greeting_local_time_sellers(self):
        for hour, period in [(9, "morning"), (14, "afternoon"), (20, "evening")]:
            moment = timezone.make_aware(datetime(2026, 1, 5, hour))
            with patch("storefront.presentation.timezone.localtime", return_value=moment):
                self.assertEqual(helloFor(self.seller), f"Good {period}, Tan Mei Ling")

    def test_selling_steps_reflect_listing(self):
        self.assertEqual([step["state"] for step in sell_steps(None)], ["current", "next", "next", "next"])
        flat = makeListing(self.seller)
        Offer.objects.create(
            respondedAt=timezone.now(),
            listing=flat,
            buyer=self.buyer,
            amount=500000,
            status="accepted",
        )
        states = [step["state"] for step in sell_steps(flat)]
        self.assertEqual(states, ["done", "current", "done", "next"])

    def test_confirmed_viewing_ticks_host(self):
        flat = makeListing(self.seller)
        slot = ViewingSlot.objects.create(listing=flat, startsAt=timezone.now() + timedelta(days=1))
        booking = ViewingBooking.objects.create(slot=slot, buyer=self.buyer, status="requested")
        self.assertEqual([step["state"] for step in sell_steps(flat)], ["done", "current", "next", "next"])
        booking.status = "confirmed"
        booking.save(update_fields=["status"])
        self.assertEqual([step["state"] for step in sell_steps(flat)], ["done", "done", "current", "next"])
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["stepsDone"], 2)
        self.assertContains(response, "2 of 4 done")

    def test_select_flat_falls_back(self):
        first = makeListing(self.seller, town="BEDOK")
        second = makeListing(self.seller, town="YISHUN", status="sold")
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["mainFlat"], first)
        self.assertEqual(response.context["dashboardStatus"], "Your Bedok flat is live.")
        self.assertEqual(len(response.context["stepChoices"]), 2)
        response = self.client.get(reverse("dashboard"), {"flat": second.publicId})
        self.assertEqual(response.context["mainFlat"], second)
        response = self.client.get(reverse("dashboard"), {"flat": "asdf"})
        self.assertEqual(response.context["mainFlat"], first)

    def test_agreed_sale_price_drives(self):
        makeListing(self.seller)
        sold = makeListing(self.seller, block="9", askingPrice=500000, status="sold")
        Offer.objects.create(
            respondedAt=timezone.now(),
            listing=sold,
            buyer=self.buyer,
            amount=480000,
            status="accepted",
        )
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["saved"], savings(480000)["agentFee"])
        self.assertFalse(response.context["canListAnother"])
        self.assertNotContains(response, "List another flat")

    def test_shows_confirmed_waiting_viewing(self):
        flat = makeListing(self.seller)
        slot = ViewingSlot.objects.create(listing=flat, startsAt=timezone.now() + timedelta(days=1))
        ViewingBooking.objects.create(slot=slot, buyer=self.buyer, status="confirmed")
        ViewingBooking.objects.create(slot=slot, buyer=makeUser("zxcv@test.com"))
        response = self.client.get(reverse("dashboard"))
        self.assertContains(
            response, "1 of 2 places confirmed · 1 request waiting for your reply · No places left"
        )
        weekday = timezone.localtime(slot.startsAt).strftime("%A")
        self.assertContains(response, f"<h3>{weekday} ")
        self.assertContains(response, f'href="{reverse('seller_viewings')}#slot-{slot.pk}"')


class ViewingsPageTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.buyer = makeUser("qwer@test.com", name="Priya")
        self.flat = makeListing(self.seller)
        self.client.force_login(self.seller)

    def test_offer_summary_counts_competing(self):
        offer = Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=500000)
        Offer.objects.create(listing=self.flat, buyer=makeUser("zxcv@test.com"), amount=510000)
        Offer.objects.create(
            respondedAt=timezone.now(),
            listing=self.flat,
            buyer=makeUser("uiop@test.com"),
            amount=490000,
            status="declined",
        )
        response = self.client.get(reverse("seller_viewings"))
        replies = response.context["replyOffers"]
        self.assertEqual([o.pk for o in replies][0], offer.pk)
        self.assertEqual(
            replies[0].acceptAsk,
            "Accept $500,000 from Priya? This will decline the other 1 offer on this flat.",
        )
        self.assertEqual(len(response.context["offerFlats"][0].closedHistory), 1)

    def test_open_offer_missing_buyer(self):
        offer = Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=500000)
        self.assertEqual(offer.message, "")
        response = self.client.get(reverse("seller_viewings"))
        self.assertContains(response, 'name="action" value="reply"', count=1)
        offer.status = "declined"
        offer.respondedAt = timezone.now()
        offer.save()
        response = self.client.get(reverse("seller_viewings"))
        self.assertNotContains(response, 'name="action" value="reply"')

    def test_waiting_request_yes_no(self):
        slot = ViewingSlot.objects.create(listing=self.flat, startsAt=timezone.now() + timedelta(days=2))
        ViewingBooking.objects.create(slot=slot, buyer=self.buyer)
        html = self.client.get(reverse("seller_viewings")).content.decode()
        row = html.split(f'<details class="s-item" id="slot-{slot.pk}" open>', 1)[1].split("</details>", 1)[0]
        summary, panel = row.split("</summary>", 1)
        self.assertIn("Needs your reply", summary)
        self.assertIn("View bookings <span", summary)
        for action, label in (("confirm", "Agree"), ("seller_cancel", "Disagree")):
            self.assertEqual(panel.count(f'name="action" value="{action}">{label} <span'), 1)
        for word in ("Say yes", "Say no", "your OK", "<summary>Details"):
            self.assertNotIn(word, html)

    def test_separate_waiting_requests(self):
        waiting = ViewingSlot.objects.create(listing=self.flat, startsAt=timezone.now() + timedelta(days=3))
        planned = ViewingSlot.objects.create(listing=self.flat, startsAt=timezone.now() + timedelta(days=1))
        ViewingBooking.objects.create(slot=waiting, buyer=self.buyer)
        ViewingBooking.objects.create(slot=planned, buyer=self.buyer, status="confirmed")
        response = self.client.get(reverse("seller_viewings"))
        self.assertEqual(list(response.context["waitingSlots"]), [waiting])
        self.assertEqual(list(response.context["plannedSlots"]), [planned])
        self.assertEqual(response.context["upcomingCount"], 1)
        self.assertEqual(list(response.context["slotPage"])[0], waiting)

    def test_offer_acceptance_disabled_buyer(self):
        other = makeListing(makeUser("uiop@test.com", "seller"), block="9")
        Offer.objects.create(
            respondedAt=timezone.now(),
            listing=other,
            buyer=self.buyer,
            amount=500000,
            status="accepted",
        )
        Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=510000)
        response = self.client.get(reverse("seller_viewings"))
        self.assertTrue(response.context["replyOffers"][0].buyerAgreedElsewhere)
        self.assertContains(
            response,
            "This buyer has already agreed a price on another flat, so you can’t accept yet.",
        )
        self.assertNotContains(response, 'value="accept"')

    def test_sold_listing_shows_agreed(self):
        Offer.objects.create(
            respondedAt=timezone.now(),
            listing=self.flat,
            buyer=self.buyer,
            amount=500000,
            status="accepted",
        )
        self.flat.status = "sold"
        self.flat.statusBeforeSold = "live"
        self.flat.save()
        response = self.client.get(reverse("seller_viewings"))
        self.assertContains(response, "Sold to this buyer")
        self.assertNotContains(response, "Under offer")
        self.assertNotContains(response, "This buyer has already agreed")


class PlanTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("asdf@test.com", "seller")
        cls.flat = makeListing(cls.seller)

    def test_package_status_handles_expiry(self):
        order = makeOrder(self.seller, makeProduct("Boost 30 days", days=30), listing=self.flat)
        item = order.items.get()
        self.assertEqual(packState(item), "Active · 29 days left")
        later = timezone.now() + timedelta(days=31)
        self.assertEqual(packState(item, later), "Ended")
        self.assertEqual(packsNow([self.flat], later), [])
        order.status = "refunded"
        order.save()
        self.assertEqual(packState(item), "Refunded")
        self.assertEqual(packsNow([self.flat]), [])

    def test_latest_permanent_package_wins(self):
        makeOrder(self.seller, makeProduct("Boost 30 days", days=30), listing=self.flat)
        newer = makeOrder(self.seller, makeProduct("Top spot", days=None), listing=self.flat)
        plan = plansNow([self.flat])[self.flat.pk]
        self.assertEqual((plan["name"], plan["order"], plan["expiry"]), ("Top spot", newer, None))
        self.flat.status = "sold"
        self.flat.save()
        self.assertEqual(plansNow([self.flat]), {})

    def test_eligible_packages_exclude(self):
        now = timezone.now()
        short = makeOrder(self.seller, makeProduct("Boost 30 days", days=30), listing=self.flat)
        long = makeOrder(self.seller, makeProduct("Boost 90 days", days=90), listing=self.flat)
        forever = makeOrder(self.seller, makeProduct("Top spot", days=None), listing=self.flat)
        for order in (short, long, forever):
            order.createdAt = now - timedelta(days=60)
            order.save()
        names = [line.name for line in goodPacks(now).order_by("pk")]
        self.assertEqual(names, ["Boost 90 days", "Top spot"])
        self.assertEqual(goodPacks(now + timedelta(days=31)).count(), 1)
