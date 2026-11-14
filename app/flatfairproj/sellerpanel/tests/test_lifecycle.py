from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from adminpanel.models import AuditLog
from storefront.listing_rules import otherOpen
from storefront.models import (
    ActivityNotice,
    Collection,
    Listing,
    Offer,
    SavedListing,
    ViewingBooking,
    ViewingSlot,
)
from storefront.offeradvice import suggestion
from storefront.recommend_flats import goodFlats
from storefront.tests.helpers import makeListing, makeStaff, makeUser


class PauseListingTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.buyer = makeUser("qwer@test.com")
        self.saver = makeUser("zxcv@test.com")
        self.flat = makeListing(
            self.seller, createdAt=timezone.now() - timedelta(days=60), askingPrice=700000
        )
        self.offer = Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=650000)
        self.slot = ViewingSlot.objects.create(listing=self.flat, startsAt=timezone.now() + timedelta(days=2))
        self.booking = ViewingBooking.objects.create(slot=self.slot, buyer=self.buyer, status="confirmed")
        collection = Collection.objects.create(user=self.saver, name="Saved")
        SavedListing.objects.create(collection=collection, listing=self.flat)

    def pause(self, reason="travel"):
        self.client.force_login(self.seller)
        return self.client.post(
            reverse("pause_listing", args=[self.flat.publicId]), {"reason": reason}, follow=True
        )

    def test_page_asks_first_gives(self):
        self.client.force_login(self.seller)
        page = self.client.get(reverse("pause_listing", args=[self.flat.publicId]))
        for check_text_1 in (
            "Pause your listing?",
            "Why are you pausing? (optional)",
            "I’m travelling",
            "We’ll let the 2 buyers following your flat know.",
            "Keep it live",
        ):
            self.assertContains(page, check_text_1)
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "live")

    def test_pausing_hides_flat_but(self):
        response = self.pause()
        self.assertContains(response, "Your listing is paused. We’ve let 2 interested buyers know.")
        self.flat.refresh_from_db()
        self.assertEqual((self.flat.status, self.flat.pauseReason), ("paused", "travel"))
        self.assertIsNotNone(self.flat.pausedAt)
        self.assertFalse(Listing.objects.public().filter(pk=self.flat.pk).exists())
        self.offer.refresh_from_db()
        self.booking.refresh_from_db()
        self.assertEqual((self.offer.status, self.booking.status), ("pending", "confirmed"))
        buyerNotice = ActivityNotice.objects.get(user=self.buyer, kind="flat_paused")
        self.assertIn("Your offer and viewing stay as they are", buyerNotice.text)
        saverNotice = ActivityNotice.objects.get(user=self.saver, kind="flat_paused")
        self.assertIn("a flat you saved, is paused for now", saverNotice.text)
        self.assertTrue(
            AuditLog.objects.filter(action="listing paused by seller", detail="I’m travelling").exists()
        )

    def test_buyers_see_paused_page(self):
        self.pause()
        other = makeUser("other@test.com")
        self.client.force_login(other)
        page = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        self.assertContains(page, "Paused by the seller")
        self.assertContains(
            page,
            "The seller has paused this flat, so you can’t make an offer or book a viewing just now.",
        )
        self.client.post(
            reverse("activity_action"),
            {"action": "offer", "id": self.flat.publicId, "amount": 650000},
        )
        self.client.post(reverse("activity_action"), {"action": "book", "id": self.slot.pk})
        self.assertFalse(Offer.objects.filter(buyer=other).exists())
        self.assertFalse(ViewingBooking.objects.filter(buyer=other).exists())
        search = self.client.get(reverse("listings"))
        self.assertNotContains(search, str(self.flat.publicId))

    def test_resuming_puts_back_live(self):
        self.pause()
        self.client.force_login(self.seller)
        response = self.client.post(reverse("resume_listing", args=[self.flat.publicId]), follow=True)
        self.assertContains(response, "Your listing is live again. We’ve let 2 interested buyers know.")
        self.flat.refresh_from_db()
        self.assertEqual((self.flat.status, self.flat.pauseReason, self.flat.pausedAt), ("live", "", None))
        self.assertTrue(Listing.objects.public().filter(pk=self.flat.pk).exists())
        self.assertIn(
            "is back on sale", ActivityNotice.objects.get(user=self.buyer, kind="flat_resumed").text
        )
        self.assertIn(
            "a flat you saved, is back on sale",
            ActivityNotice.objects.get(user=self.saver, kind="flat_resumed").text,
        )

    def test_paused_shows_in_my(self):
        self.pause("renovate")
        self.client.force_login(self.seller)
        page = self.client.get(reverse("my_listings"))
        self.assertContains(page, "Paused")
        page = self.client.get(reverse("seller_listing", args=[self.flat.publicId]))
        self.assertContains(page, "Resume listing")
        self.assertNotContains(page, "Pause listing")
        self.client.force_login(makeStaff("staff@test.com"))
        page = self.client.get(reverse("admin_listings"), {"status": "paused"})
        self.assertContains(page, "Paused")
        page = self.client.get(reverse("admin_listing", args=[self.flat.pk]))
        self.assertContains(page, "Paused by the seller")
        self.assertContains(page, "Reason: I’m doing some renovation")

    def test_paused_flat_counts_as(self):
        self.pause()
        self.client.force_login(self.seller)
        response = self.client.get(reverse("sell"), follow=True)
        self.assertContains(response, "You can sell one flat at a time.")

    def test_live_flat_your_own(self):
        self.client.force_login(makeUser("stranger@test.com", "seller"))
        response = self.client.post(reverse("pause_listing", args=[self.flat.publicId]))
        self.assertEqual(response.status_code, 404)
        Listing.objects.filter(pk=self.flat.pk).update(status="review")
        response = self.pause()
        self.assertContains(response, "You can pause a flat only while buyers can see it.")
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")

    def test_price_tips_recommendations_skip(self):
        Listing.objects.filter(pk=self.flat.pk).update(estimatedPrice=600000)
        self.flat.refresh_from_db()
        Offer.objects.filter(pk=self.offer.pk).update(createdAt=timezone.now() - timedelta(days=40))
        self.assertIsNotNone(suggestion(self.flat))
        self.pause()
        self.flat.refresh_from_db()
        self.assertIsNone(suggestion(self.flat))
        self.assertNotIn(self.flat.pk, goodFlats(self.saver).values_list("pk", flat=True))


class WithdrawListingTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.buyer = makeUser("qwer@test.com")
        self.viewer = makeUser("zxcv@test.com")
        self.saver = makeUser("uiop@test.com")
        self.flat = makeListing(self.seller)
        self.offer = Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=520000)
        self.slot = ViewingSlot.objects.create(listing=self.flat, startsAt=timezone.now() + timedelta(days=2))
        self.booking = ViewingBooking.objects.create(slot=self.slot, buyer=self.viewer, status="confirmed")
        collection = Collection.objects.create(user=self.saver, name="Saved")
        SavedListing.objects.create(collection=collection, listing=self.flat)

    def delete(self, **data):
        self.client.force_login(self.seller)
        return self.client.post(reverse("delete_listing", args=[self.flat.publicId]), data, follow=True)

    def test_delete_page_says_what(self):
        self.client.force_login(self.seller)
        page = self.client.get(reverse("delete_listing", args=[self.flat.publicId]))
        for check_text_2 in (
            "Delete your listing?",
            "we’ll close 1 open offer and cancel 1 viewing",
            "We’ll let the 3 buyers following your flat know.",
            "I sold it through an agent",
            "Keep my listing",
        ):
            self.assertContains(page, check_text_2)

    def test_a_reason_is_required(self):
        response = self.delete()
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Please tell us why you’re deleting this listing.", status_code=400)
        response = self.delete(reason="other", note="no")
        self.assertContains(
            response,
            "Please tell us in a few words why you’re deleting this listing.",
            status_code=400,
        )
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "live")

    def test_deleting_closes_everything(self):
        response = self.delete(reason="agent")
        self.assertContains(response, "Your listing is deleted. We’ve let 3 interested buyers know")
        self.flat.refresh_from_db()
        self.assertEqual((self.flat.status, self.flat.withdrawReason), ("withdrawn", "agent"))
        self.assertIsNotNone(self.flat.withdrawnAt)
        self.offer.refresh_from_db()
        self.booking.refresh_from_db()
        self.assertEqual(self.offer.status, "declined")
        self.assertEqual((self.booking.status, self.booking.cancelCounts), ("cancelled", False))
        self.assertIn(
            "so we’ve closed your offer",
            ActivityNotice.objects.get(user=self.buyer, kind="flat_withdrawn").text,
        )
        self.assertIn(
            "so we’ve cancelled your viewing",
            ActivityNotice.objects.get(user=self.viewer, kind="flat_withdrawn").text,
        )
        self.assertIn(
            "is no longer for sale",
            ActivityNotice.objects.get(user=self.saver, kind="flat_withdrawn").text,
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="listing deleted by seller", detail="I sold it through an agent"
            ).exists()
        )

    def test_seller_list_new_flat(self):
        self.assertTrue(otherOpen(self.seller))
        self.delete(reason="other", note="Moving overseas")
        self.assertFalse(otherOpen(self.seller))
        makeListing(self.seller, block="456", postalCode="760456")
        self.assertEqual(Listing.objects.filter(seller=self.seller).count(), 2)

    def test_flat_agreed_price_blocks(self):
        Offer.objects.filter(pk=self.offer.pk).update(status="accepted", respondedAt=timezone.now())
        response = self.delete(reason="mind")
        self.assertContains(response, "You’ve agreed a price with a buyer.")
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "live")

    def test_leaves_my_but_admin(self):
        self.delete(reason="other", note="Moving overseas")
        self.client.force_login(self.seller)
        self.assertNotContains(self.client.get(reverse("my_listings")), str(self.flat.publicId))
        self.assertEqual(
            self.client.get(reverse("seller_listing", args=[self.flat.publicId])).status_code, 404
        )
        self.client.force_login(makeStaff("staff@test.com"))
        page = self.client.get(reverse("admin_listing", args=[self.flat.pk]))
        self.assertContains(page, "Deleted by the seller")
        self.assertContains(page, "Reason: Something else: “Moving overseas”")

    def test_buyers_opening_old_link(self):
        self.delete(reason="known")
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("listing_detail", args=[self.flat.publicId]), follow=True)
        self.assertContains(response, "This flat is no longer for sale.")
        self.assertFalse(Listing.objects.public().filter(pk=self.flat.pk).exists())

    def test_sold_flats_blocks_deleted(self):
        Listing.objects.filter(pk=self.flat.pk).update(status="sold", statusBeforeSold="live")
        response = self.delete(reason="mind")
        self.assertContains(response, "Sold flats stay in your records")
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "sold")


