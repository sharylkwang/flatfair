import io
import zipfile
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from PIL import Image, ImageDraw, ImageFont
from django.conf import settings
from django.core.files.base import ContentFile
from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from accounts.verification import setEmailOk
from adminpanel.models import AuditLog, Decision
from sellerpanel.photocheck import doubtQ
from storefront.address import get_postal
from storefront.floorplan import drawPlan
from storefront.interiors import picsReady, seedPics
from storefront.models import Listing, Profile, Product, Order, OrderItem, Payment, Refund
from storefront.presentation import TIER_RANKS
from storefront.templatetags.estate_tags import flat_label
from storefront.valuation import band, flagReason, VERSION

CATALOGUE = [
    (
        "Boost 30 days",
        "package",
        49,
        30,
        "Shows higher than free listings for 30 days, with a Sponsored tag.",
    ),
    (
        "Boost 90 days",
        "package",
        149,
        90,
        "Shows higher than Boost 30 days for 90 days, with a Sponsored tag.",
    ),
    (
        "Top spot",
        "package",
        299,
        None,
        "Your flat shows first in search, with a Sponsored tag and a spot on the home page until it sells.",
    ),
    (
        "Pro photography",
        "addon",
        180,
        None,
        "A photographer takes up to 20 edited photos of your flat. Download them from your orders within 3 working days.",
    ),
    (
        "Floor plan",
        "addon",
        40,
        None,
        "A floor plan drawn from your HDB layout. Download the PDF from your orders and share it with buyers.",
    ),
    ("Viewing concierge", "addon", 60, None, "A FlatFair host runs one 2-hour viewing for you."),
]

ACCOUNTS = [
    ("seller@flatfair.sg", "Wei Ming", "Tan", "seller12345678", "seller"),
    ("buyer@flatfair.sg", "Priya Nair", "", "buyer12345678", "buyer"),
    ("admin@flatfair.sg", "Admin Staff", "", "admin12345678", "Admin"),
    ("seller1@demo.flatfair.sg", "Wei Ling", "Tan", "demo12345678", "seller"),
    ("seller2@demo.flatfair.sg", "Arjun", "Kumar", "demo12345678", "seller"),
    ("seller3@demo.flatfair.sg", "Siti", "Noor", "demo12345678", "seller"),
    ("seller4@demo.flatfair.sg", "Marcus", "Lim", "demo12345678", "seller"),
    ("seller5@demo.flatfair.sg", "Hui Min", "Chua", "demo12345678", "seller"),
    ("seller6@demo.flatfair.sg", "Daniel", "Ong", "demo12345678", "seller"),
    ("seller7@demo.flatfair.sg", "Nurul", "Huda", "demo12345678", "seller"),
    ("seller8@demo.flatfair.sg", "Kelvin", "Goh", "demo12345678", "seller"),
    ("seller9@demo.flatfair.sg", "Rachel", "Teo", "demo12345678", "seller"),
    ("seller10@demo.flatfair.sg", "Suresh", "Pillai", "demo12345678", "seller"),
    ("seller11@demo.flatfair.sg", "Mei Ling", "Wong", "demo12345678", "seller"),
    ("seller12@demo.flatfair.sg", "Farid", "Rahman", "demo12345678", "seller"),
    ("seller13@demo.flatfair.sg", "Grace", "Koh", "demo12345678", "seller"),
    ("seller14@demo.flatfair.sg", "Ahmad", "Ismail", "demo12345678", "seller"),
    ("buyer1@demo.flatfair.sg", "Jun Hao", "Lee", "demo12345678", "buyer"),
    ("buyer2@demo.flatfair.sg", "Nur Aisyah", "Salleh", "demo12345678", "buyer"),
    ("buyer3@demo.flatfair.sg", "Vikram", "Menon", "demo12345678", "buyer"),
    ("buyer4@demo.flatfair.sg", "Michelle", "Tan", "demo12345678", "buyer"),
    ("buyer5@demo.flatfair.sg", "Hafiz", "Osman", "demo12345678", "buyer"),
    ("buyer6@demo.flatfair.sg", "Siew Ling", "Ng", "demo12345678", "buyer"),
]

REVIEW_POSTALS = ("560201",)
PLAN_DAYS = {2: 20, 4: 25, 9: 45}

DESCRIPTIONS = {
    "560201": "Corner unit with morning sun. Kitchen redone in 2021. Five minutes to the bus interchange.",
    "460157": "Breezy, with no afternoon sun. New toilets in 2023. Hawker centre and market downstairs.",
    "570442": "High floor with a clear view of Bishan Park. Built-in wardrobes in all bedrooms stay.",
    "680150": "Well kept, original layout. Near Choa Chu Kang MRT and Lot One.",
    "541326": "Near-new flat with a view of the park connector. Aircon in every room. Walk to Sengkang MRT.",
}

