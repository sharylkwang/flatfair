from datetime import timedelta
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from storefront.models import ActivityNotice, ViewingBooking, ViewingSlot, Listing
from .helpers import makeListing, makeUser
from storefront.activity import CANCEL_DAYS, CANCEL_LIMIT
from storefront.tests.helpers import makeListing, makeUser


class ViewingTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.buyer = makeUser("qwer@test.com")
        self.other = makeUser("zxcv@test.com")
        self.flat = makeListing(self.seller)
        self.slot = ViewingSlot.objects.create(
            listing=self.flat, startsAt=timezone.now() + timedelta(days=2), capacity=1
        )

    def act(self, user, kind, id, **data):
        if kind in ("cancel", "seller_cancel", "remove_slot"):
            data.setdefault("reason", "time")
        self.client.force_login(user)
        return self.client.post(reverse("activity_action"), {"action": kind, "id": id, **data})

    def when(self, days):
        return timezone.localtime(timezone.now() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")

    def test_buyer_one_line_badge(self):
        booking = ViewingBooking.objects.create(slot=self.slot, buyer=self.buyer, status="confirmed")
        self.client.force_login(self.buyer)
        content = self.client.get(reverse("my_offers")).content.decode()
        group = content.split('<article class="buyer-flat"', 1)[1].split("</article>", 1)[0]
        line = group.split(f'id="slot-{self.slot.pk}"', 1)[1].split("</li>", 1)[0]
        self.assertIn("Asking $550,000", group)
        self.assertIn(">View flat</a>", group)
        self.assertIn(">Viewing</span>", line)
        self.assertEqual(line.count('class="sbadge sbadge--good"'), 1)
        self.assertIn("Confirmed", line)
        self.assertNotIn("<details", group)
        self.assertNotIn('value="cancel"', content)
        flat = self.client.get(reverse("listing_detail", args=[self.flat.publicId])).content.decode()
        mine = flat.split('id="your-viewing"', 1)[1].split("</div>", 1)[0]
        self.assertIn(f'href="{reverse('cancel_viewing')}?booking={booking.pk}">Cancel viewing', mine)
        self.assertNotIn('value="cancel"', mine)

    def test_seller_creates_slot_requested(self):
        response = self.act(self.seller, "slot", self.flat.publicId, startsAt=self.when(3), capacity="3")
        slot = ViewingSlot.objects.exclude(pk=self.slot.pk).get()
        self.assertEqual(slot.capacity, 3)
        self.assertEqual(response.url, reverse("seller_viewings") + f"#slot-{slot.pk}")

    def test_slot_creation_blocks_past(self):
        response = self.act(self.seller, "slot", self.flat.publicId, startsAt=self.when(-1))
        self.assertEqual(response.url, reverse("seller_viewings") + "#slot-form")
        self.act(self.seller, "slot", self.flat.publicId, startsAt=self.when(200))
        self.act(self.seller, "slot", self.flat.publicId, startsAt=self.when(3), capacity="0")
        self.assertEqual(ViewingSlot.objects.count(), 1)

    def test_seller_blocks_create_slot(self):
        other = makeUser("uiop@test.com", "seller")
        response = self.act(other, "slot", self.flat.publicId, startsAt=self.when(3))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ViewingSlot.objects.count(), 1)

    def test_buyer_booking_notifies_seller(self):
        self.act(self.buyer, "book", self.slot.pk)
        booking = ViewingBooking.objects.get()
        self.assertEqual((booking.buyer, booking.status), (self.buyer, "requested"))
        self.assertTrue(ActivityNotice.objects.filter(user=self.seller, kind="viewing_booked").exists())
        self.act(self.buyer, "book", self.slot.pk)
        self.assertEqual(ViewingBooking.objects.count(), 1)

    def test_booking_blocks_slot_at(self):
        self.act(self.buyer, "book", self.slot.pk)
        self.act(self.other, "book", self.slot.pk)
        self.assertEqual(ViewingBooking.objects.count(), 1)

    def test_buyer_cannot_book_past_slot(self):
        self.slot.startsAt = timezone.now() - timedelta(hours=1)
        self.slot.save()
        self.act(self.buyer, "book", self.slot.pk)
        self.assertFalse(ViewingBooking.objects.exists())

    def test_confirm_cancel_rebook_update(self):
        booking = ViewingBooking.objects.create(slot=self.slot, buyer=self.buyer)
        self.act(self.seller, "confirm", booking.pk)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "confirmed")
        self.assertTrue(ActivityNotice.objects.filter(user=self.buyer, kind="viewing_confirmed").exists())
        self.act(self.buyer, "cancel", booking.pk)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")
        self.act(self.buyer, "book", self.slot.pk)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "requested")

    def test_booking_actions_require_owner(self):
        booking = ViewingBooking.objects.create(slot=self.slot, buyer=self.buyer)
        self.act(self.other, "cancel", booking.pk)
        self.act(self.buyer, "confirm", booking.pk)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "requested")
        self.act(self.seller, "seller_cancel", booking.pk)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")

    def test_removing_booked_slot_requires(self):
        booking = ViewingBooking.objects.create(slot=self.slot, buyer=self.buyer, status="confirmed")
        self.act(self.seller, "remove_slot", self.slot.pk)
        self.assertTrue(ViewingSlot.objects.filter(pk=self.slot.pk, removed=False).exists())
        self.act(self.seller, "remove_slot", self.slot.pk, confirm="yes")
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")
        self.assertTrue(ViewingSlot.objects.get(pk=self.slot.pk).removed)
        self.assertTrue(ActivityNotice.objects.filter(user=self.buyer, kind="viewing_cancelled").exists())

    def test_removing_empty_slot_deletes(self):
        self.act(self.seller, "remove_slot", self.slot.pk)
        self.assertFalse(ViewingSlot.objects.exists())

    def test_buyer_context_shows_upcoming(self):
        ViewingBooking.objects.create(slot=self.slot, buyer=self.buyer, status="confirmed")
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("my_offers"))
        self.assertEqual(len(response.context["upcomingBookings"]), 1)
        self.assertEqual(response.context["nextViewing"].slot, self.slot)
        response = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        self.assertEqual(response.context["myBookedSlots"], {self.slot.pk})
        self.assertFalse(response.context["canBook"])

    def test_confirming_booking_respects(self):
        self.slot.capacity = 1
        self.slot.save()
        first = ViewingBooking.objects.create(slot=self.slot, buyer=self.buyer, status="confirmed")
        second = ViewingBooking.objects.create(slot=self.slot, buyer=self.other)
        self.act(self.seller, "confirm", second.pk)
        second.refresh_from_db()
        self.assertEqual(second.status, "requested")
        self.act(self.seller, "seller_cancel", first.pk)
        self.act(self.seller, "confirm", second.pk)
        second.refresh_from_db()
        self.assertEqual(second.status, "confirmed")

    def test_past_reject_booking(self):
        self.slot.startsAt = timezone.now() - timedelta(hours=1)
        self.slot.save()
        response = self.act(self.buyer, "book", self.slot.pk)
        self.assertEqual(
            response.url,
            reverse("listing_detail", args=[self.flat.publicId]) + "#listing-action-card",
        )
        ViewingBooking.objects.create(slot=self.slot, buyer=self.buyer, status="confirmed")
        response = self.act(self.buyer, "cancel", ViewingBooking.objects.get().pk)
        self.assertEqual(ViewingBooking.objects.get().status, "confirmed")
        self.assertEqual(response.url, reverse("my_offers"))

    def test_guest_sees_times_signing(self):
        page = reverse("listing_detail", args=[self.flat.publicId])
        response = self.client.get(page)
        self.assertContains(response, "Viewing times")
        self.assertContains(response, timezone.localtime(self.slot.startsAt).strftime("%a %-d %b"))
        self.assertContains(response, "1 spot left")
        self.assertContains(response, "Sign in to make an offer or visit")
        self.assertNotContains(response, 'name="action" value="book"')
        self.slot.delete()
        self.assertContains(self.client.get(page), "No viewing times yet.")

    def test_requesting_stays_on_flat(self):
        page = reverse("listing_detail", args=[self.flat.publicId])
        response = self.act(self.buyer, "book", self.slot.pk)
        self.assertRedirects(response, page + "#your-viewing", fetch_redirect_response=False)
        flat = self.client.get(page)
        for check_text_1 in (
            "Waiting for the seller to confirm.",
            'id="your-viewing"',
            '<span class="sbadge sbadge--warn">',
            'class="stack buyer-offer-form"',
        ):
            self.assertContains(flat, check_text_1)

    def test_flat_page_names_state(self):
        booking = ViewingBooking.objects.create(slot=self.slot, buyer=self.buyer)
        page = reverse("listing_detail", args=[self.flat.publicId])
        self.client.force_login(self.buyer)
        response = self.client.get(page)
        self.assertEqual(response.context["activitySlots"][0].myBooking, booking)
        self.assertContains(response, '<span class="sbadge sbadge--warn">')
        self.assertContains(response, "Waiting for seller to confirm")
        booking.status = "confirmed"
        booking.save()
        response = self.client.get(page)
        self.assertContains(response, '<span class="sbadge sbadge--good">')
        self.assertContains(response, "Confirmed")
        self.assertNotContains(response, "Waiting for seller to confirm")

    def test_flat_missing_times_points(self):
        self.slot.delete()
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        for check_text_2 in (
            "No viewing times yet. Ask the seller and we’ll tell you when they add some.",
            "Ask for a viewing time",
            'type="submit">Send offer</button>',
        ):
            self.assertContains(response, check_text_2)
        self.assertNotContains(response, ">Request viewing</button>")

    def test_flat_page_includes_available(self):
        later = []
        for days in (3, 4, 5, 6):
            later.append(
                ViewingSlot.objects.create(listing=self.flat, startsAt=timezone.now() + timedelta(days=days))
            )
        ViewingBooking.objects.create(slot=self.slot, buyer=self.buyer)
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        self.assertEqual(response.context["activitySlots"], [self.slot] + later)

    def test_booking_confirmation_notifies(self):
        booking = ViewingBooking.objects.create(slot=self.slot, buyer=self.buyer)
        self.act(self.seller, "confirm", booking.pk)
        self.act(self.seller, "confirm", booking.pk)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "confirmed")
        self.assertEqual(ActivityNotice.objects.filter(user=self.buyer, kind="viewing_confirmed").count(), 1)


class SlotRuleTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("asdf@test.com", "seller")
        cls.buyer = makeUser("qwer@test.com")
        cls.flat = makeListing(cls.seller)

    def act(self, user, kind, id, **data):
        if kind in ("cancel", "seller_cancel", "remove_slot"):
            data.setdefault("reason", "time")
        self.client.force_login(user)
        return self.client.post(reverse("activity_action"), {"action": kind, "id": id, **data})

    def test_past_viewing_blocks_removed(self):
        past = ViewingSlot.objects.create(listing=self.flat, startsAt=timezone.now() - timedelta(hours=2))
        self.act(self.seller, "remove_slot", past.pk)
        self.assertTrue(ViewingSlot.objects.filter(pk=past.pk, removed=False).exists())

    def test_recreating_removed_reuses_time(self):
        when = timezone.now().replace(second=0, microsecond=0) + timedelta(days=2)
        slot = ViewingSlot.objects.create(listing=self.flat, startsAt=when)
        ViewingBooking.objects.create(slot=slot, buyer=self.buyer, status="cancelled")
        self.act(self.seller, "remove_slot", slot.pk)
        slot.refresh_from_db()
        self.assertTrue(slot.removed)
        starts = timezone.localtime(when).strftime("%Y-%m-%dT%H:%M")
        self.act(self.seller, "slot", self.flat.publicId, startsAt=starts, capacity="4")
        slot.refresh_from_db()
        self.assertEqual((slot.removed, slot.capacity), (False, 4))
        self.assertEqual(ViewingSlot.objects.count(), 1)

    def test_later_booked_kept_in(self):
        when = timezone.now() + timedelta(days=1)
        for i in range(6):
            ViewingSlot.objects.create(listing=self.flat, startsAt=when + timedelta(hours=i))
        mine = ViewingSlot.objects.create(listing=self.flat, startsAt=when + timedelta(days=5))
        ViewingBooking.objects.create(slot=mine, buyer=self.buyer)
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        slots = response.context["activitySlots"]
        self.assertEqual(len(slots), 5)
        self.assertEqual(slots[-1], mine)
        self.assertTrue(response.context["canBook"])

    def test_review_listing_blocks_add(self):
        other = self.flat
        other.status = "review"
        other.save()
        response = self.act(self.seller, "slot", other.publicId, startsAt="2030-01-01T10:00")
        self.assertFalse(ViewingSlot.objects.exists())
        self.assertEqual(response.url, reverse("seller_viewings") + "#slot-form")

    def test_capacity_dots_shown_seller(self):
        slot = ViewingSlot.objects.create(
            listing=self.flat, startsAt=timezone.now() + timedelta(days=2), capacity=3
        )
        ViewingBooking.objects.create(slot=slot, buyer=self.buyer)
        self.assertEqual(slot.capacityDots, "●○○")
        self.client.force_login(self.seller)
        self.assertContains(self.client.get(reverse("seller_viewings")), "●○○")
        self.client.force_login(makeUser("zxcv@test.com"))
        self.assertContains(self.client.get(reverse("listing_detail", args=[self.flat.publicId])), "●○○")


class CancelRuleTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("cancel-seller@test.com", "seller")
        cls.buyer = makeUser("cancel-buyer@test.com", "buyer")
        cls.flat = makeListing(cls.seller)

    def slot(self, days=2):
        return ViewingSlot.objects.create(
            listing=self.flat, startsAt=timezone.now() + timedelta(days=days), capacity=5
        )

    def booking(self, status="confirmed", days=2):
        return ViewingBooking.objects.create(slot=self.slot(days), buyer=self.buyer, status=status)

    def act(self, user, kind, id, **data):
        self.client.force_login(user)
        if kind in ("cancel", "seller_cancel", "remove_slot"):
            data["cancelPage"] = "1"
        return self.client.post(reverse("activity_action"), {"action": kind, "id": id, **data}, follow=True)

    def test_buyer_must_give_reason(self):
        booking = self.booking()
        response = self.act(self.buyer, "cancel", booking.pk)
        self.assertContains(response, "Please tell us why you’re cancelling.")
        self.assertContains(response, "Why are you cancelling?")
        booking.refresh_from_db()
        self.assertEqual(booking.status, "confirmed")
        response = self.act(self.buyer, "cancel", booking.pk, reason="other", note="ok")
        self.assertContains(response, "Please tell us in a few words why you’re cancelling.")
        self.act(self.buyer, "cancel", booking.pk, reason="time")
        booking.refresh_from_db()
        self.assertEqual(
            (booking.status, booking.cancelledBy, booking.cancelReason, booking.cancelCounts),
            ("cancelled", "buyer", "time", True),
        )
        notice = ActivityNotice.objects.get(user=self.seller, kind="viewing_cancelled")
        self.assertIn("Reason: The time no longer works", notice.text)

    def test_page_shows_viewing_reasons(self):
        booking = self.booking()
        self.client.force_login(self.buyer)
        page = self.client.get(reverse("cancel_viewing"), {"booking": booking.pk})
        self.assertContains(page, "Cancel this viewing?")
        self.assertContains(page, "I found another flat")
        self.assertContains(page, f"cancellation 1 of {CANCEL_LIMIT} in {CANCEL_DAYS} days")
        self.assertContains(page, "Keep viewing")
        self.client.force_login(makeUser("stranger@test.com", "buyer"))
        self.assertEqual(self.client.get(reverse("cancel_viewing"), {"booking": booking.pk}).status_code, 404)

    def test_limit_buyer_blocks_book(self):
        for number in range(CANCEL_LIMIT):
            self.act(self.buyer, "cancel", self.booking(days=number + 2).pk, reason="plans")
        fresh = self.slot(days=9)
        response = self.act(self.buyer, "book", fresh.pk)
        self.assertContains(
            response,
            f"You’ve cancelled {CANCEL_LIMIT} confirmed viewings in the last {CANCEL_DAYS} days",
        )
        self.assertFalse(ViewingBooking.objects.filter(slot=fresh).exists())
        ViewingBooking.objects.filter(cancelCounts=True).update(
            cancelledAt=timezone.now() - timedelta(days=CANCEL_DAYS + 1)
        )
        self.act(self.buyer, "book", fresh.pk)
        self.assertTrue(ViewingBooking.objects.filter(slot=fresh, status="requested").exists())

    def test_cancelling_unconfirmed_request(self):
        for number in range(CANCEL_LIMIT + 1):
            self.act(
                self.buyer,
                "cancel",
                self.booking(status="requested", days=number + 2).pk,
                reason="plans",
            )
        self.assertEqual(ViewingBooking.objects.filter(cancelCounts=True).count(), 0)
        fresh = self.slot(days=9)
        self.act(self.buyer, "book", fresh.pk)
        self.assertTrue(ViewingBooking.objects.filter(slot=fresh, status="requested").exists())

    def test_seller_gives_reason_same(self):
        booking = self.booking()
        response = self.act(self.seller, "seller_cancel", booking.pk)
        self.assertContains(response, "Please tell us why you’re cancelling.")
        self.act(self.seller, "seller_cancel", booking.pk, reason="unavailable")
        notice = ActivityNotice.objects.get(user=self.buyer, kind="viewing_cancelled")
        self.assertIn("Reason: The flat is no longer available", notice.text)
        for number in range(CANCEL_LIMIT - 1):
            taken = self.booking(days=number + 3)
            self.act(self.seller, "remove_slot", taken.slot.pk, confirm="yes", reason="time")
        when = timezone.localtime(timezone.now() + timedelta(days=12)).strftime("%Y-%m-%dT%H:%M")
        response = self.act(self.seller, "slot", self.flat.publicId, startsAt=when, capacity="2")
        self.assertContains(response, "so you can’t add viewing times until")

    def test_declining_request_needs_no(self):
        booking = self.booking(status="requested")
        self.act(self.seller, "seller_cancel", booking.pk)
        booking.refresh_from_db()
        self.assertEqual((booking.status, booking.cancelCounts), ("cancelled", False))


class MoveViewingTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.buyer = makeUser("qwer@test.com")
        self.flat = makeListing(self.seller)
        self.first = self.slot(2)
        self.second = self.slot(3)
        self.booking = ViewingBooking.objects.create(slot=self.first, buyer=self.buyer, status="confirmed")

    def slot(self, days, capacity=2):
        return ViewingSlot.objects.create(
            listing=self.flat, startsAt=timezone.now() + timedelta(days=days), capacity=capacity
        )

    def move(self, slot, booking=None):
        self.client.force_login(self.buyer)
        booking = booking or self.booking
        return self.client.post(
            reverse("move_viewing"), {"booking": booking.pk, "slot": slot.pk}, follow=True
        )

    def test_moving_frees_old_time(self):
        response = self.move(self.second)
        self.assertContains(response, "We’ve asked the seller to agree to the new time.")
        self.booking.refresh_from_db()
        self.assertEqual(
            (self.booking.status, self.booking.cancelReason, self.booking.cancelCounts),
            ("cancelled", "moved", False),
        )
        moved = ViewingBooking.objects.get(slot=self.second, buyer=self.buyer)
        self.assertEqual((moved.status, moved.movedFromStatus), ("requested", "confirmed"))
        notices = ActivityNotice.objects.filter(user=self.seller)
        self.assertEqual(notices.count(), 1)
        notice = notices.get()
        self.assertEqual(notice.kind, "viewing_moved")
        self.assertTrue(notice.text.startswith("A buyer moved their viewing of Blk 123 Yishun from "))
        self.assertIn(" to ", notice.text)
        self.assertEqual(notice.url, reverse("seller_viewings") + f"#slot-{self.second.pk}")

    def test_page_lists_other_open(self):
        full = self.slot(4, capacity=1)
        ViewingBooking.objects.create(slot=full, buyer=makeUser("zxcv@test.com"), status="confirmed")
        self.client.force_login(self.buyer)
        page = self.client.get(reverse("move_viewing"), {"booking": self.booking.pk})
        self.assertContains(page, "Change your viewing time")
        self.assertContains(page, f'value="{self.second.pk}"')
        self.assertNotContains(page, f'name="slot" value="{full.pk}"')
        self.assertNotContains(page, f'name="slot" value="{self.first.pk}"')
        self.assertContains(page, "Move my viewing")
        self.assertContains(page, "Keep current time")
        self.client.force_login(makeUser("zxcv2@test.com"))
        self.assertEqual(
            self.client.get(reverse("move_viewing"), {"booking": self.booking.pk}).status_code, 404
        )

    def test_moved_once(self):
        self.move(self.second)
        moved = ViewingBooking.objects.get(slot=self.second, buyer=self.buyer)
        third = self.slot(5)
        response = self.move(third, moved)
        self.assertContains(response, "You’ve already moved this viewing once.")
        self.assertFalse(ViewingBooking.objects.filter(slot=third).exists())

    def test_cancelling_moved_confirmed(self):
        self.move(self.second)
        moved = ViewingBooking.objects.get(slot=self.second, buyer=self.buyer)
        self.client.post(reverse("activity_action"), {"action": "cancel", "id": moved.pk, "reason": "plans"})
        moved.refresh_from_db()
        self.assertEqual((moved.status, moved.cancelCounts), ("cancelled", True))

    def test_buyer_paused_cancelling_blocks(self):
        for number in range(CANCEL_LIMIT):
            ViewingBooking.objects.create(
                slot=self.slot(10 + number),
                buyer=self.buyer,
                status="cancelled",
                cancelledBy="buyer",
                cancelCounts=True,
                cancelledAt=timezone.now(),
            )
        response = self.move(self.second)
        self.assertContains(response, f"You’ve cancelled {CANCEL_LIMIT} confirmed viewings")
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "confirmed")

    def test_full_time_paused_flat(self):
        full = self.slot(4, capacity=1)
        ViewingBooking.objects.create(slot=full, buyer=makeUser("zxcv@test.com"), status="requested")
        response = self.move(full)
        self.assertContains(response, "That time is full or no longer open.")
        Listing.objects.filter(pk=self.flat.pk).update(status="paused")
        response = self.move(self.second)
        self.assertContains(response, "The seller has paused this flat for now.")
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "confirmed")

    def test_flat_page_my_offers(self):
        self.client.force_login(self.buyer)
        page = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        self.assertContains(page, f"{reverse('move_viewing')}?booking={self.booking.pk}")
        page = self.client.get(reverse("my_offers"))
        self.assertContains(page, f"{reverse('move_viewing')}?booking={self.booking.pk}&amp;from=offers")