class NextStepsTests(TestCase):

    def setUp(self):
        self.seller = makeUser("next-steps@test.com", "seller")
        self.flat = makeListing(self.seller, status="live", block="123", town="YISHUN")
        self.client.force_login(self.seller)

    def test_newly_live_flat_shows(self):
        page = self.client.get(reverse("dashboard"))
        for text in (
            "Blk 123 Yishun is on FlatFair",
            "Add viewing times",
            "Add time",
            "Share on WhatsApp",
            "Email the link",
            "Set a minimum",
            "See boosts",
            "Got it, I’ll do the rest later",
            "data-open-on-load",
        ):
            self.assertContains(page, text)
        self.assertContains(page, 'class="next-step is-next"', count=1)

    def test_finished_steps_are_ticked(self):
        ViewingSlot.objects.create(listing=self.flat, startsAt=timezone.now() + timedelta(days=2), capacity=2)
        Listing.objects.filter(pk=self.flat.pk).update(autoDeclineLow=True)
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertEqual(page.count('class="next-step is-done"'), 2)
        self.assertIn("1 time added · Add more", page)
        self.assertNotIn("Set a minimum", page)

    def test_got_hides_good(self):
        response = self.client.post(
            reverse("next_steps_done", args=[self.flat.publicId]), {"next": reverse("my_listings")}
        )
        self.assertRedirects(response, reverse("my_listings"), fetch_redirect_response=False)
        self.assertNotContains(self.client.get(reverse("dashboard")), "data-open-on-load")

    def test_closing_box_remembered_missing(self):
        response = self.client.post(reverse("next_steps_done", args=[self.flat.publicId]), HTTP_X_BEACON="1")
        self.assertEqual(response.status_code, 204)
        self.flat.refresh_from_db()
        self.assertIsNotNone(self.flat.nextStepsSeenAt)

    def test_opens_page_missing_box(self):
        expected = {
            "viewings": reverse("seller_viewings") + "?first=1#slot-form",
            "minimum": reverse("seller_listing", args=[self.flat.publicId]) + "#offer-minimum",
            "boost": reverse("packages") + f"?listing={self.flat.publicId}",
        }
        for step, url in expected.items():
            response = self.client.get(reverse("next_step_go", args=[self.flat.publicId, step]))
            self.assertRedirects(response, url, fetch_redirect_response=False)
            self.assertNotContains(self.client.get(reverse("dashboard")), "data-open-on-load")
            self.assertContains(self.client.get(reverse("dashboard")), "data-open-on-load")
        self.flat.refresh_from_db()
        self.assertIsNone(self.flat.nextStepsSeenAt)

    def test_adding_time_in_box(self):
        when = (timezone.localtime() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
        self.client.post(
            reverse("activity_action"),
            {"action": "slot", "id": self.flat.publicId, "startsAt": when, "capacity": "2"},
        )
        self.assertTrue(ViewingSlot.objects.filter(listing=self.flat).exists())
        page = self.client.get(reverse("seller_viewings")).content.decode()
        self.assertIn('class="next-step is-done"', page)
        self.assertIn('class="next-step is-next"', page)

    def test_flats_being_checked_already(self):
        Listing.objects.filter(pk=self.flat.pk).update(status="review")
        self.assertNotContains(self.client.get(reverse("dashboard")), "data-open-on-load")
        Listing.objects.filter(pk=self.flat.pk).update(status="live", nextStepsSeenAt=timezone.now())
        self.assertNotContains(self.client.get(reverse("dashboard")), "data-open-on-load")

    def test_another_seller_blocks_dismiss(self):
        self.client.force_login(makeUser("other-seller@test.com", "seller"))
        self.assertEqual(
            self.client.post(reverse("next_steps_done", args=[self.flat.publicId])).status_code, 404
        )
        self.flat.refresh_from_db()
        self.assertIsNone(self.flat.nextStepsSeenAt)


class StepLinkTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("steps-seller@test.com", "seller")
        cls.buyer = makeUser("steps-buyer@test.com", "buyer")
        cls.flat = makeListing(cls.seller)

    def steps(self):
        self.client.force_login(self.seller)
        page = self.client.get(reverse("dashboard")).content.decode()
        return page.split('class="journey selling-journey', 1)[1].split("</ol>", 1)[0]

    def test_even_done(self):
        slot = ViewingSlot.objects.create(
            listing=self.flat, startsAt=timezone.now() + timedelta(days=2), capacity=2
        )
        ViewingBooking.objects.create(slot=slot, buyer=self.buyer, status="confirmed")
        Offer.objects.create(
            listing=self.flat,
            buyer=self.buyer,
            amount=self.flat.askingPrice,
            status="accepted",
            respondedAt=timezone.now(),
        )
        steps = self.steps()
        flatPage = reverse("seller_listing", args=[self.flat.publicId])
        self.assertEqual(steps.count("<a href="), 4)
        self.assertIn(f'<a href="{flatPage}">List your flat (free)</a>', steps)
        self.assertIn(f'<a href="{reverse('seller_viewings')}#viewings-tab">Host viewings</a>', steps)
        self.assertIn(f'<a href="{reverse('seller_viewings')}#offers-tab">Accept an offer</a>', steps)
        self.assertIn(f'<a href="{flatPage}">Finish with HDB, then mark it sold</a>', steps)
