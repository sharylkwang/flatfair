from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.cache import caches
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .helpers import makeListing, makeOrder, makeProduct, makeUser
from storefront.models import ActivityNotice, OrderItem, ViewingBooking, ViewingSlot
from storefront.reminders import (
    REMINDER_KEY,
    autoNudge,
    nudgeOrders,
    nudgeAll,
    sendViewingReminders,
    visitWords,
)

SINGAPORE = ZoneInfo("Asia/Singapore")


class ViewingReminderTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("asdf@test.com", "seller")
        cls.buyer = makeUser("qwer@test.com")
        cls.flat = makeListing(cls.seller, town="SENGKANG", block="326A")

    def viewing(self, hours, status="confirmed", buyer=None, **slotFields):
        slot = ViewingSlot.objects.create(
            listing=self.flat, startsAt=timezone.now() + timedelta(hours=hours), **slotFields
        )
        ViewingBooking.objects.create(slot=slot, buyer=buyer or self.buyer, status=status)
        return slot

    def reminders(self, user=None):
        notices = ActivityNotice.objects.filter(kind="viewing_reminder")
        return notices.filter(user=user) if user else notices

    def test_goes_once_buyer_seller(self):
        slot = self.viewing(20)
        self.assertEqual(sendViewingReminders(), 2)
        self.assertEqual(sendViewingReminders(), 0)
        buyerNotice = self.reminders(self.buyer).get()
        sellerNotice = self.reminders(self.seller).get()
        self.assertEqual(buyerNotice.url, reverse("my_offers") + f"#slot-{slot.pk}")
        self.assertEqual(sellerNotice.url, reverse("seller_viewings") + f"#slot-{slot.pk}")
        self.assertEqual(buyerNotice.text, visitWords(slot, timezone.now()))
        self.assertEqual(buyerNotice.text, sellerNotice.text)
        self.assertEqual(buyerNotice.get_kind_display(), "Viewing reminder")
        self.assertFalse(buyerNotice.read)

    def test_words_say_today_tomorrow(self):
        slot = ViewingSlot(listing=self.flat, startsAt=datetime(2026, 11, 14, 15, 0, tzinfo=SINGAPORE))
        dayBefore = datetime(2026, 11, 13, 16, 0, tzinfo=SINGAPORE)
        sameDay = datetime(2026, 11, 14, 9, 0, tzinfo=SINGAPORE)
        self.assertEqual(visitWords(slot, dayBefore), "Viewing tomorrow, 3:00 pm · Blk 326A Sengkang")
        self.assertEqual(visitWords(slot, sameDay), "Viewing today, 3:00 pm · Blk 326A Sengkang")

    def test_seller_gets_one_slot(self):
        slot = self.viewing(20)
        self.assertEqual(sendViewingReminders(), 2)
        late = makeUser("zxcv@test.com")
        ViewingBooking.objects.create(slot=slot, buyer=late, status="confirmed")
        self.assertEqual(sendViewingReminders(), 1)
        self.assertEqual(self.reminders(late).count(), 1)
        self.assertEqual(self.reminders(self.seller).count(), 1)
        self.assertEqual(self.reminders().count(), 3)

    def test_no_far_away_past(self):
        self.viewing(72)
        self.viewing(-1)
        self.viewing(5, status="cancelled")
        self.viewing(6, status="requested")
        self.viewing(7, removed=True)
        self.assertEqual(sendViewingReminders(), 0)
        self.assertFalse(self.reminders().exists())

    def test_no_hidden_listing_until(self):
        self.viewing(20)
        self.flat.status = "hidden"
        self.flat.save(update_fields=["status"])
        self.assertEqual(sendViewingReminders(), 0)
        self.flat.status = "live"
        self.flat.save(update_fields=["status"])
        self.assertEqual(sendViewingReminders(), 2)

    def test_far_reminded_once_enters(self):
        slot = self.viewing(30)
        self.assertEqual(sendViewingReminders(), 0)
        ViewingSlot.objects.filter(pk=slot.pk).update(startsAt=timezone.now() + timedelta(hours=23))
        self.assertEqual(sendViewingReminders(), 2)
        ViewingSlot.objects.filter(pk=slot.pk).update(startsAt=timezone.now() - timedelta(minutes=1))
        self.assertEqual(sendViewingReminders(), 0)
        self.assertEqual(self.reminders().count(), 2)

    def test_my_offers_page_sends(self):
        self.viewing(20)
        caches["resources"].delete(REMINDER_KEY)
        self.client.force_login(self.buyer)
        self.assertEqual(self.client.get(reverse("my_offers")).status_code, 200)
        self.assertEqual(self.reminders(self.buyer).count(), 1)
        self.assertEqual(self.reminders(self.seller).count(), 1)
        caches["resources"].delete(REMINDER_KEY)
        with patch("storefront.reminders.nudgeAll", return_value=0) as send:
            autoNudge()
            autoNudge()
        send.assert_called_once()

    def test_notifications_page_shows_on(self):
        self.viewing(20)
        caches["resources"].delete(REMINDER_KEY)
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("notifications"))
        self.assertContains(response, "Viewing reminder")
        self.assertContains(response, "326A Sengkang")

    def test_seller_page_sends_due(self):
        self.viewing(20)
        caches["resources"].delete(REMINDER_KEY)
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get(reverse("seller_viewings")).status_code, 200)
        self.assertEqual(self.reminders(self.seller).count(), 1)


class OrderReminderTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("uiop@test.com", "seller")
        cls.product = makeProduct("Floor plan", "addon", 40, None)

    def deliveredOrder(self, days, status="processing", note="Plan delivered"):
        order = makeOrder(self.seller, self.product, status=status)
        order.items.update(deliveryNote=note, deliveredAt=timezone.now() - timedelta(days=days))
        return order

    def reminders(self):
        return ActivityNotice.objects.filter(kind="order_completing")

    def test_goes_once_in_last(self):
        order = self.deliveredOrder(6.5)
        self.assertEqual(nudgeOrders(), 1)
        self.assertEqual(nudgeOrders(), 0)
        notice = self.reminders().get()
        completes = timezone.localtime(order.items.get().deliveredAt + timedelta(days=7))
        self.assertEqual(notice.user, self.seller)
        self.assertEqual(notice.url, reverse("my_orders") + f"#order-{order.pk}")
        self.assertEqual(
            notice.text,
            f"Order #{order.pk} completes by itself on {completes.day} {completes:%b}. Check your delivery in Orders before then and tell us if anything is wrong.",
        )
        self.assertEqual(notice.get_kind_display(), "Order completing soon")

    def test_no_last_day_deadline(self):
        self.deliveredOrder(2)
        self.deliveredOrder(5.9)
        self.deliveredOrder(7)
        self.deliveredOrder(9)
        self.assertEqual(nudgeOrders(), 0)
        self.assertFalse(self.reminders().exists())

    def test_no_will_not_auto(self):
        self.deliveredOrder(6.5, status="refunded")
        self.deliveredOrder(6.5, status="completed")
        self.deliveredOrder(6.5, status="paid")
        self.deliveredOrder(6.5, note="")
        makeOrder(self.seller, makeProduct("Boost 30 days"), status="processing")
        partly = self.deliveredOrder(6.5)
        OrderItem.objects.create(
            order=partly, product=self.product, price=40, kind="addon", name="Second service"
        )
        self.assertEqual(nudgeOrders(), 0)
        self.assertFalse(self.reminders().exists())

    def test_counts_last_delivered_service(self):
        order = self.deliveredOrder(9)
        extra = OrderItem.objects.create(
            order=order,
            product=self.product,
            price=40,
            kind="addon",
            name="Second service",
            deliveryNote="Also delivered",
            deliveredAt=timezone.now() - timedelta(days=3),
        )
        self.assertEqual(nudgeOrders(), 0)
        extra.deliveredAt = timezone.now() - timedelta(days=6.2)
        extra.save()
        self.assertEqual(nudgeOrders(), 1)

    def test_seller_confirming_first_means(self):
        order = self.deliveredOrder(6.5)
        self.client.force_login(self.seller)
        self.client.post(reverse("confirm_delivery", args=[order.pk]))
        order.refresh_from_db()
        self.assertEqual(order.status, "completed")
        self.assertEqual(nudgeOrders(), 0)

    def test_command_sends_reports_them(self):
        self.deliveredOrder(6.5)
        output = StringIO()
        call_command("reconcile_payments", stdout=output)
        call_command("reconcile_payments", stdout=output)
        self.assertEqual(self.reminders().count(), 1)
        self.assertEqual(output.getvalue().count("Sent 1 viewing and order reminders."), 1)

    def test_page_sends_due(self):
        self.deliveredOrder(6.5)
        caches["resources"].delete(REMINDER_KEY)
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get(reverse("my_orders")).status_code, 200)
        self.assertEqual(self.reminders().count(), 1)

    def test_one_failing_not_stop(self):
        self.deliveredOrder(6.5)
        with patch("storefront.reminders.sendViewingReminders", side_effect=RuntimeError("qwerty")) as broken:
            broken.__name__ = "sendViewingReminders"
            with self.assertLogs("storefront.reminders", level="ERROR"):
                self.assertEqual(nudgeAll(), 1)
        self.assertEqual(self.reminders().count(), 1)
