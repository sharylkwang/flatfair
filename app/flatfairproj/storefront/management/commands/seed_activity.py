from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from accounts.verification import setEmailOk
from adminpanel.models import Ticket, TicketMessage
from storefront.interiors import seedPics
from storefront.management.commands.seed_demo import okFlagged, deliver, logOrder, retireDemo
from storefront.models import (
    ACTIVE_BOOKINGS, ActivityNotice, FlatView, Listing,
    Offer, Profile, ViewingSlot, ViewingBooking,
    Collection, OfferMessage, PriceSuggestion, SavedListing,
    SavedSearch, Product, Order, OrderItem,
    Review,
)
from storefront.notices import offer_notes
from storefront.valuation import band, VERSION

REVIEWS = [
    ("demo-plan-2", 5, "Got three viewing requests in the first week.", False),
    (
        "demo-plan-4",
        4,
        "More people looked at the flat. The Sponsored tag could stand out more.",
        False,
    ),
    ("demo-plan-9", 2, "Not many extra views for the price.", False),
    ("demo-history-2", 5, "The host was on time and answered every buyer question.", False),
    ("demo-history-6", 4, "Bright, clean photos. They took four working days, not three.", False),
    ("demo-history-10", 5, "Clear plan. Buyers asked fewer questions about room sizes.", False),
    ("demo-history-14", 1, "Call me at 9123 4567 for a cheaper deal.", True),
]

TICKETS = [
    (
        "seller@flatfair.sg",
        "refund",
        "Viewing concierge",
        "open",
        1,
        "I booked the viewing host by mistake. It has not started yet. Can I get a refund?",
        [],
    ),
    (
        "seller@flatfair.sg",
        "orders",
        "Boost 90 days",
        "open",
        2,
        "I paid for this upgrade but my flat is not near the top of search yet. Can you check?",
        [
            (
                "admin@flatfair.sg",
                "Your package started on the order date, and your flat now shows above free listings in its town. Thanks for waiting.",
            )
        ],
    ),
    (
        "buyer@flatfair.sg",
        "viewings",
        None,
        "open",
        1,
        "The seller cancelled my viewing twice. Is the flat still for sale?",
        [],
    ),
    (
        "seller1@demo.flatfair.sg",
        "listing",
        None,
        "resolved",
        6,
        "How do I change the photo of my kitchen?",
        [
            (
                "admin@flatfair.sg",
                "Open My listings, press Edit on your flat, then upload a new kitchen photo. The photo check runs again.",
            ),
            ("seller1@demo.flatfair.sg", "Done, thank you."),
        ],
    ),
]

DEMO_SELLERS = [f"seller{i}@demo.flatfair.sg" for i in range(1, 15)]
DEMO_BUYERS = [f"buyer{i}@demo.flatfair.sg" for i in range(1, 6)]

ACTIVITY_FLATS = [
    ("BEDOK", "159", "460159", "4 ROOM", 104, 1985, "live", 24, 0.98, "seller11@demo.flatfair.sg"),
    ("BISHAN", "221", "570221", "5 ROOM", 121, 1992, "live", 33, 1.02, "seller12@demo.flatfair.sg"),
    (
        "TAMPINES",
        "319",
        "520319",
        "4 ROOM",
        109,
        1995,
        "review",
        3,
        None,
        "seller13@demo.flatfair.sg",
    ),
    ("YISHUN", "622", "760622", "3 ROOM", 73, 1987, "sold", 120, 1.01, "seller@flatfair.sg"),
    ("BEDOK", "219A", "461219", "3 ROOM", 67, 2010, "review", 1, 0.6, "seller14@demo.flatfair.sg"),
]

OFFER_PLANS = (
    ((".97", "pending"), (".95", "pending"), (".92", "declined"), (".94", "countered")),
    ((".99", "accepted"), (".96", "declined"), (".95", "declined"), (".93", "withdrawn")),
    ((".98", "pending"), (".93", "countered"), (".9", "declined"), (".95", "withdrawn")),
    ((".96", "countered"), (".97", "pending"), (".91", "declined"), (".94", "pending")),
)


def visit_time(date):
    if date.weekday() == 5:
        hour, minute = 10, 30
    elif date.weekday() == 6:
        hour, minute = 11, 0
    else:
        hour, minute = 19, 30

    return timezone.make_aware(datetime.combine(date, time(hour, minute)))


def slot_times(now, count, shift):
    today = timezone.localdate(now)
    times = []

    for counter in range(count):
        times.append(visit_time(today + timedelta(days=1 + shift % 3 + counter * 4)))

    return times