PHOTO_REASONS = [
    "Photos match the number of rooms in the listing.",
    "Checked the flagged photos with the seller. They show this flat.",
    "Staff compared the photos with the listing details. No changes needed.",
]

FLATS = [
    ("ANG MO KIO", "201", "560201", "3 ROOM", "New Generation", 67, 4, 1977, 2, 1, 62, 1.0),
    ("BEDOK", "157", "460157", "4 ROOM", "Model A", 104, 10, 1985, 3, 2, 55, 1.02),
    ("BISHAN", "442", "570442", "5 ROOM", "Improved", 121, 18, 1990, 3, 2, 48, 0.98),
    ("BUKIT BATOK", "354", "650354", "EXECUTIVE", "Maisonette", 149, 6, 1987, 4, 3, 41, None),
    ("CHOA CHU KANG", "150", "680150", "4 ROOM", "Model A", 102, 14, 1998, 3, 2, 36, 1.03),
    ("HOUGANG", "665", "530665", "3 ROOM", "Model A", 74, 8, 1988, 2, 2, 30, 0.99),
    ("JURONG EAST", "204", "600204", "5 ROOM", "Improved", 123, 12, 1983, 3, 2, 26, None),
    ("PASIR RIS", "203", "510203", "EXECUTIVE", "Apartment", 146, 9, 1993, 4, 2, 21, 1.04),
    ("TAMPINES", "318", "520318", "4 ROOM", "Model A", 109, 5, 1995, 3, 2, 17, 0.97),
    ("YISHUN", "153", "760153", "3 ROOM", "New Generation", 68, 3, 1986, 2, 1, 50, None),
    ("SENGKANG", "326A", "541326", "5 ROOM", "Premium Apartment", 112, 18, 2015, 3, 2, 12, 1.01),
]


def plan_pdf(flat):
    image = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(image)
    title = ImageFont.load_default(size=40)
    label = ImageFont.load_default(size=24)
    small = ImageFont.load_default(size=18)
    address = get_postal(flat.postalCode)
    street = address["label"] if address else f"Blk {flat.block}"
    draw.text((100, 100), f"{street}, {flat.town.title()}", fill="black", font=title)
    details = f"{flat_label(flat.flatType)} · {flat.flatModel} · {flat.floorAreaSqm:g} sqm · Floor {flat.storey}"


    draw.text((100, 160), details, fill="black", font=label)

    if "Maisonette" in flat.flatModel:
        draw.text(
            (100, 200),
            "Maisonette: the upper and lower floors are shown as one level.",
            fill="#555555",
            font=small,
        )

    plan = drawPlan(flat.flatType, flat.bedrooms, flat.bathrooms, round(flat.floorAreaSqm))
    image.paste(plan.resize((1040, 878)), (100, 260))
    draw.text(
        (100, 1650),
        "Drawn by FlatFair from the flat type and size. Share it with buyers before a viewing.",
        fill="#555555",
        font=small,
    )
    buffer = io.BytesIO()


    image.save(buffer, "PDF", resolution=150)

    return buffer.getvalue()


def photos_zip(flat):
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w") as archive:
        number = 1

        for photo in flat.orderedPhotos():
            room = photo.get_room_display().lower().replace(" ", "-")
            stem = f"blk-{flat.block.lower()}-{flat.postalCode}"

            with photo.image.open("rb") as file:
                data = file.read()

            archive.writestr(f"{stem}-{number:02d}-{room}.jpg", data)
            number += 1
            picture = Image.open(io.BytesIO(data)).convert("RGB")
            width, height = picture.size


            detail = picture.crop((width * 0.15, height * 0.15, width * 0.85, height * 0.85))
            output = io.BytesIO()
            detail.save(output, "JPEG", quality=90)
            archive.writestr(f"{stem}-{number:02d}-{room}-close-up.jpg", output.getvalue())
            number += 1

    return buffer.getvalue()


def deliver(item):
    notes = {
        "Pro photography": "Your edited photos are in the ZIP file. To use them, press Change photos or details on My listings.",
        "Floor plan": "Your floor plan is ready. Share the PDF with buyers before a viewing.",
        "Viewing concierge": "Our host ran the viewing. Three buyers came and one asked for a second visit.",
    }

    if item.deliveryNote or item.kind != "addon" or item.order.status != "completed":
        return

    item.deliveryNote = notes.get(item.name, "Your service is done.")

    if item.listing and item.name == "Floor plan":
        name = f"floor-plan-blk-{item.listing.block.lower()}.pdf"
        item.deliveryFile.save(name, ContentFile(plan_pdf(item.listing)), save=False)

    if item.listing and item.name == "Pro photography":
        name = f"photos-blk-{item.listing.block.lower()}.zip"


        item.deliveryFile.save(name, ContentFile(photos_zip(item.listing)), save=False)

    item.save()


