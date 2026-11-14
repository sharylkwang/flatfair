from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from adminpanel.models import Decision
from .helpers import CompletedPhotoQueueMixin
from sellerpanel.listings import sellState
from storefront.models import (
    lock_user,
    ActivityNotice,
    Collection,
    ListingPhoto,
    Offer,
    SavedListing,
    ViewingBooking,
    ViewingSlot,
)
from storefront.tests.helpers import (
    CLEAN,
    FAIR,
    FLAT,
    ROOMS,
    jpeg,
    makeListing,
    makeStaff,
    makeUser,
)


def flatFields():
    return {
        "town": "JURONG EAST",
        "block": "225A",
        "postalCode": "601225",
        "flatType": "3 ROOM",
        "flatModel": "New Generation",
        "floorAreaSqm": 67,
        "bedrooms": 2,
        "bathrooms": 1,
        "leaseCommenceYear": 1984,
        "askingPrice": 400000,
        "estimatedPrice": 400000,
        "bandLow": 350000,
        "bandHigh": 450000,
    }


class PriceTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.flat = makeListing(self.seller, **flatFields())
        self.client.force_login(self.seller)
        price = patch("sellerpanel.listings.band", return_value=FAIR)
        price.start()
        self.addCleanup(price.stop)
        self.url = reverse("update_price", args=[self.flat.publicId])

    def test_list_row_one_view(self):
        content = self.client.get(reverse("my_listings")).content.decode()
        row = content.split(f'id="listing-{self.flat.publicId}"', 1)[1].split("</article>", 1)[0]
        for fact in ("$400,000", "Live", "View listing <span"):
            self.assertIn(fact, row)
        self.assertEqual(row.count("<a "), 1)
        self.assertIn(reverse("seller_listing", args=[self.flat.publicId]), row)
        for gone in (
            "Change listing",
            "View flat",
            "Save price",
            "Mark as sold",
            "<details",
            "<form",
            'name="askingPrice"',
        ):
            self.assertNotIn(gone, content.split("<main", 1)[1].split("</main>", 1)[0])

    def test_listing_page_summary_one(self):
        response = self.client.get(reverse("seller_listing", args=[self.flat.publicId]))
        for fact in (
            "$400,000",
            "No offers yet",
            "0 views",
            "Free listing",
            'aria-label="Edit listing"',
        ):
            self.assertContains(response, fact)
        self.assertContains(response, reverse("edit_listing", args=[self.flat.publicId]), count=1)
        self.assertContains(response, "Change your price")
        self.assertContains(response, 'name="askingPrice"', count=1)
        edit = self.client.get(reverse("edit_listing", args=[self.flat.publicId]))
        self.assertContains(edit, "Mark as sold")
        self.assertContains(edit, f'href="{reverse('seller_listing', args=[self.flat.publicId])}">Cancel</a>')

    def test_sold_listing_page_no(self):
        self.flat.status = "sold"
        self.flat.statusBeforeSold = "live"
        self.flat.save()
        response = self.client.get(reverse("seller_listing", args=[self.flat.publicId]))
        self.assertContains(response, "Sold flats can’t be edited.")
        self.assertNotContains(response, "Edit listing")
        self.assertNotContains(response, reverse("edit_listing", args=[self.flat.publicId]))
        other = makeUser("other-seller@test.com", "seller")
        self.client.force_login(other)
        self.assertEqual(
            self.client.get(reverse("seller_listing", args=[self.flat.publicId])).status_code, 404
        )

    def test_fair_update_keeps_listing(self):
        response = self.client.post(self.url, {"askingPrice": "420000"})
        self.assertEqual(response.url, reverse("my_listings") + f"#listing-{self.flat.publicId}")
        self.flat.refresh_from_db()
        self.assertEqual((self.flat.askingPrice, self.flat.status), (420000, "live"))

    def test_high_update_sends_listing(self):
        self.client.post(self.url, {"askingPrice": "480000"})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")

    def test_lowering_unheld_review_fair(self):
        self.flat.status = "review"
        self.flat.askingPrice = 480000
        self.flat.save()
        rows = self.client.get(reverse("my_listings")).context["rows"]
        self.assertEqual(rows[0]["lowerTo"], 450000)
        self.client.post(self.url, {"askingPrice": "440000"})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "live")

    def test_invalid_keeps_listing_returns(self):
        response = self.client.post(
            self.url, {"askingPrice": "12.5", "returnQuery": "listings_page=2&status=live"}
        )
        self.assertEqual(
            response.url,
            reverse("my_listings") + f"?listings_page=2&status=live#listing-{self.flat.publicId}-price",
        )
        rows = self.client.get(reverse("my_listings")).context["rows"]
        self.assertEqual(rows[0]["priceError"]["value"], "12.5")
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.askingPrice, 400000)

    def test_seller_blocks_update_another(self):
        other = makeUser("qwer@test.com", "seller")
        self.client.force_login(other)
        self.client.post(self.url, {"askingPrice": "420000"})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.askingPrice, 400000)

    def test_update_blocks_second_open(self):
        self.flat.status = "review"
        self.flat.askingPrice = 480000
        self.flat.save()
        with patch("sellerpanel.listings.otherOpen", return_value=True):
            response = self.client.post(self.url, {"askingPrice": "440000"}, follow=True)
        self.flat.refresh_from_db()
        self.assertEqual((self.flat.status, self.flat.askingPrice), ("review", 480000))
        self.assertContains(response, "You can sell one flat at a time.")


class SoldTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.buyer = makeUser("qwer@test.com")
        self.flat = makeListing(self.seller)
        self.client.force_login(self.seller)
        self.url = reverse("toggle_sold", args=[self.flat.publicId])

    def test_marking_declines_open_offers(self):
        offer = Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=500000)
        slot = ViewingSlot.objects.create(listing=self.flat, startsAt=timezone.now() + timedelta(days=1))
        booking = ViewingBooking.objects.create(slot=slot, buyer=self.buyer)
        self.client.post(self.url, {"sold": "1"})
        self.flat.refresh_from_db()
        offer.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual((self.flat.status, self.flat.statusBeforeSold), ("sold", "live"))
        self.assertEqual((offer.status, booking.status), ("declined", "cancelled"))

    def test_marking_idempotent_relisting(self):
        self.client.post(self.url, {"sold": "1"})
        self.client.post(self.url, {"sold": "1"})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "sold")
        self.client.post(self.url, {"sold": "0"})
        self.flat.refresh_from_db()
        self.assertEqual((self.flat.status, self.flat.statusBeforeSold), ("live", ""))

    def test_hidden_status_protected_price(self):
        self.flat.status = "hidden"
        sellState(self.flat, "sold")
        self.assertEqual(self.flat.status, "hidden")
        self.flat.status = "sold"
        self.flat.statusBeforeSold = "live"
        sellState(self.flat, "price", flagged=True)
        self.assertEqual((self.flat.status, self.flat.statusBeforeSold), ("sold", "review"))

    def test_marking_notifies_closed_offer(self):
        other = makeUser("soldbuyer@test.com")
        pending = Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=500000)
        countered = Offer.objects.create(
            respondedAt=timezone.now(),
            listing=self.flat,
            buyer=other,
            amount=510000,
            status="countered",
            counterAmount=520000,
        )
        future = ViewingSlot.objects.create(listing=self.flat, startsAt=timezone.now() + timedelta(days=1))
        past = ViewingSlot.objects.create(listing=self.flat, startsAt=timezone.now() - timedelta(days=1))
        for buyer, status in ((self.buyer, "requested"), (other, "confirmed")):
            ViewingBooking.objects.create(slot=future, buyer=buyer, status=status)
        untouched = ViewingBooking.objects.create(slot=past, buyer=self.buyer, status="confirmed")
        self.client.post(self.url, {"sold": "1"})
        self.client.post(self.url, {"sold": "1"})
        self.assertEqual(ActivityNotice.objects.count(), 4)
        for offer in (pending, countered):
            notice = ActivityNotice.objects.get(user=offer.buyer, kind="offer_declined")
            self.assertEqual(notice.url, reverse("my_offers") + f"#offer-{offer.pk}")
            notice = ActivityNotice.objects.get(user=offer.buyer, kind="viewing_cancelled")
            self.assertEqual(notice.url, reverse("my_offers") + f"#slot-{future.pk}")
        untouched.refresh_from_db()
        self.assertEqual(untouched.status, "confirmed")

    def test_listing_blocks_relisted_another(self):
        self.client.post(self.url, {"sold": "1"})
        makeListing(self.seller, block="9")
        response = self.client.get(reverse("my_listings"))
        self.assertNotContains(response, "Relist")
        self.assertNotContains(response, "List another flat")
        self.client.post(self.url, {"sold": "0"})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "sold")

    def test_invalid_values_leave_listing(self):
        offer = Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=500000)
        slot = ViewingSlot.objects.create(listing=self.flat, startsAt=timezone.now() + timedelta(days=1))
        booking = ViewingBooking.objects.create(slot=slot, buyer=self.buyer)
        for data in ({}, {"sold": ""}, {"sold": "yes"}):
            with self.subTest(data=data):
                self.client.post(self.url, data)
                self.flat.refresh_from_db()
                offer.refresh_from_db()
                booking.refresh_from_db()
                self.assertEqual(self.flat.status, "live")
                self.assertEqual((offer.status, booking.status), ("pending", "requested"))
        self.client.post(self.url, {"sold": "1"})
        self.client.post(self.url, {"sold": "invalid"})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "sold")

    def test_flat_relist_blocks_buyer(self):
        accepted = Offer.objects.create(
            respondedAt=timezone.now(),
            listing=self.flat,
            buyer=self.buyer,
            amount=500000,
            status="accepted",
        )
        self.client.post(self.url, {"sold": "1"})
        other = makeListing(makeUser("seller@test.com", "seller"))
        Offer.objects.create(
            respondedAt=timezone.now(),
            listing=other,
            buyer=self.buyer,
            amount=500000,
            status="accepted",
        )
        response = self.client.post(self.url, {"sold": "0"}, follow=True)
        self.flat.refresh_from_db()
        accepted.refresh_from_db()
        self.assertEqual((self.flat.status, self.flat.statusBeforeSold), ("sold", "live"))
        self.assertEqual(accepted.status, "accepted")
        self.assertContains(response, "This buyer has already agreed a price on another flat.")

    def test_flat_relist_allows_buyers(self):
        Offer.objects.create(
            respondedAt=timezone.now(),
            listing=self.flat,
            buyer=self.buyer,
            amount=500000,
            status="accepted",
        )
        self.client.post(self.url, {"sold": "1"})
        other = makeListing(makeUser("seller@test.com", "seller"), status="sold")
        Offer.objects.create(
            respondedAt=timezone.now(),
            listing=other,
            buyer=self.buyer,
            amount=500000,
            status="accepted",
        )
        with (
            patch("sellerpanel.views.lock_user", wraps=lock_user) as lock,
            patch("sellerpanel.listings.lock_user", new=lock),
        ):
            self.client.post(self.url, {"sold": "0"})
        self.assertEqual([call.args[0] for call in lock.call_args_list], [self.buyer.pk, self.seller.pk])
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "live")


class MyListingsTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.admin = makeStaff("admin@test.com")
        self.client.force_login(self.seller)

    def test_status_counts_filters_hold(self):
        makeListing(self.seller, status="sold")
        makeListing(self.seller, status="review", askingPrice=700000)
        response = self.client.get(reverse("my_listings"))
        self.assertEqual(response.context["totalCount"], 2)
        self.assertEqual(response.context["counts"]["review"], 1)
        held = [row for row in response.context["rows"] if row["flat"].status == "review"][0]
        self.assertIn("higher than similar flats", held["holdMessage"])
        self.assertContains(response, "the price is higher than similar flats sold for")
        self.assertContains(
            self.client.get(reverse("seller_listing", args=[held["flat"].publicId])), "0 views"
        )
        self.assertEqual(len(self.client.get(reverse("my_listings"), {"status": "sold"}).context["rows"]), 1)

    def test_rejected_shows_staff_decision(self):
        flat = makeListing(self.seller, status="rejected")
        Decision.objects.create(
            listing=flat,
            staff=self.admin,
            decision="rejected",
            reason="Wrong photos",
            askingPrice=1,
        )
        row = self.client.get(reverse("my_listings")).context["rows"][0]
        self.assertEqual(row["staffNote"], "Wrong photos")

    def test_seller_sees_their_own(self):
        self.client.force_login(makeUser("qwer@test.com", "seller"))
        makeListing(self.seller)
        self.assertEqual(self.client.get(reverse("my_listings")).context["rows"], [])

    def test_hidden_filtered_edited_but(self):
        makeListing(self.seller)
        flat = makeListing(self.seller, block="9", status="hidden")
        response = self.client.get(reverse("my_listings"))
        self.assertEqual((response.context["totalCount"], response.context["counts"]["hidden"]), (2, 1))
        self.assertContains(response, "Taken down")
        page = self.client.get(reverse("seller_listing", args=[flat.publicId]))
        self.assertContains(page, reverse("edit_listing", args=[flat.publicId]))
        self.assertNotContains(page, f"?listing={flat.publicId}")
        rows = self.client.get(reverse("my_listings"), {"status": "hidden"}).context["rows"]
        self.assertEqual([row["flat"] for row in rows], [flat])
        self.assertEqual(self.client.get(reverse("edit_listing", args=[flat.publicId])).status_code, 200)

    def test_saved_count_counts_distinct(self):
        flat = makeListing(self.seller)
        for email in ("qwer@test.com", "zxcv@test.com"):
            buyer = makeUser(email)
            for name in ("Near the MRT", "Shortlist"):
                collection = Collection.objects.create(user=buyer, name=name)
                SavedListing.objects.create(collection=collection, listing=flat)
        response = self.client.get(reverse("my_listings"))
        self.assertEqual(response.context["rows"][0]["flat"].savedCount, 2)
        self.assertContains(
            self.client.get(reverse("seller_listing", args=[flat.publicId])),
            "2 buyers saved this flat",
        )
        self.assertFalse(response.context["canListAnother"])
        flat.status = "sold"
        flat.save()
        response = self.client.get(reverse("my_listings"))
        self.assertTrue(response.context["canListAnother"])
        self.assertContains(response, "List another flat")

    def test_rejected_blocks_reopen_another(self):
        makeListing(self.seller)
        flat = makeListing(self.seller, block="9", status="rejected")
        response = self.client.post(reverse("edit_listing", args=[flat.publicId]), {})
        self.assertRedirects(response, reverse("my_listings"))
        flat.refresh_from_db()
        self.assertEqual(flat.status, "rejected")


