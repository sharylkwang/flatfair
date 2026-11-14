from collections import Counter
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
import zipfile

from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.models import Count
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
import numpy as np
import pandas as pd

from accounts.models import EmailCheck
from adminpanel.models import AuditLog, Decision, Ticket
from .helpers import makeListing, makeStaff, makeUser
from sellerpanel.plans import packsNow
from storefront.floorplan import planRooms
from storefront.interiors import demoPhotos, seedPics, townCover
from storefront.management.commands.seed_bulk import fixStatus, batchBands, bulkStatus, sourceRows
from storefront.models import (
    Listing,
    FlatView,
    ListingPhoto,
    Offer,
    Order,
    OrderItem,
    Payment,
    Refund,
    Product,
    Profile,
    Review,
    SavedSearch,
    ViewingBooking,
    ViewingSlot,
    withUsed,
    Collection,
    SavedListing,
)
from storefront.recommend_flats import MAX_CANDIDATES, maybeFlats, recommendedFlats

BAND = (450000, 500000, 550000)


@patch("storefront.management.commands.seed_activity.band", return_value=BAND)
@patch("storefront.management.commands.seed_demo.band", return_value=BAND)
class SeedTests(TestCase):

    def seed(self):
        call_command("seed_demo", stdout=StringIO())
        call_command("seed_activity", stdout=StringIO())

    def test_creates_demo_catalogue_accounts(self, demoBand, activityBand):
        self.seed()
        self.assertEqual(EmailCheck.objects.filter(verifiedAt__isnull=False).count(), User.objects.count())
        self.assertEqual(Product.objects.count(), 6)
        self.assertTrue(User.objects.get(username="admin@flatfair.sg").groups.filter(name="Admin").exists())
        self.assertFalse(User.objects.filter(username="skmt@flatfair.sg").exists())
        self.assertFalse(User.objects.filter(is_superuser=True).exists())
        self.assertEqual(User.objects.filter(is_staff=True).count(), 1)
        self.assertFalse(User.objects.filter(username="support@flatfair.sg").exists())
        self.assertEqual(
            User.objects.get(username="seller1@demo.flatfair.sg").get_full_name(), "Wei Ling Tan"
        )
        self.assertEqual(Listing.objects.count(), 16)
        self.assertTrue(FlatView.objects.filter(buyer__username="buyer@flatfair.sg").exists())
        self.assertEqual(
            FlatView.objects.filter(buyer__username__in=[f"buyer{i}@demo.flatfair.sg" for i in range(1, 6)])
            .values("buyer")
            .distinct()
            .count(),
            5,
        )
        from storefront.recommend_flats import recommendedFlats

        self.assertTrue(recommendedFlats(User.objects.get(username="buyer@flatfair.sg")))
        self.assertEqual(User.objects.filter(profile__role="buyer").count(), 7)
        self.assertEqual(Profile.objects.filter(canSell=True, canBuy=True).count(), 15)
        self.assertFalse(Profile.objects.filter(role="buyer", canSell=True).exists())
        self.assertTrue(ListingPhoto.objects.filter(image__startswith="demo/").exists())
        self.assertEqual(
            SavedSearch.objects.get(buyer__username="buyer@flatfair.sg").name,
            "4-room in Tampines under $600k",
        )

    def test_retire_legacy_demo_staff(self, demoBand, activityBand):
        retired = makeStaff("support@flatfair.sg", "Support")
        entry = AuditLog.objects.create(actor=retired, action="ticket resolved", target="Ticket #old")
        self.seed()
        retired.refresh_from_db()
        self.assertFalse(retired.is_staff)
        self.assertFalse(retired.is_active)
        self.assertEqual(AuditLog.objects.get(pk=entry.pk).actor_id, retired.pk)
        self.assertEqual(User.objects.filter(is_staff=True).count(), 1)
        retired.is_staff = True
        retired.is_active = True
        retired.save()
        call_command("seed_activity", stdout=StringIO())
        retired.refresh_from_db()
        self.assertFalse(retired.is_staff)
        self.assertFalse(retired.is_active)

    def test_commands_enable_buying_existing(self, demoBand, activityBand):
        self.seed()
        outsider = makeUser("outsider@example.com", "seller")
        staff = User.objects.get(username="admin@flatfair.sg")
        Profile.objects.create(user=staff, role="seller", canSell=True)
        for command in ("seed_demo", "seed_activity"):
            with self.subTest(command=command):
                Profile.objects.filter(canSell=True).update(canBuy=False)
                call_command(command, stdout=StringIO())
                self.assertEqual(Profile.objects.filter(canSell=True, canBuy=True).count(), 15)
                self.assertFalse(Profile.objects.get(user=outsider).canBuy)
                self.assertFalse(Profile.objects.get(user=staff).canBuy)
                self.assertFalse(Profile.objects.filter(role="buyer", canSell=True).exists())

    def test_commands_idempotent(self, demoBand, activityBand):
        self.seed()
        counts = [
            Listing.objects.count(),
            Order.objects.count(),
            Payment.objects.count(),
            Refund.objects.count(),
            Offer.objects.count(),
            ViewingBooking.objects.count(),
            SavedSearch.objects.count(),
            FlatView.objects.count(),
        ]
        EmailCheck.objects.update(verifiedAt=None)
        self.seed()
        self.assertFalse(EmailCheck.objects.filter(verifiedAt__isnull=True).exists())
        again = [
            Listing.objects.count(),
            Order.objects.count(),
            Payment.objects.count(),
            Refund.objects.count(),
            Offer.objects.count(),
            ViewingBooking.objects.count(),
            SavedSearch.objects.count(),
            FlatView.objects.count(),
        ]
        self.assertEqual(counts, again)
        self.assertEqual(Order.objects.count(), 26)
        self.assertEqual(Payment.objects.count(), 26)
        self.assertFalse(Order.objects.filter(payment__isnull=True).exists())
        self.assertFalse(Payment.objects.exclude(method="demo").exists())
        self.assertEqual(Refund.objects.count(), Order.objects.filter(status="refunded").count())

    def test_includes_offer_decision_review(self, demoBand, activityBand):
        self.seed()
        self.assertTrue(Offer.objects.filter(status="accepted").exists())
        self.assertTrue(Offer.objects.filter(status="countered").exists())
        for offer in Offer.objects.filter(status="countered"):
            self.assertGreater(offer.counterAmount, offer.amount)
        self.assertTrue(Decision.objects.filter(decision="approved").exists())
        self.assertTrue(Review.objects.filter(hidden=True).exists())
        self.assertEqual(Ticket.objects.count(), 4)

    @override_settings(DEMO_DATA=False)
    def test_demo_requires_data_setting(self, demoBand, activityBand):
        with self.assertRaises(CommandError):
            call_command("seed_demo", stdout=StringIO())
        self.assertFalse(User.objects.exists())

    def test_activity_missing_demo_users(self, demoBand, activityBand):
        demo = User.objects.create_user("seed@demo.flatfair.sg", "seed@demo.flatfair.sg")
        normal = User.objects.create_user("normal@example.com", "normal@example.com")
        call_command("seed_activity", stdout=StringIO())
        self.assertIsNotNone(EmailCheck.objects.get(user=demo).verifiedAt)
        self.assertFalse(EmailCheck.objects.filter(user=normal).exists())
        self.assertFalse(Offer.objects.exists())

    def test_respects_listing_ownership(self, demoBand, activityBand):
        self.seed()
        flats = Listing.objects.filter(seller__username="seller@flatfair.sg")
        self.assertEqual(sorted(flats.values_list("status", flat=True)), ["live", "sold"])
        for user in User.objects.all():
            self.assertLessEqual(user.listings.exclude(status="sold").count(), 1, user.username)
        for slot in withUsed(ViewingSlot.objects.all()):
            self.assertLessEqual(slot.usedCount, slot.capacity)
        plan = OrderItem.objects.get(order__seller__username="seller@flatfair.sg", name="Floor plan")
        self.assertTrue(plan.deliveryFileName.endswith(".pdf"))
        self.assertFalse(
            OrderItem.objects.filter(kind="addon", order__status="completed", deliveryNote="").exists()
        )

    def test_orders_refunds_reviews_follow(self, demoBand, activityBand):
        self.seed()
        self.assertFalse(OrderItem.objects.filter(kind="package").exclude(order__status="completed").exists())
        for ticket in Ticket.objects.exclude(order=None):
            self.assertGreater(ticket.createdAt, ticket.order.createdAt)
        refund = Ticket.objects.get(topic="refund")
        self.assertEqual(refund.order.status, "paid")
        self.assertFalse(refund.order.items.filter(kind="package").exists())
        for review in Review.objects.filter(orderItem__kind="package"):
            self.assertGreaterEqual(review.createdAt - review.orderItem.order.createdAt, timedelta(days=7))
        packages = OrderItem.objects.filter(order__seller__username="seller@flatfair.sg", kind="package")
        self.assertEqual(len(packsNow(Listing.objects.filter(seller__username="seller@flatfair.sg"))), 1)
        self.assertEqual(packages.count(), 2)

    def test_closes_rival_offers_keeps(self, demoBand, activityBand):
        self.seed()
        taken = Offer.objects.filter(status="accepted").values("listing")
        self.assertFalse(
            Offer.objects.filter(listing__in=taken, status__in=["pending", "countered"]).exists()
        )
        self.assertFalse(Offer.objects.filter(buyer__profile__role="seller").exists())
        self.assertFalse(ViewingBooking.objects.filter(buyer__profile__role="seller").exists())
        for flat in Listing.objects.filter(status="sold"):
            self.assertFalse(flat.underOffer)
        for flat in Listing.objects.filter(status="live"):
            buyers = ViewingBooking.objects.filter(slot__listing=flat).values("buyer").distinct().count()
            self.assertGreater(buyers, 1)
        self.assertEqual(Listing.objects.filter(status="review", decisions=None).count(), 3)

    def test_delivers_photography_zip_sized(self, demoBand, activityBand):
        self.seed()
        photos = OrderItem.objects.get(order__seller__username="seller@flatfair.sg", name="Pro photography")
        with zipfile.ZipFile(photos.deliveryFile.open("rb")) as archive:
            names = archive.namelist()
        self.assertEqual(len(names), photos.listing.photos.count() * 2)
        self.assertTrue(all((name.startswith("blk-622-760622-") for name in names)))
        plan = OrderItem.objects.get(order__seller__username="seller@flatfair.sg", name="Floor plan")
        self.assertTrue(plan.deliveryFile.open("rb").read(5).startswith(b"%PDF"))
        flat = plan.listing
        width, depth, rooms = planRooms(flat.flatType, flat.bedrooms, flat.bathrooms, flat.floorAreaSqm)
        names = [room["name"] for room in rooms]
        for name in (
            "Living",
            "Kitchen",
            "Service yard",
            "Household shelter",
            "Master toilet",
            "Bedroom 3",
        ):
            self.assertIn(name, names)
        self.assertAlmostEqual(width * depth, plan.listing.floorAreaSqm)

    def test_keeps_main_buyer_available(self, demoBand, activityBand):
        self.seed()
        self.assertFalse(
            Offer.objects.filter(buyer__username="buyer@flatfair.sg", status="accepted").exists()
        )
        self.assertTrue(
            Offer.objects.filter(buyer__username__endswith="@demo.flatfair.sg", status="accepted").exists()
        )
        for offer in Offer.objects.select_related("listing"):
            step = 500 if offer.listing.askingPrice < 400000 else 1000
            self.assertEqual(offer.amount % step, 0)
            if offer.counterAmount:
                self.assertEqual(offer.counterAmount % step, 0)

    def test_builds_chronological_idempotent(self, demoBand, activityBand):
        self.seed()
        steps = {
            "processing": ["Paid → Processing"],
            "completed": ["Paid → Processing", "Processing → Completed"],
        }
        steps["refunded"] = ["Paid → Refunded"]
        steps["paid"] = []
        orders = Order.objects.filter(items__kind="addon").distinct()
        self.assertEqual(len({order.status for order in orders}), 4)
        for order in orders:
            logs = AuditLog.objects.filter(action="order status", target=f"Order #{order.pk}").order_by(
                "createdAt"
            )
            self.assertEqual([log.detail for log in logs], steps[order.status])
            for log in logs:
                self.assertEqual(log.actor.username, "admin@flatfair.sg")
                self.assertGreater(log.createdAt, order.createdAt)
                self.assertLess(log.createdAt, timezone.now())
        count = AuditLog.objects.filter(action="order status").count()
        self.seed()
        self.assertEqual(AuditLog.objects.filter(action="order status").count(), count)

    def test_accepted_offers_follow(self, demoBand, activityBand):
        self.seed()
        for offer in Offer.objects.filter(status="accepted"):
            bookings = ViewingBooking.objects.filter(slot__listing=offer.listing, buyer=offer.buyer)
            for booking in bookings:
                self.assertEqual(booking.status, "done")
                self.assertLess(booking.slot.startsAt, offer.createdAt)
        accepted = Offer.objects.filter(status="accepted", listing__status="live")
        self.assertTrue(
            ViewingBooking.objects.filter(slot__listing__offers__in=accepted, status="done").exists()
        )

    def test_at_most_one_running(self, demoBand, activityBand):
        self.seed()
        running = packsNow(Listing.objects.all())
        flats = [plan["listing"].pk for plan in running]
        self.assertEqual(len(flats), len(set(flats)))

    def test_shows_offer_thread_price(self, demoBand, activityBand):
        from storefront.models import ActivityNotice, OfferMessage, PriceSuggestion

        with self.captureOnCommitCallbacks(execute=True):
            self.seed()
        buyer = User.objects.get(username="buyer@flatfair.sg")
        self.assertEqual(
            list(OfferMessage.objects.filter(offer__buyer=buyer).values_list("by", flat=True)),
            ["seller", "buyer"],
        )
        coached = PriceSuggestion.objects.get(status="open")
        self.assertTrue(coached.listing.seller.username.endswith("@demo.flatfair.sg"))
        self.assertLess(coached.price, coached.oldPrice)
        self.assertTrue(ActivityNotice.objects.filter(user=buyer, kind="saved_price_drop").exists())
        with self.captureOnCommitCallbacks(execute=True):
            self.seed()
        self.assertEqual(OfferMessage.objects.count(), 2)
        self.assertEqual(PriceSuggestion.objects.count(), 1)
        self.assertEqual(ActivityNotice.objects.filter(kind="saved_price_drop", user=buyer).count(), 1)