ORDER_STEPS = {
    "processing": [("Paid", "Processing")],
    "completed": [("Paid", "Processing"), ("Processing", "Completed")],
    "refunded": [("Paid", "Refunded")],
}


def logOrder(order, staff):
    if not order.payment_id:
        payment = Payment.objects.create(
            seller=order.seller,
            method="demo",
            amount=order.total,
            status="refunded" if order.status == "refunded" else "paid",
            cardBrand="Visa",
            cardLast4="4242",
            checkoutToken=order.checkoutToken or f"seed-{order.pk}",
            createdAt=order.createdAt,
            paidAt=order.createdAt,
        )

        order.payment = payment
        order.save(update_fields=["payment"])
    else:
        Payment.objects.filter(pk=order.payment_id).update(seller=order.seller)

    if order.status == "refunded" and staff:
        Refund.objects.get_or_create(
            order=order,
            defaults={
                "amount": order.total,
                "status": "succeeded",
                "staff": staff,
                "createdAt": order.createdAt,
            },
        )



    if staff is None or not order.items.filter(kind="addon").exists():
        return

    if AuditLog.objects.filter(action="order status", target=f"Order #{order.pk}").exists():
        return

    gap = min(timedelta(days=1), (timezone.now() - order.createdAt) / 3)
    when = order.createdAt



    for old, new in ORDER_STEPS.get(order.status, []):
        when = when + gap
        AuditLog.objects.create(
            actor=staff,
            action="order status",
            target=f"Order #{order.pk}",
            detail=f"{old} → {new}",
            createdAt=when,
        )


def seedChoice(flat, staff, choice, reason, when, askingPrice):
    Decision.objects.create(
        listing=flat,
        staff=staff,
        decision=choice,
        reason=reason,
        askingPrice=askingPrice,
        estimatedPrice=flat.estimatedPrice,
        bandLow=flat.bandLow,
        bandHigh=flat.bandHigh,
        modelVersion=flat.modelVersion,
        createdAt=when,
    )


    if choice == "approved":
        flat.photos.update(staffChecked=True)

    AuditLog.objects.create(
        actor=staff,
        action=f"listing {choice}",
        target=f"Listing #{flat.pk} (Blk {flat.block} {flat.town.title()})",
        detail=reason,
        createdAt=when,
    )


def okFlagged(staff):
    reasons = [
        "Full renovation in 2024. The seller sent the receipts.",
        "High floor with an open view. Two recent sales in the same block were close to this price.",
        "New kitchen and toilets. The seller sent the receipts.",
        "Next to the MRT station. A recent sale in the next block supports the price.",
    ]



    if staff is None:
        return

    flats = (
        Listing.objects.filter(status="live", decisions=None)
        .annotate(doubtful=Count("photos", filter=doubtQ("photos__")))
        .order_by("createdAt", "pk")
    )


    priced = 0
    photos = 0



    for flat in flats:
        if (
            flagReason(flat.askingPrice, flat.bandLow, flat.estimatedPrice, flat.bandHigh)
            == "above"
        ):
            reason = reasons[priced % len(reasons)]


            if priced < 2:
                denied = "Asking price was far above similar sales with no reason given. Asked the seller to lower it."
                wanted = int(round(flat.askingPrice * 1.08, -3))
                seedChoice(
                    flat, staff, "rejected", denied, flat.createdAt + timedelta(hours=8), wanted
                )

            priced += 1
        elif flat.doubtful:
            reason = PHOTO_REASONS[photos % len(PHOTO_REASONS)]
            photos += 1
        else:
            continue

        seedChoice(
            flat, staff, "approved", reason, flat.createdAt + timedelta(hours=20), flat.askingPrice
        )


def retireDemo():
    retired = User.objects.filter(
        Q(username__iexact="support@flatfair.sg") | Q(email__iexact="support@flatfair.sg")
    )
    retired.update(is_staff=False, is_active=False, is_superuser=False)

    for member in retired:
        member.groups.clear()