class EditTests(CompletedPhotoQueueMixin, TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.flat = makeListing(self.seller, **flatFields())
        for room in ROOMS:
            photo = ListingPhoto(
                listing=self.flat,
                room=room,
                aiLabel="kitchen",
                aiIrrelevant=0.01,
                aiGenerated=0.01,
                modelVersion=CLEAN["modelVersion"],
            )
            photo.image.save(f"{room}.jpg", jpeg())
        self.client.force_login(self.seller)
        self.url = reverse("edit_listing", args=[self.flat.publicId])
        self.token = self.client.get(self.url).context["draftToken"]
        price = patch("sellerpanel.listings.band", return_value=FAIR)
        price.start()
        self.addCleanup(price.stop)
        self.result = dict(CLEAN)
        checker = patch(
            "sellerpanel.photos.checkPics", side_effect=lambda photos: [self.result] * len(photos)
        )
        checker.start()
        self.addCleanup(checker.stop)

    def post(self, **changes):
        return self.client.post(
            self.url,
            {
                **FLAT,
                "confirm": "1",
                "draftToken": self.token,
                "revision": self.seller.listings.get(pk=self.flat.pk).revision,
                **changes,
            },
        )

    def test_sections_keep_field_photo(self):
        response = self.client.get(self.url)
        for name in response.context["form"].fields:
            with self.subTest(field=name):
                self.assertContains(response, f'name="{name}"', count=1)
        for slot in response.context["photoSlots"]:
            self.assertContains(response, f'name="photo_{slot['key']}"', count=1)
        for anchor in ["sell-fields", "sell-details", "sell-price", "sell-photos", "sell-rules"]:
            self.assertContains(response, f'href="#{anchor}"')
        self.assertContains(response, '<details class="optional-photos">')
        self.assertContains(response, "Save changes")

    def test_form_populates_flat_details(self):
        response = self.client.get(self.url)
        self.assertEqual(response.context["editing"], self.flat)
        self.assertEqual(response.context["form"]["floorAreaSqm"].value(), "67")
        urls = {slot["key"]: slot["url"] for slot in response.context["photoSlots"]}
        self.assertTrue(urls["kitchen"].startswith("/listing/"))
        self.assertEqual(urls["bedroom_3"], "")

    def test_photos_show_saved_thumbnail(self):
        response = self.client.get(self.url)
        html = response.content.decode()
        saved = html.split('data-room="living_room"', 1)[1].split('class="photo-status"', 1)[0]
        missing = html.split('data-room="bedroom_3"', 1)[1].split('class="photo-status"', 1)[0]
        self.assertIn('<small class="slot-saved">Photo saved</small>', saved)
        self.assertIn("Change photo", saved)
        self.assertNotIn("Required", saved)
        self.assertNotIn("hidden", saved.split("<img", 1)[1].split(">", 1)[0])
        self.assertIn('<small class="slot-missing">Required</small>', missing)
        self.assertIn("Add photo", missing)
        self.assertNotIn("Photo saved", missing)

    def test_invalid_form_targets_summary(self):
        response = self.post(askingPrice="", storey="")
        self.assertContains(response, f'action="{self.url}#sell-feedback"')
        self.assertContains(response, 'id="sell-feedback" role="alert" tabindex="-1"')
        for field in response.context["form"]:
            if field.errors:
                self.assertContains(response, f'href="#{field.id_for_label}"')
                self.assertContains(response, f'id="{field.id_for_label}"')

    def test_two_tabs_blocks_overwrite(self):
        revision = self.client.get(self.url).context["form"]["revision"].value()
        self.assertContains(self.client.get(self.url), 'type="hidden" name="revision"')
        self.post(askingPrice="410000", revision=revision)
        response = self.post(askingPrice="420000", revision=revision)
        self.assertContains(response, "This listing was changed in another tab")
        self.flat.refresh_from_db()
        self.assertEqual((self.flat.askingPrice, self.flat.revision), (410000, revision + 1))

    def test_price_shortcut_invalidates_open(self):
        revision = self.client.get(self.url).context["form"]["revision"].value()
        self.client.post(reverse("update_price", args=[self.flat.publicId]), {"askingPrice": "410000"})
        response = self.post(askingPrice="420000", revision=revision)
        self.assertContains(response, "Please reload the page")
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.askingPrice, 410000)

    def test_conflict_checked_taking_seller(self):
        revision = self.flat.revision

        def changeWhileLocking(pk):
            current = self.seller.listings.get(pk=self.flat.pk)
            current.description = "Updated by our team"
            current.save(update_fields=["description"])
            return self.seller

        with patch("sellerpanel.listings.lock_user", side_effect=changeWhileLocking):
            response = self.post(askingPrice="420000", revision=revision)
        self.assertContains(response, "This listing was changed in another tab")
        self.flat.refresh_from_db()
        self.assertEqual((self.flat.description, self.flat.askingPrice), ("Updated by our team", 400000))

    def test_missing_revision_blocks(self):
        response = self.post(revision="")
        self.assertIn("revision", response.context["form"].errors)
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.revision, 0)

    def test_fair_updates_price_storey(self):
        response = self.post(askingPrice="410000", storey="9")
        self.assertEqual(response.url, reverse("my_listings") + f"#listing-{self.flat.publicId}")
        self.flat.refresh_from_db()
        self.assertEqual((self.flat.askingPrice, self.flat.storey, self.flat.status), (410000, 9, "live"))

    def test_retained_stale_photos_hold(self):
        for version in [None, "previous-model"]:
            with self.subTest(version=version):
                self.flat.photos.filter(room="kitchen").update(modelVersion=version, staffChecked=True)
                self.post()
                self.flat.refresh_from_db()
                self.assertEqual(self.flat.status, "review")
                self.assertContains(self.client.get(reverse("my_listings")), "photo checks are out of date")
                self.flat.status = "live"
                self.flat.save()

    def test_seller_form_flat_page(self):
        sentence = "It’s our best estimate, not an official HDB valuation."
        response = self.client.post(
            self.url,
            {
                **FLAT,
                "draftToken": self.token,
                "askingPrice": "480000",
                "revision": self.flat.revision,
            },
        )
        self.assertContains(response, sentence)
        self.assertContains(self.client.get(reverse("listing_detail", args=[self.flat.publicId])), sentence)

    def test_high_price_sends_listing(self):
        self.post(askingPrice="480000")
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")

    def test_replacing_photo_doubtful_image(self):
        old = ListingPhoto.objects.get(listing=self.flat, room="kitchen").image.name
        self.result = {"label": "kitchen", "irrelevant": 0.6, "aiGenerated": 0.01}
        self.post(photo_kitchen=jpeg("red"))
        photo = ListingPhoto.objects.get(listing=self.flat, room="kitchen")
        self.assertNotEqual(photo.image.name, old)
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")

    def test_optional_photo_removed_but(self):
        photo = ListingPhoto(listing=self.flat, room="view")
        photo.image.save("view.jpg", jpeg())
        self.post(removePhoto="view")
        self.assertFalse(ListingPhoto.objects.filter(listing=self.flat, room="view").exists())
        self.post(removePhoto="kitchen")
        self.assertTrue(ListingPhoto.objects.filter(listing=self.flat, room="kitchen").exists())

    def test_editing_rejected_listing(self):
        self.flat.status = "rejected"
        self.flat.save()
        self.post()
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")

    def test_sold_listing_blocks_edited(self):
        self.flat.status = "sold"
        self.flat.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.flat.status = "hidden"
        self.flat.save()
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.post()
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")

    def test_page_shows_document_request(self):
        self.flat.status = "review"
        self.flat.save()
        admin = makeStaff("admin@test.com")
        Decision.objects.create(
            listing=self.flat,
            staff=admin,
            decision="documents",
            reason="Send lease papers",
            askingPrice=1,
        )
        response = self.client.get(self.url)
        self.assertEqual(response.context["staffNote"], "Send lease papers")
        self.assertIn("We're checking this listing", response.context["holdNote"])

    def test_missing_required_photo_must(self):
        ListingPhoto.objects.filter(listing=self.flat, room="kitchen").delete()
        response = self.post()
        self.assertTrue(response.context["photosMissing"])
        self.post(photo_kitchen=jpeg())
        self.assertTrue(ListingPhoto.objects.filter(listing=self.flat, room="kitchen").exists())

    def test_inactive_seller_blocks_save(self):
        self.seller.is_active = False
        self.seller.save()
        self.client.force_login(self.seller)
        self.post(askingPrice="410000")
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.askingPrice, 400000)

    def test_existing_old_intent_date(self):
        old = timezone.localdate() - timedelta(days=400)
        self.flat.intentToSellDate = old
        self.flat.save()
        self.assertEqual(self.client.get(self.url).context["form"]["intentToSellDate"].value(), old)
        self.post(askingPrice="410000", intentToSellDate=old.isoformat())
        self.flat.refresh_from_db()
        self.assertEqual((self.flat.askingPrice, self.flat.intentToSellDate), (410000, old))
        older = (old - timedelta(days=5)).isoformat()
        self.assertIn("intentToSellDate", self.post(intentToSellDate=older).context["form"].errors)
        self.assertIn("intentToSellDate", self.post(intentToSellDate="").context["form"].errors)

    def test_hidden_listing_blocks_reopen(self):
        self.flat.status = "hidden"
        self.flat.save()

        def lockSeller(pk):
            makeListing(self.seller, block="9")
            return self.seller

        with patch("sellerpanel.listings.lock_user", side_effect=lockSeller):
            response = self.post()
        self.assertRedirects(response, reverse("my_listings"))
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "hidden")

    def test_blocks_second_open_flat(self):
        with patch("sellerpanel.listings.otherOpen", return_value=True):
            response = self.post(askingPrice="410000")
        self.assertRedirects(response, reverse("my_listings"))
        self.flat.refresh_from_db()
        self.assertEqual((self.flat.status, self.flat.askingPrice), ("live", 400000))

    def test_rechecks_listing_status_unsold(self):

        def lockSeller(pk):
            self.seller.listings.filter(pk=self.flat.pk).update(status="hidden")
            makeListing(self.seller, block="9")
            return self.seller

        with patch("sellerpanel.listings.lock_user", side_effect=lockSeller):
            response = self.post()
        self.assertRedirects(response, reverse("my_listings"))
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "hidden")


