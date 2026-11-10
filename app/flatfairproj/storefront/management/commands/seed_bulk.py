import random
import time
import uuid
from datetime import timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from accounts.models import EmailCheck
from pretrained import TRAINING
from storefront.address import blocks
from storefront.forms import FLAT_TYPES
from storefront.models import Collection, FlatView, Listing, Profile, SavedListing
from storefront.valuation import (
    FLAT_MODELS,
    TOWNS,
    VERSION,
    _distance,
    _load,
    geoPoint,
    guessBand,
    flagReason,
)

BATCH_SIZE = 1000
ROOMS = {
    "1 ROOM": (1, 1),
    "2 ROOM": (1, 1),
    "3 ROOM": (2, 2),
    "4 ROOM": (3, 2),
    "5 ROOM": (3, 2),
    "EXECUTIVE": (3, 2),
    "MULTI-GENERATION": (4, 3),
}

PRICE_HOLD_REASON = "Staff are checking the asking price because it is above the fair range."
LOW_HOLD_REASON = "Staff are checking the asking price for a possible typo."


def bulkStatus(flat):
    if flat.status == "sold":
        return "sold"

    if not all((flat.bandLow, flat.estimatedPrice, flat.bandHigh)):
        return "review"

    flag = flagReason(flat.askingPrice, flat.bandLow, flat.estimatedPrice, flat.bandHigh)

    if flag == "typo" or flat.askingPrice > Decimal(flat.bandHigh) * Decimal("1.05"):
        return "review"

    return "live"


def fixStatus(flat):
    flat.status = bulkStatus(flat)
    flat.reviewSince = (flat.reviewSince or flat.createdAt) if flat.status == "review" else None

    for reason in (PRICE_HOLD_REASON, LOW_HOLD_REASON):
        flat.description = flat.description.replace(" " + reason, "")

    if flat.status == "review" and all((flat.bandLow, flat.estimatedPrice, flat.bandHigh)):
        flag = flagReason(flat.askingPrice, flat.bandLow, flat.estimatedPrice, flat.bandHigh)

        if flag:
            flat.description += " " + (PRICE_HOLD_REASON if flag == "above" else LOW_HOLD_REASON)


def sourceRows(seed):
    frame = pd.read_csv(
        TRAINING / "ResaleflatpricesbasedonregistrationdatefromJan2017onwards.csv",
        dtype={"block": str},
    )
    latest = pd.Period(frame["month"].max(), freq="M")
    frame = frame[frame["month"] >= str(latest - 23)].drop_duplicates().copy()
    frame = frame[
        frame["town"].isin(TOWNS)
        & frame["flat_type"].isin(FLAT_TYPES)
        & frame["flat_model"].isin(FLAT_MODELS)
    ]
    rng = random.Random(seed)
    ordered = []

    for town, group in frame.groupby("town", sort=True):
        rows = group.to_dict("records")
        rng.shuffle(rows)



        ordered.extend(((index + 0.5) / len(rows), town, row) for index, row in enumerate(rows))

    return [row for weight, town, row in sorted(ordered, key=lambda item: (item[0], item[1]))]