class Command(BaseCommand):
    help = (
        "Create demo accounts, listings with interior photos, and past orders. Safe to run again."
    )

    # Add some sample data
    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEMO_DATA:
            raise CommandError(
                "Demo accounts have known passwords. Set DEMO_DATA=true to add them anyway."
            )


        if not picsReady():
            raise CommandError(
                "Interior photos missing from storefront/static/storefront/interiors; no records created."
            )

        goods = []

        for name, kind, price, span, description in CATALOGUE:
            product, _ = Product.objects.get_or_create(
                name=name,
                defaults={
                    "kind": kind,
                    "price": price,
                    "durationDays": span,
                    "description": description,
                    "tier": TIER_RANKS.get(name, 0),
                },
            )

            goods.append(product)

        now = timezone.now()
        users = {}

        for email, first, last, password, role in ACCOUNTS:
            user, created = User.objects.get_or_create(username=email, defaults={"email": email})



            if created:
                user.set_password(password)
                user.date_joined = now - timedelta(days=150)

            user.first_name = first
            user.last_name = last

            if role == "Admin":
                user.is_staff = True
                user.is_superuser = False
                user.groups.add(Group.objects.get_or_create(name=role)[0])
            else:
                profile, _ = Profile.objects.get_or_create(user=user, defaults={"role": role})

                if profile.canSell and not user.is_staff:
                    profile.canBuy = True
                    profile.save(update_fields=["canBuy"])

            user.save()
            setEmailOk(user)
            users[email] = user

        retireDemo()



        self.staff = users["admin@flatfair.sg"]



        flats = []

        for i, (
            town,
            block,
            postal,
            kind,
            model,
            area,
            storey,
            lease,
            beds,
            baths,
            age,
            ratio,
        ) in enumerate(FLATS):
            if i < 10:
                owner = users[f"seller{i + 1}@demo.flatfair.sg"]
            else:
                owner = users["seller@flatfair.sg"]

            flat = Listing.objects.filter(town=town, block=block, postalCode=postal).first()

            if flat is not None and flat.seller_id != owner.pk:
                flat.seller = owner
                flat.save(update_fields=["seller"])

            if flat is not None and not flat.description:
                flat.description = DESCRIPTIONS.get(postal, "")


                flat.save(update_fields=["description"])

            if flat is None:
                low_cost, fair_cost, top_cost = band(
                    town, kind, model, area, storey, lease, postal=postal, block=block
                )

                if ratio:
                    asking = int(round(fair_cost * ratio, -3))
                else:
                    asking = int(round(top_cost * 1.08, -3))

                flat = Listing.objects.create(
                    seller=owner,
                    town=town,
                    block=block,
                    postalCode=postal,
                    flatType=kind,
                    flatModel=model,
                    floorAreaSqm=area,
                    storey=storey,
                    leaseCommenceYear=lease,
                    bedrooms=beds,
                    bathrooms=baths,
                    hasBalcony=False,
                    askingPrice=asking,
                    description=DESCRIPTIONS.get(postal, ""),
                    estimatedPrice=fair_cost,
                    bandLow=low_cost,
                    bandHigh=top_cost,
                    modelVersion=VERSION,
                    status="review" if postal in REVIEW_POSTALS else "live",
                    createdAt=now - timedelta(days=age),
                    reviewSince=now - timedelta(hours=20) if postal in REVIEW_POSTALS else None,
                )

            seedPics(flat)
            flats.append(flat)

        for i in (2, 4, 9):
            days = PLAN_DAYS[i]
            self.order(
                f"demo-plan-{i}", flats[i], goods[i % 3], now - timedelta(days=days), "completed"
            )

        states = ["paid", "processing", "completed", "refunded"]

        for i in range(17):
            flat = flats[i % 10]
            when = flat.createdAt + (now - flat.createdAt) * ((i * 7 % 16 + 1) / 18)
            self.order(f"demo-history-{i}", flat, goods[3 + i % 3], when, states[i % 4])

        okFlagged(users["admin@flatfair.sg"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Demo ready: 14 demo sellers, 6 demo buyers, {len(flats)} listings, 20 orders."
            )
        )

    def order(self, token, flat, product, when, status):
        if Order.objects.filter(checkoutToken=token).exists():
            Order.objects.filter(checkoutToken=token).update(seller=flat.seller)

            for item in OrderItem.objects.filter(order__checkoutToken=token):
                deliver(item)

            logOrder(Order.objects.get(checkoutToken=token), self.staff)

            return

        subtotal = Decimal(product.price)
        gst = (subtotal * Decimal("0.09")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


        order = Order.objects.create(
            checkoutToken=token,
            seller=flat.seller,
            subtotal=subtotal,
            gst=gst,
            total=subtotal + gst,
            createdAt=when,
            status=status,
        )

        item = OrderItem.objects.create(
            order=order,
            product=product,
            listing=flat,
            price=product.price,
            name=product.name,
            kind=product.kind,
            tier=product.tier,
            durationDays=product.durationDays,
        )

        deliver(item)
        logOrder(order, self.staff)