class HeldPriceTests(TestCase):

    def setUp(self):
        self.admin = makeStaff("admin@test.com")
        self.seller = makeUser("asdf@test.com", "seller")
        self.flat = makeListing(self.seller, **{**flatFields(), "askingPrice": 480000, "status": "review"})
        price = patch("sellerpanel.listings.band", return_value=FAIR)
        price.start()
        self.addCleanup(price.stop)
        self.url = reverse("update_price", args=[self.flat.publicId])

    def test_reactivated_sellers_listing(self):
        self.client.force_login(self.admin)
        url = reverse("admin_customer_suspend", args=[self.seller.pk])
        self.client.post(url, {"action": "suspend", "reason": "asdf"})
        self.client.post(url, {"action": "reactivate", "reason": "Account checked"})
        self.client.force_login(self.seller)
        self.client.post(self.url, {"askingPrice": "420000"})
        self.flat.refresh_from_db()
        self.assertEqual((self.flat.askingPrice, self.flat.status), (420000, "review"))

    def test_document_request_keeps_listing(self):
        Decision.objects.create(
            listing=self.flat, staff=self.admin, decision="documents", reason="asdf", askingPrice=1
        )
        self.client.force_login(self.seller)
        self.client.post(self.url, {"askingPrice": "420000"})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")

    def test_doubtful_photo_blocks_suggested(self):
        ListingPhoto.objects.create(
            listing=self.flat,
            room="kitchen",
            image="a.jpg",
            aiIrrelevant=0.9,
            modelVersion=CLEAN["modelVersion"],
        )
        self.client.force_login(self.seller)
        self.assertNotIn("lowerTo", self.client.get(reverse("my_listings")).context["rows"][0])
        self.client.post(self.url, {"askingPrice": "420000"})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")

    def test_staff_approved_photo_not(self):
        ListingPhoto.objects.create(
            listing=self.flat,
            room="kitchen",
            image="a.jpg",
            aiIrrelevant=0.9,
            aiGenerated=0.01,
            modelVersion=CLEAN["modelVersion"],
        )
        self.client.force_login(self.admin)
        self.client.post(
            reverse("admin_decide", args=[self.flat.pk]),
            {"revision": self.flat.revision, "status": self.flat.status, "decision": "approved"},
        )
        self.client.force_login(self.seller)
        self.client.post(self.url, {"askingPrice": "480000"})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "review")
        row = self.client.get(reverse("my_listings")).context["rows"][0]
        self.assertNotIn("photos", row["holdMessage"])
        self.client.post(self.url, {"askingPrice": "420000"})
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "live")