class PhotoSeedTests(TestCase):

    def test_replaces_existing_rooms_postal(self):
        postal = sorted(demoPhotos())[0]
        flat = makeListing(makeUser("asdf@test.com", "seller"), postalCode=postal)
        ListingPhoto.objects.create(listing=flat, room="other", image="old.jpg")
        seedPics(flat)
        rooms = set(flat.photos.values_list("room", flat=True))
        self.assertEqual(rooms, set(demoPhotos()[postal]))
        self.assertTrue(flat.photos.filter(image__startswith=f"demo/{flat.publicId}/").exists())

    @patch("storefront.management.commands.seed_demo.picsReady", return_value=False)
    def test_demo_blocks_unavailable_assets(self, ready):
        with self.assertRaises(CommandError):
            call_command("seed_demo", stdout=StringIO())


def fixedBands(flats):
    return [(450000, 500000, 550000)] * len(flats)


@override_settings(DEBUG=True, DEMO_DATA=True)
class BulkTests(TestCase):

    def seed(self, count=60, buyers=30, **options):
        with patch("storefront.management.commands.seed_bulk.batchBands", side_effect=fixedBands):
            call_command("seed_bulk", listings=count, buyers=buyers, stdout=StringIO(), **options)

    def test_requested_counts_verified(self):
        with patch("storefront.search.huntAlerts") as alerts:
            self.seed()
        self.assertEqual(Listing.objects.count(), 60)
        self.assertEqual(User.objects.count(), 90)
        self.assertEqual(EmailCheck.objects.filter(verifiedAt__isnull=False).count(), 90)
        self.assertEqual(Profile.objects.filter(canSell=True, canBuy=True).count(), 60)
        self.assertEqual(FlatView.objects.count(), 9)
        self.assertEqual(SavedListing.objects.count(), 3)
        self.assertEqual(FlatView.objects.values("buyer_id").distinct().count(), 3)
        self.assertFalse(
            Listing.objects.filter(status__in=("live", "review"))
            .values("seller_id")
            .annotate(total=Count("pk"))
            .filter(total__gt=1)
            .exists()
        )
        self.assertEqual(
            User.objects.filter(username="seller1@bulk.flatfair.sg").get().check_password("demo12345678"),
            True,
        )
        self.assertFalse(User.objects.get(username="seller6@bulk.flatfair.sg").has_usable_password())
        self.assertFalse(mail.outbox)
        alerts.assert_not_called()
        for flat in Listing.objects.all():
            self.assertEqual((flat.bandLow, flat.estimatedPrice, flat.bandHigh), (450000, 500000, 550000))
            self.assertTrue(450000 * 0.7 < flat.askingPrice <= 550000 * 1.08 + 1)
            index = int(flat.seedKey.removeprefix("bulk-"))
            self.assertEqual(flat.askingPrice > flat.bandHigh, index % 100 < 8)
            if flat.status == "review":
                self.assertGreater(flat.askingPrice, flat.bandHigh)
                self.assertIn("above the fair range", flat.description)
            self.assertTrue(flat.street)
        self.assertGreater(Listing.objects.exclude(postalCode="").count(), 50)

    def test_idempotent_top_up_clear(self):
        outsider = makeUser("outside@example.com", "seller")
        outside = makeListing(outsider)
        self.seed(30, 20)
        original = list(
            Listing.objects.filter(seedKey__startswith="bulk-")
            .order_by("pk")
            .values("pk", "seedKey", "publicId", "askingPrice", "town", "createdAt")
        )
        self.seed(30, 20)
        self.assertEqual(
            list(Listing.objects.filter(seedKey__startswith="bulk-").order_by("pk").values(*original[0])),
            original,
        )
        self.seed(60, 30)
        self.assertEqual(Listing.objects.count(), 61)
        self.assertEqual(User.objects.count(), 91)
        self.seed(0, 0, clear=True)
        self.assertEqual(list(Listing.objects.values_list("pk", flat=True)), [outside.pk])
        self.assertEqual(list(User.objects.values_list("pk", flat=True)), [outsider.pk])
        self.assertFalse(Collection.objects.exists())
        self.assertFalse(FlatView.objects.exists())

    @override_settings(DEBUG=False, DEMO_DATA=False)
    def test_production_guard(self):
        with self.assertRaises(CommandError):
            self.seed()
        self.assertFalse(User.objects.exists())

    def test_estate_photos_stay_labelled(self):
        self.seed()
        flat = Listing.objects.filter(status="live").first()
        response = self.client.get(reverse("listings"))
        self.assertEqual(response.status_code, 200)
        for check_text_1 in ("storefront/towns/", "Estate photo", "HDB estate, not this flat"):
            self.assertContains(response, check_text_1)
        response = self.client.get(reverse("listing_detail", args=[flat.publicId]))
        self.assertContains(response, "The seller hasn’t added room photos yet")
        self.assertContains(response, townCover(flat.town))
        self.assertContains(self.client.get(reverse("home")), "Estate photo")
        buyer = User.objects.get(username="buyer10@bulk.flatfair.sg")
        self.client.force_login(buyer)
        self.assertContains(self.client.get(reverse("saved")), "Estate photo")
        self.assertTrue(recommendedFlats(buyer))

    def test_browse_query_bound_independent(self):
        self.seed(30)
        self.client.get(reverse("listings"))
        with CaptureQueriesContext(connection) as small:
            self.client.get(reverse("listings"))
        self.seed(60)
        with self.assertNumQueries(len(small)):
            response = self.client.get(reverse("listings"))
        self.assertLessEqual(len(small), 8)
        self.assertEqual(len(response.context["listings"]), 12)
        countQueries = [row["sql"] for row in small if "COUNT(" in row["sql"]]
        self.assertTrue(countQueries)
        self.assertNotIn("GROUP BY", countQueries[0])

    def test_candidate_bound_scoring(self):
        buyer = makeUser("candidate@example.com")
        sellers = User.objects.bulk_create([User(username=f"bounded{index}") for index in range(620)])
        flats = [
            Listing(
                seller=sellers[index],
                town="YISHUN",
                flatType="4 ROOM",
                floorAreaSqm=92,
                bedrooms=3,
                bathrooms=2,
                hasBalcony=False,
                askingPrice=500000,
            )
            for index in range(620)
        ]
        Listing.objects.bulk_create(flats)
        profile = {
            "categories": {"town": Counter({"YISHUN": 1}), "flatType": Counter({"4 ROOM": 1})},
            "means": {},
            "spreads": {},
        }
        candidates = maybeFlats(buyer, profile)
        self.assertEqual(candidates.query.high_mark, MAX_CANDIDATES)
        self.assertEqual(len(candidates), MAX_CANDIDATES)

    def test_town_cover_existing_image(self):
        with patch("storefront.interiors.finders.find", return_value=None):
            self.assertEqual(townCover("KALLANG/WHAMPOA"), "storefront/estate/fallback.jpg")
        with patch("storefront.interiors.finders.find", return_value="/a/cover.jpg"):
            self.assertEqual(townCover("ANG MO KIO"), "storefront/towns/ang-mo-kio.jpg")

    def test_batch_prediction_one_call(self):
        flat = Listing(
            town="YISHUN",
            flatType="4 ROOM",
            flatModel="Model A",
            floorAreaSqm=92,
            storey=8,
            leaseCommenceYear=1995,
        )
        from unittest.mock import Mock

        models = {name: Mock() for name in ("median", "q10", "q90")}
        for name, model in models.items():
            model.predict.return_value = np.full(
                60, np.log({"median": 500000, "q10": 450000, "q90": 550000}[name])
            )
        artifact = {
            "models": models,
            "max_training_month": "2026-01",
            "calibration_offsets": {"lower": 0, "upper": 0},
            "features": ["town", "flat_type", "flat_model", "floor_area_sqm", "storey_mid"],
            "categories": {"town": ["YISHUN"], "flat_type": ["4 ROOM"], "flat_model": ["Model A"]},
        }
        with patch("storefront.management.commands.seed_bulk._load", return_value=artifact):
            self.assertEqual(len(batchBands([flat] * 60)), 60)
        for model in models.values():
            model.predict.assert_called_once()
            self.assertEqual(len(model.predict.call_args.args[0]), 60)

    def test_source_recent_distinct(self):
        allRows = sourceRows(2108)
        rows = allRows[:22000]
        latest = pd.Period(max((row["month"] for row in allRows)), freq="M")
        self.assertGreaterEqual(min((row["month"] for row in allRows)), str(latest - 23))
        volumes = Counter((row["town"] for row in allRows))
        selected = Counter((row["town"] for row in rows))
        for town, count in volumes.items():
            self.assertLessEqual(abs(selected[town] - count / len(allRows) * len(rows)), 2)
        self.assertEqual(len({tuple(row.values()) for row in rows}), 22000)
        self.assertEqual(len({row["town"] for row in rows}), 26)
        self.assertEqual(rows[:60], sourceRows(2108)[:60])

    def test_status_rules_repair_existing(self):
        self.seed(5, 0)
        flats = list(Listing.objects.order_by("pk"))
        prices = (577500, 577501, 314999, 500000, 700000)
        statuses = ("review", "live", "live", "review", "sold")
        identities = [(flat.pk, flat.publicId, flat.createdAt) for flat in flats]
        for flat, price, status in zip(flats, prices, statuses):
            flat.askingPrice = price
            flat.status = status
        Listing.objects.bulk_update(flats, ["askingPrice", "status"])
        output = StringIO()
        with (
            patch(
                "storefront.management.commands.seed_bulk._load",
                side_effect=AssertionError("No model"),
            ),
            patch(
                "storefront.management.commands.seed_bulk.sourceRows",
                side_effect=AssertionError("No reseeding"),
            ),
            patch("storefront.search.huntAlerts") as alerts,
        ):
            call_command("seed_bulk", fix_statuses=True, stdout=output)
        alerts.assert_not_called()
        repaired = list(Listing.objects.order_by("pk"))
        self.assertEqual([flat.status for flat in repaired], ["live", "review", "review", "live", "sold"])
        self.assertEqual([(flat.pk, flat.publicId, flat.createdAt) for flat in repaired], identities)
        self.assertIn("above the fair range", repaired[1].description)
        self.assertIn("possible typo", repaired[2].description)
        self.assertIsNotNone(repaired[1].reviewSince)
        self.assertIsNone(repaired[0].reviewSince)
        self.assertIn("5 existing bulk listings", output.getvalue())
        self.assertContains(self.client.get(reverse("listings")), "Above range")

    def test_price_proportions_honest(self):
        output = StringIO()
        with patch("storefront.management.commands.seed_bulk.batchBands", side_effect=fixedBands):
            call_command("seed_bulk", listings=200, buyers=10, stdout=output)
        flats = Listing.objects.all()
        above = flats.filter(askingPrice__gt=550000)
        self.assertEqual(above.count(), 16)
        self.assertEqual(above.filter(status="live").count(), 8)
        self.assertEqual(above.filter(status="review").count(), 8)
        self.assertEqual(flats.filter(status="sold").count(), 2)
        self.assertFalse(flats.filter(status="live", askingPrice__lt=315000).exists())
        self.assertIn("210 synthetic demo accounts", output.getvalue())
        self.assertIn("asking prices generated from the price model", output.getvalue())
        self.assertIn("marked verified for the demo, 10 with usable passwords", output.getvalue())

    def test_price_status_boundaries_include(self):
        flat = Listing(bandLow=500000, estimatedPrice=550000, bandHigh=600000, status="live", description="")
        for price, status in (
            (630000, "live"),
            (630001, "review"),
            (350000, "live"),
            (349999, "review"),
        ):
            with self.subTest(price=price):
                flat.askingPrice = price
                flat.status = "live"
                self.assertEqual(bulkStatus(flat), status)
        flat.bandHigh = None
        fixStatus(flat)
        self.assertEqual(flat.status, "review")