def free_time(listing, starts):
    while ViewingSlot.objects.filter(listing=listing, startsAt=starts).exists():
        starts += timedelta(minutes=30)

    return starts


def seed_slot(key, listing, starts):
    slot = ViewingSlot.objects.filter(seedKey=key).first()

    if slot is None:
        slot = ViewingSlot.objects.create(
            listing=listing, startsAt=free_time(listing, starts), seedKey=key
        )

    return slot


def seed_visit(slot, person, status):
    booking = ViewingBooking.objects.filter(slot=slot, buyer=person).first()

    if booking:
        return booking

    if (
        status in ACTIVE_BOOKINGS
        and slot.bookings.filter(status__in=ACTIVE_BOOKINGS).count() >= slot.capacity
    ):
        return None

    return ViewingBooking.objects.create(slot=slot, buyer=person, status=status)


def round_cost(value, step):
    return int((value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) * step


def seed_offer(key, listing, buyer, factor, status, when):
    notes = (
        "Keen to view this weekend, flexible on timeline.",
        "First-timer couple, HFE approved.",
        "Can we discuss including the aircon?",
        "Moving from Punggol, need key collection after March.",
        "We like the layout. Is an evening viewing possible?",
        "Buying near my parents. Happy to discuss the handover date.",
        "HFE ready, hoping to move before the new school term.",
        "Could we view on Saturday morning? Our timeline is flexible.",
    )
    offer = Offer.objects.filter(seedKey=key).first()

    if offer:
        return offer

    if status == "accepted" and Offer.objects.filter(listing=listing, status="accepted").exists():
        status = "declined"

    ask = Decimal(listing.askingPrice)
    step = 500 if ask < 400000 else 1000
    amount = round_cost(ask * Decimal(factor), step)
    counter = None

    if status == "countered":
        counter = max(amount + step, round_cost(ask * Decimal(".985"), step))

    replied = None

    if status != "pending":
        replied = min(timezone.now(), when + timedelta(hours=20))

    return Offer.objects.create(
        seedKey=key,
        listing=listing,
        buyer=buyer,
        amount=amount,
        counterAmount=counter,
        status=status,
        message=notes[(listing.pk + buyer.pk) % len(notes)],
        createdAt=when,
        respondedAt=replied,
    )


class Command(BaseCommand):
    help = "Add repeatable demo offers, viewing bookings, collections and past orders."

    @transaction.atomic
    def handle(self, *args, **options):
        retireDemo()
        now = timezone.now()
        demoUsers = User.objects.filter(username__iendswith="@flatfair.sg") | User.objects.filter(
            username__iendswith="@demo.flatfair.sg"
        )
        Profile.objects.filter(user__in=demoUsers, user__is_staff=False, canSell=True).update(
            canBuy=True
        )

        for user in demoUsers:
            setEmailOk(user)

        seller = User.objects.filter(username="seller@flatfair.sg").first()
        buyer = User.objects.filter(username="buyer@flatfair.sg").first()
        demos = list(User.objects.filter(username__in=DEMO_SELLERS).order_by("pk"))
        buyers = list(User.objects.filter(username__in=DEMO_BUYERS).order_by("pk"))

        if seller:
            self.seed_seller(seller, now)

        if buyers:
            self.seed_neighbours(seller, demos, buyers, now)

        if buyer:
            self.seed_buyer(buyer, now)

        self.seedFlatViews(([buyer] if buyer else []) + buyers, now)
        self.close_taken_offers(now)
        okFlagged(User.objects.filter(username="admin@flatfair.sg").first())
        self.seed_reviews(now)
        self.seed_notices(seller, buyer)
        self.seed_support(now)
        self.seed_trust(now)

        if buyer:
            self.seedOfferThread(buyer, now)
            self.seedSavedAlert(buyer)

        self.seedPriceCoach(now)
        call_command("seed_forum", stdout=self.stdout)
        self.stdout.write(self.style.SUCCESS("Demo activity ready."))

    def seedFlatViews(self, buyers, now):
        for buyer in buyers:
            engaged = set(
                SavedListing.objects.filter(collection__user=buyer).values_list(
                    "listing_id", flat=True
                )
            )
            engaged.update(Offer.objects.filter(buyer=buyer).values_list("listing_id", flat=True))
            engaged.update(
                ViewingBooking.objects.filter(buyer=buyer).values_list(
                    "slot__listing_id", flat=True
                )
            )

            for index, flatId in enumerate(sorted(engaged)):
                FlatView.objects.get_or_create(
                    buyer=buyer,
                    listing_id=flatId,
                    defaults={
                        "count": 2 + index % 2,
                        "firstViewedAt": now - timedelta(days=8 + index % 5),
                        "lastViewedAt": now - timedelta(days=2 + index % 4),
                    },
                )

    def seed_trust(self, now):
        for flat in Listing.objects.filter(intentToSellDate=None).order_by("pk"):
            signed = (flat.createdAt - timedelta(days=7 + flat.pk % 14)).date()
            oldest = (now - timedelta(days=360)).date()

            if signed < oldest:
                signed = oldest

            age = (now - flat.createdAt).days
            views = min(400, 30 + age * 4 + flat.pk * 37 % 90)
            Listing.objects.filter(pk=flat.pk).update(intentToSellDate=signed, viewCount=views)

        Offer.objects.filter(status="accepted").update(hasHfe=True)

        for offer in Offer.objects.filter(status__in=["pending", "countered"]).order_by("pk"):
            if offer.pk % 3 != 0:
                Offer.objects.filter(pk=offer.pk).update(hasHfe=True)

    # Add sample seller activity
    def seed_seller(self, seller, now):
        for i, (town, block, postal, kind, area, lease, status, age, ratio, email) in enumerate(
            ACTIVITY_FLATS
        ):
            owner = User.objects.filter(username=email).first()

            if owner is None:
                continue

            token = f"activity-listing-{i}"
            flat = Listing.objects.filter(publicationToken=token).first()

            if flat is not None and flat.seller_id != owner.pk:
                flat.seller = owner
                flat.save(update_fields=["seller"])

            if flat is None:
                low_cost, fair_cost, top_cost = band(
                    town, kind, "Model A", area, 8, lease, postal=postal, block=block
                )

                if ratio:
                    ask = int(round(fair_cost * ratio, -3))
                else:
                    ask = int(round(top_cost * 1.05, -3))

                flat = Listing.objects.create(
                    publicationToken=token,
                    seller=owner,
                    town=town,
                    block=block,
                    postalCode=postal,
                    flatType=kind,
                    floorAreaSqm=area,
                    leaseCommenceYear=lease,
                    bedrooms=2 if kind == "3 ROOM" else 3,
                    bathrooms=2,
                    hasBalcony=False,
                    askingPrice=ask,
                    estimatedPrice=fair_cost,
                    bandLow=low_cost,
                    bandHigh=top_cost,
                    modelVersion=VERSION,
                    status=status,
                    statusBeforeSold="live" if status == "sold" else "",
                    createdAt=now - timedelta(days=age),
                    reviewSince=now - timedelta(days=age) if status == "review" else None,
                )

            seedPics(flat)

            if status == "sold":
                buyer = User.objects.filter(username="buyer6@demo.flatfair.sg").first()

                if buyer:
                    seed_offer(
                        f"activity-sold:{flat.pk}",
                        flat,
                        buyer,
                        ".99",
                        "accepted",
                        flat.createdAt + timedelta(days=(40)),
                    )

        staff = User.objects.filter(username="admin@flatfair.sg").first()

        for i, (postal, name, days, status) in enumerate(
            (
                (
                    ("760622", "Pro photography", 2, "completed"),
                    ("760622", "Boost 30 days", 3, "completed"),
                    ("760622", "Viewing concierge", 9, "refunded"),
                    ("541326", "Floor plan", 1, "completed"),
                    ("541326", "Boost 90 days", 2, "completed"),
                    ("541326", "Viewing concierge", 11, "paid"),
                )
            )
        ):
            listing = Listing.objects.filter(seller=seller, postalCode=postal).first()
            product = Product.objects.filter(name=name).first()

            if listing is None or product is None:
                continue

            token = f"activity-order-{seller.pk}-{i}"

            if Order.objects.filter(checkoutToken=token).exists():
                for item in OrderItem.objects.filter(order__checkoutToken=token):
                    deliver(item)

                logOrder(Order.objects.get(checkoutToken=token), staff)

                continue

            subtotal = Decimal(product.price)
            gst = (subtotal * Decimal(".09")).quantize(Decimal(".01"))

            order = Order.objects.create(
                checkoutToken=token,
                seller=seller,
                status=status,
                subtotal=subtotal,
                gst=gst,
                total=subtotal + gst,
                createdAt=min(now - timedelta(hours=6), listing.createdAt + timedelta(days=days)),
            )

            item = OrderItem.objects.create(
                order=order,
                product=product,
                price=product.price,
                name=product.name,
                kind=product.kind,
                tier=product.tier,
                durationDays=product.durationDays,
                listing=listing,
            )

            deliver(item)
            logOrder(order, staff)

    def seed_neighbours(self, seller, demos, buyers, now):
        sellers = list(demos)

        if seller:
            sellers.append(seller)

        for flat in Listing.objects.filter(seller__in=sellers, status="live").order_by("pk"):
            own = seller is not None and flat.seller_id == seller.pk
            start = flat.pk % len(buyers)
            pool = buyers[start:] + buyers[:start]
            plan = OFFER_PLANS[flat.pk % len(OFFER_PLANS)]
            seen = None
            latest = now

            if plan[0][1] == "accepted":
                seen = visit_time(timezone.localdate(now) - timedelta(days=4))
                latest = seen + timedelta(days=1)

            for i, person in enumerate(pool[: 4 if own else 2]):
                factor, status = plan[i]
                seed_offer(
                    f"activity:{flat.pk}:{person.pk}",
                    flat,
                    person,
                    factor,
                    status,
                    latest - timedelta(days=i),
                )

            for person in pool[: 3 if own else flat.pk % 4]:
                faves, _ = Collection.objects.get_or_create(user=person, name="Shortlist")
                SavedListing.objects.get_or_create(collection=faves, listing=flat)

            soon = slot_times(now, 4, flat.pk)

            for i in range(6 if own else 3):
                if i == 0 and seen:
                    starts = seen
                elif i < 4:
                    starts = soon[i]
                else:
                    starts = visit_time(timezone.localdate(now) - timedelta(days=i))

                slot = seed_slot(f"activity-slot:{flat.pk}:{i}", flat, starts)

                for j in range(2 if own and i == 0 else 1):
                    person = pool[(i + j) % len(pool)]

                    if slot.startsAt < now:
                        status = "done"
                    elif j == 0:
                        status = "confirmed"
                    else:
                        status = "requested"

                    seed_visit(slot, person, status)

    def close_taken_offers(self, now):
        agreed = Offer.objects.filter(status="accepted").values("listing")
        Offer.objects.filter(listing__in=agreed, status__in=["pending", "countered"]).update(
            status="declined", counterAmount=None, respondedAt=now
        )

    def seed_buyer(self, buyer, now):
        if not buyer.savedSearches.exists():
            SavedSearch.objects.create(
                buyer=buyer,
                queryParams={"town": "TAMPINES", "flatType": "4 ROOM", "maxPrice": "600000"},
            )

        public = list(
            Listing.objects.public()
            .filter(seller__username__in=["seller@flatfair.sg"] + DEMO_SELLERS)
            .exclude(status="sold")
            .order_by("pk")[:8]
        )

        if not public:
            return

        for i, (factor, status, days) in enumerate(
            (
                (
                    (".97", "pending", 1),
                    (".95", "countered", 2),
                    (".93", "declined", 2),
                    (".95", "withdrawn", 6),
                )
            )
        ):
            seed_offer(
                f"buyer:{buyer.pk}:{i}",
                public[i % len(public)],
                buyer,
                factor,
                status,
                now - timedelta(days=days),
            )

        soon = slot_times(now, 2, buyer.pk + 1)

        for i in range(3):
            if i < 2:
                starts = soon[i]
            else:
                starts = visit_time(timezone.localdate(now) - timedelta(days=3))

            slot = seed_slot(f"buyer-slot:{buyer.pk}:{i}", public[i % len(public)], starts)
            status = "done" if slot.startsAt < now else "confirmed"
            seed_visit(slot, buyer, status)

        for i, name in enumerate(("Near the MRT", "Room to grow")):
            collection, _ = Collection.objects.get_or_create(user=buyer, name=name)

            for flat in public[i : i + 4]:
                SavedListing.objects.get_or_create(collection=collection, listing=flat)

    def seedOfferThread(self, buyer, now):
        offer = Offer.objects.filter(seedKey=f"buyer:{buyer.pk}:0", status="pending").first()

        if offer is None or offer.messages.exists():
            return

        OfferMessage.objects.create(
            offer=offer,
            by="seller",
            text="Thanks for your offer. Are you flexible on when you move in?",
            createdAt=now - timedelta(hours=20),
        )

        OfferMessage.objects.create(
            offer=offer,
            by="buyer",
            text="Yes, we can wait up to three months for the keys.",
            createdAt=now - timedelta(hours=18),
        )

    def seedSavedAlert(self, buyer):
        if ActivityNotice.objects.filter(user=buyer, kind="saved_price_drop").exists():
            return

        saved = (
            Listing.objects.filter(status="live", savedBy__collection__user=buyer)
            .exclude(
                pk__in=Offer.objects.filter(
                    buyer=buyer, status__in=["pending", "countered", "accepted"]
                ).values("listing")
            )
            .distinct()
            .order_by("pk")
        )

        for flat in saved:
            savers = User.objects.filter(collections__items__listing=flat).values_list(
                "username", flat=True
            )

            if all(name.endswith("flatfair.sg") for name in savers):
                flat.askingPrice = int(round_cost(Decimal(flat.askingPrice) * Decimal(".98"), 1000))
                flat.save(update_fields=["askingPrice"])

                return

    def seedPriceCoach(self, now):
        from storefront.offeradvice import WINDOW_DAYS, coachSlow
        from storefront.offeradvice import round1000

        if PriceSuggestion.objects.filter(listing__seller__username__in=DEMO_SELLERS).exists():
            return

        agreed = Offer.objects.filter(status="accepted").values("listing")

        flat = (
            Listing.objects.filter(
                seller__username__in=DEMO_SELLERS, status="live", estimatedPrice__isnull=False
            )
            .exclude(pk__in=agreed)
            .exclude(offers__buyer__username="buyer@flatfair.sg")
            .order_by("-pk")
            .first()
        )

        if flat is None:
            return

        quiet = now - timedelta(days=WINDOW_DAYS + 6)

        Listing.objects.filter(pk=flat.pk).update(
            createdAt=now - timedelta(days=45),
            askingPrice=max(flat.askingPrice, round1000(flat.estimatedPrice * 1.06)),
        )

        Offer.objects.filter(listing=flat, createdAt__gt=quiet).update(createdAt=quiet)
        FlatView.objects.filter(listing=flat, lastViewedAt__gt=quiet).update(lastViewedAt=quiet)
        SavedListing.objects.filter(listing=flat, createdAt__gt=quiet).update(createdAt=quiet)
        User.objects.filter(pk=flat.seller_id).update(last_login=now)
        coachSlow(now)

    def seed_reviews(self, now):
        for token, rating, comment, hidden in REVIEWS:
            item = OrderItem.objects.filter(
                order__checkoutToken=token, order__status="completed"
            ).first()

            if item is None:
                continue

            Review.objects.get_or_create(
                orderItem=item,
                defaults={
                    "seller": item.order.seller,
                    "rating": rating,
                    "comment": comment,
                    "hidden": hidden,
                    "createdAt": min(item.order.createdAt + timedelta(days=8), now),
                },
            )

    def seed_notices(self, seller, buyer):
        if (
            seller is None
            or buyer is None
            or ActivityNotice.objects.filter(user__in=[seller, buyer]).exists()
        ):
            return

        offers = Offer.objects.select_related("listing").order_by("-createdAt")
        offer_notes(
            offers.filter(listing__seller=seller, status="pending")[:2], "offer_new", "seller"
        )
        offer_notes(offers.filter(buyer=buyer, status="countered")[:1], "offer_countered", "buyer")
        offer_notes(offers.filter(buyer=buyer, status="declined")[:1], "offer_declined", "buyer")

    def seed_support(self, now):
        staff = User.objects.filter(username="admin@flatfair.sg").first()

        for email, topic, product, status, days, message, replies in TICKETS:
            customer = User.objects.filter(username=email).first()

            if (
                customer is None
                or Ticket.objects.filter(customer=customer, message=message).exists()
            ):
                continue

            opened = now - timedelta(days=days)
            order = None

            if product:
                order = (
                    Order.objects.filter(seller=customer, items__name=product)
                    .order_by("-createdAt", "-pk")
                    .first()
                )

            if order:
                opened = order.createdAt + timedelta(hours=3)

            closed = None

            if status == "resolved":
                closed = opened + timedelta(hours=6)

            ticket = Ticket.objects.create(
                customer=customer,
                email=customer.email,
                topic=topic,
                message=message,
                status=status,
                order=order,
                assignee=staff if status == "resolved" else None,
                createdAt=opened,
                resolvedAt=closed,
                customerSeenAt=closed,
            )

            for i, (author, body) in enumerate(replies):
                writer = User.objects.filter(username=author).first()

                TicketMessage.objects.create(
                    ticket=ticket,
                    author=writer,
                    body=body,
                    fromStaff=bool(writer and writer.is_staff),
                    createdAt=opened + timedelta(hours=2 + i * 2),
                )