class ReviewUnderOfferTests(TestCase):

    def setUp(self):
        self.seller = makeUser("held-seller@test.com", "seller")
        self.buyer = makeUser("held-buyer@test.com", "buyer")
        self.other = makeUser("held-other@test.com", "buyer")
        self.flat = makeListing(self.seller)
        Offer.objects.create(
            listing=self.flat,
            buyer=self.buyer,
            amount=self.flat.askingPrice,
            status="accepted",
            respondedAt=timezone.now(),
        )
        self.flat.status = "review"
        self.flat.save(update_fields=["status"])

    def test_seller_sees_one_status(self):
        self.client.force_login(self.seller)
        page = self.client.get(reverse("seller_listing", args=[self.flat.publicId])).content.decode()
        head = page.split('class="listing-sum-head"', 1)[1].split("</header>", 1)[0]
        self.assertIn("Being checked", head)
        self.assertNotIn("Under offer", head)
        self.assertIn("Your buyer’s agreed price still stands", head)
        self.assertNotIn("before buyers see it", head)

    def test_agreed_buyer_open_flat(self):
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        self.assertContains(response, "The seller accepted your offer of $")
        self.assertNotContains(response, ".00.")
        self.assertContains(response, "Your agreed price still stands.")
        self.client.force_login(self.other)
        response = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)

    def test_live_flat_shows(self):
        self.flat.status = "live"
        self.flat.save(update_fields=["status"])
        self.client.force_login(self.seller)
        page = self.client.get(reverse("seller_listing", args=[self.flat.publicId])).content.decode()
        head = page.split('class="listing-sum-head"', 1)[1].split("</header>", 1)[0]
        self.assertIn("Under offer", head)

    def test_agreed_buyer_sees_photos(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from tempfile import TemporaryDirectory
        from storefront.models import ListingPhoto

        with TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media):
            ListingPhoto.objects.create(
                listing=self.flat, room="kitchen", image=SimpleUploadedFile("k.jpg", b"jpegdata")
            )
            url = reverse("listing_photo", args=[self.flat.publicId, "kitchen"])
            self.client.force_login(self.buyer)
            self.assertEqual(self.client.get(url).status_code, 200)
            self.client.force_login(self.other)
            self.assertEqual(self.client.get(url).status_code, 404)