def batchBands(flats):
    bundle = _load()
    today = timezone.localdate()
    trend = min(
        pd.Timestamp(today.replace(day=1)), pd.Timestamp(bundle["max_training_month"] + "-01")
    )

    frame = pd.DataFrame(
        {
            "town": [flat.town for flat in flats],
            "flat_type": [flat.flatType for flat in flats],
            "flat_model": [flat.flatModel for flat in flats],
            "floor_area_sqm": [flat.floorAreaSqm for flat in flats],
            "storey_mid": [(flat.storey - 1) // 3 * 3 + 2 for flat in flats],
            "flat_age": [
                (today.year - flat.leaseCommenceYear) + (today.month - 1) / 12 for flat in flats
            ],
            "remaining_lease_months": [
                1188 - (today.year - flat.leaseCommenceYear) * 12 - today.month + 1
                for flat in flats
            ],
            "month_index": (trend.year - 2017) * 12 + trend.month - 1,
            "sale_year": trend.year,
            "sale_month": trend.month,
        }
    )


    geo = bundle.get("geo")

    if geo:
        points = [geoPoint(geo, flat.town, flat.postalCode, flat.block) for flat in flats]
        lat = np.array([point["lat"] for point in points])
        lon = np.array([point["lon"] for point in points])
        stops = np.asarray(geo["stations"])
        gaps = _distance(lat[:, None], lon[:, None], stops[:, 0], stops[:, 1])
        frame["lat"], frame["lon"] = lat, lon
        frame["dist_cbd_km"] = _distance(lat, lon, 1.2840, 103.8514)
        frame["dist_mrt_km"] = gaps.min(axis=1)
        frame["n_mrt_1km"] = (gaps <= 1).sum(axis=1)



    for name, groups in bundle["categories"].items():
        if not frame[name].isin(groups).all():
            raise CommandError(f"Source contains unsupported {name} values.")

        frame[name] = pd.Categorical(frame[name], categories=groups)

    frame = frame[bundle["features"]]
    predictions = {
        name: model.predict(frame, num_threads=2) for name, model in bundle["models"].items()
    }



    return [
        guessBand(
            {name: values[index] for name, values in predictions.items()},
            bundle["calibration_offsets"],
        )
        for index in range(len(flats))
    ]


class Command(BaseCommand):
    help = "Top up synthetic demo accounts and listings with asking prices generated from the price model."

    def add_arguments(self, parser):
        parser.add_argument("--listings", type=int, default=22000)
        parser.add_argument("--buyers", type=int, default=3000)
        parser.add_argument("--seed", type=int, default=2108)
        parser.add_argument("--clear", action="store_true")
        parser.add_argument(
            "--fix-statuses",
            action="store_true",
            help="Correct existing bulk statuses without generating accounts or prices.",
        )

    # Make many sample accounts
    def handle(self, *args, **options):
        if not (settings.DEBUG or settings.DEMO_DATA):
            raise CommandError(
                "Bulk accounts have known passwords. Set DEBUG or DEMO_DATA=true to seed them."
            )

        if options["listings"] < 0 or options["buyers"] < 0:
            raise CommandError("Listing and buyer counts must be nonnegative.")

        if options["fix_statuses"]:
            self.fixStatuses()

            return

        started = time.perf_counter()
        rows = sourceRows(options["seed"]) if options["listings"] else []

        if options["listings"] > len(rows):
            raise CommandError(
                f"Only {len(rows):,} distinct resale rows are available in the latest 24 months."
            )

        if options["clear"]:
            with transaction.atomic():
                Listing.objects.filter(seedKey__startswith="bulk-").delete()
                User.objects.filter(
                    username__regex=r"^(seller|buyer)[0-9]+@bulk\.flatfair\.sg$",
                    email__endswith="@bulk.flatfair.sg",
                ).delete()


            self.stdout.write("Cleared bulk listings and accounts.")


        now = timezone.now()
        self.passwordHash = make_password("demo12345678")
        self.seed = options["seed"]
        self.now = now
        self.accounts("seller", options["listings"])
        self.accounts("buyer", options["buyers"])
        postalIndex = {
            (entry["t"], entry["b"], entry["s"]): postal for postal, entry in blocks().items()
        }
        already = set(
            Listing.objects.filter(seedKey__startswith="bulk-").values_list("seedKey", flat=True)
        )
        created = 0

        for start in range(0, options["listings"], BATCH_SIZE):
            flats = []
            sellerNames = [
                f"seller{index + 1}@bulk.flatfair.sg"
                for index in range(start, min(start + BATCH_SIZE, options["listings"]))
            ]
            sellers = dict(
                User.objects.filter(username__in=sellerNames).values_list("username", "pk")
            )

            for index, row in enumerate(rows[start : start + BATCH_SIZE], start + 1):
                if index > options["listings"]:
                    break

                seedKey = f"bulk-{index}"

                if seedKey in already:
                    continue

                rng = random.Random(f"{self.seed}:listing:{index}")
                lowFloor, highFloor = map(int, row["storey_range"].split(" TO "))
                beds, baths = ROOMS[row["flat_type"]]
                status = "sold" if index % 100 == 99 else "live"
                createdAt = now - timedelta(seconds=rng.randrange(120 * 86400))

                flats.append(
                    Listing(
                        seedKey=seedKey,
                        publicId=uuid.uuid5(uuid.NAMESPACE_URL, f"flatfair:{self.seed}:{seedKey}"),
                        seller_id=sellers[f"seller{index}@bulk.flatfair.sg"],
                        town=row["town"],
                        block=row["block"],
                        street=row["street_name"],
                        postalCode=postalIndex.get(
                            (row["town"], row["block"], row["street_name"]), ""
                        ),
                        flatType=row["flat_type"],
                        flatModel=row["flat_model"],
                        floorAreaSqm=row["floor_area_sqm"],
                        storey=rng.randint(lowFloor, min(highFloor, 50)),
                        leaseCommenceYear=row["lease_commence_date"],
                        bedrooms=beds,
                        bathrooms=baths,
                        hasBalcony=False,
                        askingPrice=1,
                        modelVersion=VERSION,
                        status=status,
                        statusBeforeSold="live" if status == "sold" else "",
                        createdAt=createdAt,
                        reviewSince=createdAt if status == "review" else None,
                        viewCount=rng.randrange(5, 1500),
                        description=f"Blk {row['block']} {row['street_name'].title()}. {row['town'].title()} HDB resale flat.",
                    )
                )

            if flats:
                for flat, (low, worth, high) in zip(flats, batchBands(flats)):
                    rng = random.Random(f"{self.seed}:price:{flat.seedKey}")


                    index = int(flat.seedKey.removeprefix("bulk-"))
                    above = index % 100 < 8
                    ratio = rng.uniform(1.01, 1.05) if index % 100 < 4 else rng.uniform(1.051, 1.08)



                    asking = (
                        round(high * ratio)
                        if above
                        else max(low, min(high, round(worth * rng.uniform(0.95, 1.08))))
                    )
                    flat.bandLow, flat.estimatedPrice, flat.bandHigh = low, worth, high
                    flat.askingPrice = max(round(low * 0.7) + 1, asking)
                    fixStatus(flat)

                with transaction.atomic():
                    Listing.objects.bulk_create(flats, batch_size=BATCH_SIZE)

                created += len(flats)
                self.stdout.write(
                    f"Listings: {start + len(flats):,}/{options['listings']:,} ({time.perf_counter() - started:.1f}s)"
                )


        self.fixStatuses()
        self.activity(options["buyers"])

        for role, count in (("seller", options["listings"]), ("buyer", options["buyers"])):
            for index in range(1, min(5, count) + 1):
                self.stdout.write(f"{role}{index}@bulk.flatfair.sg / demo12345678")

        counts = Listing.objects.filter(seedKey__startswith="bulk-").count()
        sellers = User.objects.filter(username__regex=r"^seller[0-9]+@bulk\.flatfair\.sg$").count()
        buyers = User.objects.filter(username__regex=r"^buyer[0-9]+@bulk\.flatfair\.sg$").count()


        self.stdout.write(
            self.style.SUCCESS(
                f"Bulk ready: {counts:,} listings ({created:,} new) with asking prices generated from the price model; {sellers + buyers:,} synthetic demo accounts ({sellers:,} sellers, {buyers:,} buyers) marked verified for the demo, {min(5, sellers) + min(5, buyers)} with usable passwords, in {time.perf_counter() - started:.2f}s."
            )
        )

    def fixStatuses(self):
        changed = 0
        pending = []
        fields = ["status", "reviewSince", "description"]

        for flat in (
            Listing.objects.filter(seedKey__startswith="bulk-")
            .order_by("pk")
            .iterator(chunk_size=BATCH_SIZE)
        ):
            before = tuple(getattr(flat, field) for field in fields)
            fixStatus(flat)

            if before != tuple(getattr(flat, field) for field in fields):
                pending.append(flat)

            if len(pending) >= BATCH_SIZE:
                Listing.objects.bulk_update(pending, fields, batch_size=BATCH_SIZE)
                changed += len(pending)
                pending = []

        if pending:
            Listing.objects.bulk_update(pending, fields, batch_size=BATCH_SIZE)


            changed += len(pending)

        self.stdout.write(
            f"Corrected statuses and price hold reasons for {changed:,} existing bulk listings."
        )



    def accounts(self, role, total):
        for start in range(0, total, BATCH_SIZE):
            names = [
                f"{role}{index}@bulk.flatfair.sg"
                for index in range(start + 1, min(start + BATCH_SIZE, total) + 1)
            ]
            already = set(
                User.objects.filter(username__in=names).values_list("username", flat=True)
            )
            users = []

            for index, email in enumerate(names, start + 1):
                if email in already:
                    continue

                rng = random.Random(f"{self.seed}:{email}")
                first, last = rng.choice(
                    (
                        (
                            ("Wei Ming", "Tan"),
                            ("Hui Min", "Lim"),
                            ("Jun Hao", "Lee"),
                            ("Mei Ling", "Wong"),
                            ("Siew Ling", "Ng"),
                            ("Marcus", "Goh"),
                            ("Rachel", "Teo"),
                            ("Grace", "Koh"),
                            ("Arjun", "Kumar"),
                            ("Priya", "Nair"),
                            ("Vikram", "Menon"),
                            ("Suresh", "Pillai"),
                            ("Siti", "Noor"),
                            ("Nur Aisyah", "Salleh"),
                            ("Farid", "Rahman"),
                            ("Hafiz", "Osman"),
                            ("Daniel", "Ong"),
                            ("Nurul", "Huda"),
                            ("Michelle", "Chua"),
                            ("Ahmad", "Ismail"),
                        )
                    )
                )

                users.append(
                    User(
                        username=email,
                        email=email,
                        first_name=first,
                        last_name=last,
                        password=self.passwordHash if index <= 5 else "!bulk-unusable",
                        date_joined=self.now - timedelta(days=150),
                    )
                )


            with transaction.atomic():
                User.objects.bulk_create(users, batch_size=BATCH_SIZE)
                users = list(User.objects.filter(username__in=names))
                tastes = set(
                    Profile.objects.filter(user__in=users).values_list("user_id", flat=True)
                )



                sure = set(
                    EmailCheck.objects.filter(user__in=users).values_list("user_id", flat=True)
                )

                Profile.objects.bulk_create(
                    [
                        Profile(user=user, role=role, canSell=role == "seller", canBuy=True)
                        for user in users
                        if user.pk not in tastes
                    ],
                    batch_size=BATCH_SIZE,
                )

                EmailCheck.objects.bulk_create(
                    [
                        EmailCheck(user=user, verifiedAt=self.now)
                        for user in users
                        if user.pk not in sure
                    ],
                    batch_size=BATCH_SIZE,
                )



            self.stdout.write(
                f"{role.title()} accounts: {min(start + BATCH_SIZE, total):,}/{total:,}"
            )

    def activity(self, total):
        flatIds = list(
            Listing.objects.filter(seedKey__startswith="bulk-", status="live")
            .order_by("seedKey")
            .values_list("pk", flat=True)
        )

        if not flatIds:
            return

        for start in range(1, total + 1, BATCH_SIZE):
            names = [
                f"buyer{index}@bulk.flatfair.sg"
                for index in range(start, min(start + BATCH_SIZE, total + 1))
                if index % 10 == 0
            ]
            buyers = list(User.objects.filter(username__in=names))
            views, folders, saves = [], [], []

            for buyer in buyers:
                key = f"bulk-saved-{buyer.username}"
                folders.append(Collection(seedKey=key, user=buyer, name="Flats to explore"))

            with transaction.atomic():
                Collection.objects.bulk_create(
                    folders, batch_size=BATCH_SIZE, ignore_conflicts=True
                )
                folderIds = dict(
                    Collection.objects.filter(
                        user__in=buyers, seedKey__startswith="bulk-"
                    ).values_list("user_id", "pk")
                )

                for buyer in buyers:
                    rng = random.Random(f"{self.seed}:activity:{buyer.username}")
                    chosen = rng.sample(flatIds, min(3, len(flatIds)))

                    for flatId in chosen:
                        when = self.now - timedelta(days=rng.randint(2, 25))


                        views.append(
                            FlatView(
                                buyer=buyer,
                                listing_id=flatId,
                                count=rng.randint(1, 3),
                                firstViewedAt=when,
                                lastViewedAt=when,
                            )
                        )



                    saves.append(
                        SavedListing(collection_id=folderIds[buyer.pk], listing_id=chosen[0])
                    )

                FlatView.objects.bulk_create(views, batch_size=BATCH_SIZE, ignore_conflicts=True)
                SavedListing.objects.bulk_create(
                    saves, batch_size=BATCH_SIZE, ignore_conflicts=True
                )
