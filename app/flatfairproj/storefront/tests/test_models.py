from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from adminpanel.models import Ticket, TicketMessage
from storefront.models import (
    Collection,
    Listing,
    ListingPhoto,
    Offer,
    Order,
    OrderItem,
    Product,
    Review,
    SavedListing,
    ViewingBooking,
    ViewingSlot,
    withUsed,
    counts,
    extraRooms,
    needRooms,
    Payment,
    Refund,
)
from .helpers import makeListing, makeOrder, makeProduct, makeUser
from storefront.listing_rules import OPEN_LISTING_STATUSES
from storefront.tests.helpers import makeListing, makeOrder, makeProduct, makeStaff, makeUser


class ListingTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("asdf@test.com", "seller")
        cls.flat = makeListing(cls.seller)

    def test_converts_price_area_remaining(self):
        self.assertEqual(self.flat.pricePerSqm(), round(550000 / 92))
        self.assertEqual(self.flat.floorAreaSqft(), 990)
        self.flat.leaseCommenceYear = timezone.localdate().year - 10
        self.assertEqual(self.flat.remainingLease(), 89)

    def test_public_require_visible_status(self):
        self.assertTrue(Listing.objects.public().filter(pk=self.flat.pk).exists())
        for status in ["review", "rejected", "hidden"]:
            Listing.objects.filter(pk=self.flat.pk).update(status=status)
            self.assertFalse(Listing.objects.public().filter(pk=self.flat.pk).exists())
        Listing.objects.filter(pk=self.flat.pk).update(status="sold", statusBeforeSold="live")
        self.assertTrue(Listing.objects.public().filter(pk=self.flat.pk).exists())
        self.seller.is_active = False
        self.seller.save()
        self.assertFalse(Listing.objects.public().filter(pk=self.flat.pk).exists())

    def test_photos_follow_room_order(self):
        ListingPhoto.objects.create(listing=self.flat, room="view", image="a.jpg")
        ListingPhoto.objects.create(listing=self.flat, room="kitchen", image="b.jpg")
        ListingPhoto.objects.create(listing=self.flat, room="living_room", image="c.jpg")
        self.assertEqual([p.room for p in self.flat.orderedPhotos()], ["living_room", "kitchen", "view"])
        self.assertEqual(self.flat.coverPhoto().room, "living_room")
        with self.assertRaises(IntegrityError):
            ListingPhoto.objects.create(listing=self.flat, room="kitchen", image="d.jpg")

    def test_under_offer_requires_unsold(self):
        buyer = makeUser("qwer@test.com")
        self.assertFalse(self.flat.underOffer)
        Offer.objects.create(
            respondedAt=timezone.now(),
            listing=self.flat,
            buyer=buyer,
            amount=500000,
            status="accepted",
        )
        self.assertTrue(self.flat.underOffer)
        self.flat.status = "sold"
        self.assertFalse(self.flat.underOffer)
        other = makeUser("zxcv@test.com")
        with self.assertRaises(IntegrityError):
            Offer.objects.create(
                respondedAt=timezone.now(),
                listing=self.flat,
                buyer=other,
                amount=510000,
                status="accepted",
            )


class OfferRuleTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("asdf@test.com", "seller")
        cls.buyer = makeUser("qwer@test.com")
        cls.flat = makeListing(cls.seller)

    def test_amount_requires_whole_dollars(self):
        Offer(listing=self.flat, buyer=self.buyer, amount=Decimal("500000")).full_clean()
        for amount in ["0", "-5", "100000", "3000001", "500000.50"]:
            with self.assertRaises(ValidationError):
                Offer(listing=self.flat, buyer=self.buyer, amount=Decimal(amount)).full_clean()

    def test_seller_blocks_on_own(self):
        with self.assertRaises(ValidationError):
            Offer(listing=self.flat, buyer=self.seller, amount=Decimal("500000")).full_clean()

    def test_counter_amount_between_asking(self):
        offer = Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=500000)
        offer.status = "countered"
        offer.respondedAt = timezone.now()
        offer.counterAmount = Decimal("499000")
        with self.assertRaises(ValidationError):
            offer.full_clean()
        offer.counterAmount = Decimal("560000")
        with self.assertRaises(ValidationError):
            offer.full_clean()
        offer.counterAmount = Decimal("540000")
        offer.full_clean()
        self.assertEqual(offer.askingPercent, 90.9)


class IntegrityTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("asdf@test.com", "seller")
        cls.buyer = makeUser("qwer@test.com")
        cls.flat = makeListing(cls.seller)

    def test_listing_area_asking_price(self):
        for field in ("floorAreaSqm", "askingPrice"):
            for value in (0, -1):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(IntegrityError), transaction.atomic():
                        Listing.objects.filter(pk=self.flat.pk).update(**{field: value})

    def test_product_order_item_prices(self):
        product = makeProduct(price=0)
        order = makeOrder(self.seller, product)
        item = order.items.get()
        for model, pk in ((Product, product.pk), (OrderItem, item.pk)):
            with self.subTest(model=model.__name__):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    model.objects.filter(pk=pk).update(price=-1)

    def test_order_amounts_must_nonnegative(self):
        values = (
            {"subtotal": -1, "gst": 2, "total": 1},
            {"subtotal": 2, "gst": -1, "total": 1},
            {"subtotal": 0, "gst": 0, "total": -1},
        )
        for amounts in values:
            with self.subTest(amounts=amounts):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Order.objects.create(seller=self.seller, **amounts)

    def test_order_total_matches_subtotal(self):
        order = Order.objects.create(
            seller=self.seller, subtotal=Decimal("0.10"), gst=Decimal("0.20"), total=Decimal("0.30")
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Order.objects.filter(pk=order.pk).update(total=Decimal("0.31"))

    def test_offer_amount_must_positive(self):
        for amount in (0, -1):
            with self.subTest(amount=amount):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=amount)

    def test_buyer_one_active_offer(self):
        for first in ("pending", "countered", "accepted"):
            offer = Offer.objects.create(
                listing=self.flat,
                buyer=self.buyer,
                amount=500000,
                status=first,
                counterAmount=520000 if first == "countered" else None,
                respondedAt=timezone.now() if first != "pending" else None,
            )
            for second in ("pending", "countered", "accepted"):
                with self.subTest(first=first, second=second):
                    with self.assertRaises(IntegrityError), transaction.atomic():
                        Offer.objects.create(
                            listing=self.flat,
                            buyer=self.buyer,
                            amount=510000,
                            status=second,
                            counterAmount=520000 if second == "countered" else None,
                            respondedAt=timezone.now() if second != "pending" else None,
                        )
            offer.delete()

    def test_closed_offers_allow_new(self):
        for status in ("declined", "withdrawn"):
            Offer.objects.create(
                listing=self.flat,
                buyer=self.buyer,
                amount=500000,
                status=status,
                respondedAt=timezone.now(),
            )
        Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=510000)
        other = makeUser("other@test.com")
        Offer.objects.create(listing=self.flat, buyer=other, amount=520000)
        otherFlat = makeListing(makeUser("seller@test.com", "seller"))
        Offer.objects.create(listing=otherFlat, buyer=self.buyer, amount=530000)
        self.assertEqual(Offer.objects.count(), 5)

    def test_closed_offer_blocks_updated(self):
        Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=500000)
        closed = Offer.objects.create(
            respondedAt=timezone.now(),
            listing=self.flat,
            buyer=self.buyer,
            amount=510000,
            status="withdrawn",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Offer.objects.filter(pk=closed.pk).update(status="countered", counterAmount=520000)


class ViewingTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.buyer = makeUser("qwer@test.com")
        self.flat = makeListing(self.seller)
        self.slot = ViewingSlot.objects.create(listing=self.flat, startsAt=timezone.now() + timedelta(days=2))

    def test_used_places_exclude_cancelled(self):
        ViewingBooking.objects.create(slot=self.slot, buyer=self.buyer)
        ViewingBooking.objects.create(slot=self.slot, buyer=makeUser("zxcv@test.com"), status="cancelled")
        self.assertEqual(self.slot.used, 1)
        self.assertEqual(self.slot.placesLeft, 1)
        self.assertEqual(withUsed(ViewingSlot.objects.all()).get().usedCount, 1)

    def test_buyer_blocks_book_same(self):
        ViewingBooking.objects.create(slot=self.slot, buyer=self.buyer)
        with self.assertRaises(IntegrityError):
            ViewingBooking.objects.create(slot=self.slot, buyer=self.buyer)

    def test_slots_require_unique_time(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ViewingSlot.objects.create(listing=self.flat, startsAt=self.slot.startsAt)
        with self.assertRaises(IntegrityError):
            ViewingSlot.objects.create(listing=self.flat, startsAt=timezone.now(), capacity=0)

    def test_booking_status_labels_account(self):
        booking = ViewingBooking.objects.create(slot=self.slot, buyer=self.buyer, status="confirmed")
        self.assertEqual(booking.statusLabel, "Confirmed")
        self.slot.startsAt = timezone.now() - timedelta(hours=1)
        self.assertEqual(booking.statusLabel, "Done")
        booking.status = "requested"
        self.assertEqual(booking.statusLabel, "Not confirmed")


class OtherModelTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = makeUser("asdf@test.com", "seller")

    def test_collection_names_unique_per(self):
        Collection.objects.create(user=self.user, name="Near MRT")
        with self.assertRaises(IntegrityError):
            Collection.objects.create(user=self.user, name="Near MRT")

    def test_saved_listing_unique_within(self):
        collection = Collection.objects.create(user=self.user, name="asdf")
        flat = makeListing(self.user)
        SavedListing.objects.create(collection=collection, listing=flat)
        with self.assertRaises(IntegrityError):
            SavedListing.objects.create(collection=collection, listing=flat)

    def test_review_rating_blocks_exceed(self):
        order = makeOrder(self.user, makeProduct("Floor plan", "addon", 40, None), status="completed")
        item = order.items.get()
        with self.assertRaises(IntegrityError):
            Review.objects.create(seller=self.user, orderItem=item, rating=6)

    def test_order_status_index_handles(self):
        self.assertEqual(makeOrder(self.user, makeProduct()).statusIndex(), 0)
        self.assertEqual(makeOrder(self.user, makeProduct(), status="refunded").statusIndex(), -1)

    def test_room_requirements_follow(self):
        have = counts(3, 2, True)
        self.assertEqual(needRooms(have)[:3], ["living_room", "kitchen", "master_bedroom"])
        self.assertIn("bedroom_3", needRooms(have))
        self.assertNotIn("bedroom_4", needRooms(have))
        self.assertIn("master_toilet", needRooms(have))
        self.assertIn("balcony", extraRooms(have))
        self.assertNotIn("balcony", extraRooms(counts(3, 2, False)))

    def test_review_author_name_respects(self):
        order = makeOrder(self.user, makeProduct("Floor plan", "addon", 40, None), status="completed")
        review = Review.objects.create(seller=self.user, orderItem=order.items.get(), rating=4)
        self.user.first_name = "Tan Wei Ming"
        self.assertEqual(review.firstName, "Tan")
        self.user.first_name = "Wei Ming"
        self.user.last_name = "Tan"
        self.assertEqual(review.firstName, "Wei Ming")


class SupportModelTests(TestCase):

    def setUp(self):
        self.user = makeUser("asdf@test.com", "seller", name="  ")
        self.ticket = Ticket.objects.create(
            customer=self.user, email="asdf@test.com", topic="other", message="asdf"
        )

    def test_ticket_tone_tracks_pending(self):
        self.assertEqual(self.ticket.tone(), "st-pending")
        self.ticket.status = "resolved"
        self.assertEqual(self.ticket.tone(), "st-resolved")

    def test_unseen_messages_include_unread(self):
        TicketMessage.objects.create(ticket=self.ticket, body="a", fromStaff=True)
        TicketMessage.objects.create(ticket=self.ticket, body="b")
        self.assertEqual(TicketMessage.objects.unseen().count(), 1)
        self.ticket.customerSeenAt = timezone.now()
        self.ticket.save()
        self.assertEqual(TicketMessage.objects.unseen().count(), 0)

    def test_missing_reviewer_name_photo(self):
        order = makeOrder(self.user, makeProduct("Floor plan", "addon", 40, None), status="completed")
        review = Review.objects.create(seller=self.user, orderItem=order.items.get(), rating=3)
        self.assertEqual(review.firstName, "A seller")
        flat = makeListing(self.user)
        photo = ListingPhoto(listing=flat, room="kitchen")
        self.assertEqual(
            photo.image.field.upload_to(photo, "Photo.JPG"), f"listings/{flat.publicId}/kitchen.jpg"
        )


class IntegrityRuleTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("integrityseller@test.com", "seller")
        cls.staff = makeStaff("integritystaff@test.com")

    def test_refund_requires_exactly_one(self):
        order = makeOrder(self.seller, makeProduct())
        payment = Payment.objects.create(
            seller=self.seller, method="demo", amount=40, checkoutToken="integrity"
        )
        for targets in ({}, {"order": order, "payment": payment}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Refund.objects.create(staff=self.staff, amount=40, **targets)
        Refund.objects.create(staff=self.staff, amount=40, order=order)
        refund = Refund.objects.create(staff=self.staff, amount=40, payment=payment)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Refund.objects.filter(pk=refund.pk).update(order=order)

    def test_one_open_listing_constraint(self):
        constraint = next(
            (rule for rule in Listing._meta.constraints if rule.name == "one_open_listing_per_seller")
        )
        self.assertEqual(set(dict(constraint.condition.children)["status__in"]), set(OPEN_LISTING_STATUSES))
        flat = makeListing(self.seller)
        for status in OPEN_LISTING_STATUSES:
            with self.assertRaises(IntegrityError), transaction.atomic():
                makeListing(self.seller, status=status)
        closed = [makeListing(self.seller, status=status) for status in ("sold", "hidden", "rejected")]
        with self.assertRaises(IntegrityError), transaction.atomic():
            Listing.objects.filter(pk=closed[0].pk).update(status="review")
        flat.status = "sold"
        flat.save()
        closed[0].status = "review"
        closed[0].save()
        makeListing(makeUser("integrityother@test.com", "seller"))
        makeListing(None)
        makeListing(None, status="review")

    def test_countered_offer_requires(self):
        flat = makeListing(self.seller)
        buyer = makeUser("counterbuyer@test.com")
        for amount in (None, Decimal("0"), Decimal("-1")):
            with self.subTest(amount=amount):
                offer = Offer(
                    respondedAt=timezone.now(),
                    listing=flat,
                    buyer=buyer,
                    amount=Decimal("500000"),
                    status="countered",
                    counterAmount=amount,
                )
                with self.assertRaises(ValidationError):
                    offer.full_clean()
                with self.assertRaises(IntegrityError), transaction.atomic():
                    offer.save()
        offer = Offer(
            respondedAt=timezone.now(),
            listing=flat,
            buyer=buyer,
            amount=Decimal("500000"),
            status="countered",
            counterAmount=Decimal("525000"),
        )
        offer.full_clean()
        offer.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Offer.objects.filter(pk=offer.pk).update(counterAmount=None)
        Offer.objects.filter(pk=offer.pk).update(status="pending", counterAmount=None)