class ChangePriceTests(TestCase):

    def setUp(self):
        self.seller = makeUser("price-seller@test.com", "seller")
        self.flat = makeListing(self.seller, askingPrice=550000)
        self.client.force_login(self.seller)
        self.page = reverse("seller_listing", args=[self.flat.publicId])

    def test_flat_page_offers(self):
        page = self.client.get(self.page)
        for check_text_1 in ("Change your price", 'name="askingPrice"', "Check this price"):
            self.assertContains(page, check_text_1)

    def test_checking_shows_verdict_missing(self):
        response = self.client.post(
            reverse("update_price", args=[self.flat.publicId]),
            {"askingPrice": "640000", "preview": "1", "back": "flat"},
        )
        self.assertRedirects(response, self.page + "?try=640000#change-price", fetch_redirect_response=False)
        page = self.client.get(self.page, {"try": "640000"})
        self.assertContains(page, "Your price is on the high side")
        self.assertContains(page, "Above is how $640,000 compares with similar flats.")
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.askingPrice, 550000)

    def test_saving_returns_flat_page(self):
        with self.settings():
            response = self.client.post(
                reverse("update_price", args=[self.flat.publicId]),
                {"askingPrice": "560000", "back": "flat"},
                follow=True,
            )
        self.assertEqual(response.redirect_chain[-1][0], self.page + "#change-price")
        self.assertContains(response, "Your price is saved")
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.askingPrice, 560000)

    def test_bad_shows_notice_on(self):
        response = self.client.post(
            reverse("update_price", args=[self.flat.publicId]),
            {"askingPrice": "12", "back": "flat"},
            follow=True,
        )
        self.assertContains(response, "Please enter a price from $50,000 to $3,000,000, with no cents.")
        self.assertEqual(response.redirect_chain[-1][0], self.page + "#change-price")
