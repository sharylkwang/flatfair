from statistics import median
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import (
    Case, Exists, When, Value,
    OuterRef, Subquery, prefetch_related_objects, Count,
    Min, Q, Avg, Prefetch,
)
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from sellerpanel.plans import goodPacks, packsNow, cartFlat, planNote, CART_LISTING_STATUSES
from sellerpanel.recommend import showTip
from sellerpanel.stripepay import savings
from vision.templatetags.vision_tags import shop_look
from .forms import FLAT_TYPES
from .models import (
    Offer, picsByRoom, ListingPhoto, Review, hasRole, notAllowed,
    SavedListing, publicQ, Collection, Product, listing_id, Listing,
)
from storefront.address import get_postal, distance, walk_index
from .offeradvice import floorCosts, lastSales
from storefront.templatetags.estate_tags import nearest_mrt

TIER_NAMES = {1: "Boost 30 days", 2: "Boost 90 days", 3: "Top spot"}
TIER_RANKS = {name: rank for rank, name in TIER_NAMES.items()}
BUDGET_BANDS = (
    {"label": "Under $500k", "maxPrice": 499999},
    {"label": "$500k to $700k", "minPrice": 500000, "maxPrice": 699999},
    {"label": "$700k to $1m", "minPrice": 700000, "maxPrice": 999999},
    {"label": "$1m and above", "minPrice": 1000000},
)
RATING_CHOICES = [1, 2, 3, 4, 5]
LOW_RATING = 2


def live_rank():
    lines = goodPacks(timezone.now()).filter(listing=OuterRef("pk")).order_by("-tier")

    return Coalesce(Subquery(lines.values("tier")[:1]), Value(0))


def withTier(listings):
    listings = listings.annotate(rank=live_rank())
    tierWhen = []

    for rank, name in TIER_NAMES.items():
        tierWhen.append(When(rank=rank, then=Value(name)))

    return listings.annotate(tier=Case(*tierWhen, default=Value("")))


def hasDeal():
    return Exists(Offer.objects.filter(listing=OuterRef("pk"), status="accepted"))


def rankFlats(listings):
    listings = withTier(listings).annotate(taken=hasDeal())

    return listings.order_by("-rank", "taken", "-createdAt", "-pk")


def starFirst(listings):
    starred = Case(When(rank=(3), then=Value(1)), default=Value(0))
    listings = withTier(listings).annotate(taken=hasDeal())

    return listings.order_by(starred.desc(), "taken", "-createdAt", "-pk")


def tierOf(rank):
    return TIER_NAMES.get(rank, "")


def decorate(listings):
    listings = list(listings)

    for flat in listings:
        flat.adTag = ""

        if flat.status == "live" and flat.tier:
            flat.adTag = "Sponsored"

        flat.fairPrice = False

        if flat.bandLow is not None and flat.bandHigh is not None:
            flat.fairPrice = flat.bandLow <= flat.askingPrice <= flat.bandHigh

    return listings


def helloFor(user):
    hour = timezone.localtime().hour
    if hour < 12:
        period = "morning"
    elif hour < 18:
        period = "afternoon"
    else:
        period = "evening"

    name = user.get_full_name().strip() or user.username

    return f"Good {period}, {name}"


def alertRow(icon, tone, title, meta, url, action, hero=""):
    return {
        "icon": icon,
        "tone": tone,
        "title": title,
        "meta": meta,
        "url": url,
        "action": action,
        "hero": hero,
    }


def typeShelf(live, shown=()):
    rows = live.filter(flatType__in=FLAT_TYPES).values("flatType")
    shelf = list(rows.annotate(count=Count("pk"), lowest=Min("askingPrice")).order_by("flatType"))
    withPics = Exists(ListingPhoto.objects.filter(listing_id=OuterRef("pk")))

    for row in shelf:
        flats = starFirst(live.filter(flatType=row["flatType"])).prefetch_related(picsByRoom())
        fresh = flats.exclude(pk__in=shown)
        row["flat"] = fresh.filter(withPics).first() or fresh.first() or flats.first()

    return shelf


def budgetShelf(live):
    lookups = {
        "minPrice": "askingPrice__gte",
        "maxPrice": "askingPrice__lte",
    }

    counts = {}

    for index, band in enumerate(BUDGET_BANDS):
        prices = {lookups[field]: band[field] for field in lookups if field in band}
        counts[f"band{index}"] = Count("pk", filter=Q(**prices))

    found = live.aggregate(**counts)
    shelf = []

    for index, band in enumerate(BUDGET_BANDS):
        if found[f"band{index}"]:
            query = urlencode({field: band[field] for field in lookups if field in band})
            shelf.append({"label": band["label"], "count": found[f"band{index}"], "query": query})

    return shelf


def addRatings(products):
    shown = Q(orderItems__review__hidden=False)

    return products.annotate(
        reviewCount=Count("orderItems__review", filter=shown),
        averageRating=Avg("orderItems__review__rating", filter=shown),
    )


def newReviews(limit=6):
    reviews = (
        Review.objects.filter(hidden=False)
        .exclude(comment="")
        .select_related("orderItem__product", "seller")
        .order_by("-createdAt", "-pk")
    )

    return list(reviews[:limit])


def my_folders(user):
    if not hasRole(user, "buyer"):
        return None

    saves = SavedListing.objects.filter(publicQ("listing__")).only("collection_id", "listing_id")

    saved = Prefetch("items", queryset=saves, to_attr="savedItems")
    folders = list(user.collections.order_by("createdAt").prefetch_related(saved))

    for folder in folders:
        folder.listingIds = {item.listing_id for item in folder.savedItems}

    return folders


def save_data(user):
    folders = my_folders(user)

    if folders is None:
        return {"collections": None, "savedIds": None}

    savedIds = {listingId for collection in folders for listingId in collection.listingIds}

    return {"collections": folders, "savedIds": savedIds}


def mainFolder(user):
    default_list = "My shortlist"

    folder = user.collections.filter(name__iexact=default_list).order_by("createdAt", "pk").first()

    if folder is None:
        folder = Collection.objects.create(user=user, name=default_list)

    return folder


def cleanName(raw):
    return " ".join(raw.split())[:40]


def backTo(request, fallback):
    nextUrl = request.POST.get("next", "")

    if url_has_allowed_host_and_scheme(nextUrl, allowed_hosts={request.get_host()}):
        return redirect(nextUrl)

    return redirect(fallback)


def packages(request):
    packQuery = Product.objects.filter(kind="package", isActive=True).order_by("price")
    goods = list(addRatings(packQuery))
    pickFlat = ""
    theFlat = None

    if request.user.is_authenticated:
        try:
            picked = listing_id(request.GET.get("listing"))
            theFlat = Listing.objects.filter(publicId=picked, seller=request.user).first()
            pickFlat = theFlat.publicId if theFlat else ""
        except ValidationError:
            pass

        if not request.GET.get("listing") and hasRole(request.user, "seller"):
            theFlat = cartFlat(request.user, request.session.get("cartListing"))
            pickFlat = theFlat.publicId if theFlat else ""

    running = {}
    planNotes = []

    if theFlat:
        for plan in packsNow([theFlat]):
            planNotes.append(planNote(plan))
            tier = tierOf(plan["tier"])

            if tier and tier not in running:
                running[tier] = plan

    topTier = ""

    for tier in running:
        if TIER_RANKS[tier] > TIER_RANKS.get(topTier, 0):
            topTier = tier

    for product in goods:
        product.packageTier = tierOf(product.tier)
        product.includingGst = savings(0, Decimal(product.price) * Decimal("1.09"))["cost"]
        spareText = {"benefits": [product.description], "place": "With free flats"}
        copy = PACKAGE_COPY.get(product.packageTier, spareText)
        product.benefits = [
            benefit.replace("{duration}", "").strip() for benefit in copy["benefits"]
        ]
        product.place = copy["place"]
        product.runningPlan = running.get(product.packageTier)
        product.coveredBy = ""

        if (
            topTier
            and not product.runningPlan
            and TIER_RANKS.get(product.packageTier, 0) < TIER_RANKS[topTier]
        ):
            product.coveredBy = topTier

    addons = list(addRatings(Product.objects.filter(kind="addon", isActive=True)).order_by("price"))

    extraInfo = {
        "Pro photography": "Up to 20 edited photos of your flat.",
        "Floor plan": "Your HDB layout as a shareable PDF.",
        "Viewing concierge": "A host for one 2-hour viewing.",
    }

    for product in addons:
        product.summary = extraInfo.get(product.name, product.description.split(". ")[0])
        product.includingGst = savings(0, Decimal(product.price) * Decimal("1.09"))["cost"]

    tip = showTip(theFlat) if hasRole(request.user, "seller") else None

    context = {
        "packages": goods,
        "addons": addons,
        "addonPhotos": any(product.image for product in addons),
        "reviews": newReviews(),
        "starRange": RATING_CHOICES,
        "isSeller": hasRole(request.user, "seller"),
        "cartCount": Product.objects.filter(
            pk__in=request.session.get("cart", []), isActive=True
        ).count(),
        "cartIds": request.session.get("cart", []),
        "selectedListing": pickFlat or "",
        "selectedFlat": theFlat,
        "runningNotes": planNotes,
        "runningPlans": sorted(running.values(), key=lambda plan: plan["tier"], reverse=True),
        "packageRecommendation": tip,
        "recommendationReason": (
            tip["reason"].split(". ")[0].split(";")[0].rstrip(".") + "." if tip else ""
        ),
        "listingAction": (
            reverse("sell")
            if hasRole(request.user, "seller")
            else reverse("register") + "?role=seller"
        ),
        "hasFlat": hasRole(request.user, "seller")
        and request.user.listings.filter(status__in=CART_LISTING_STATUSES).exists(),
    }

    return render(request, "storefront/packages.html", context)


@login_required
def saved(request):
    if not hasRole(request.user, "buyer"):
        return notAllowed(request, "buyer")

    folders = my_folders(request.user)
    current = None

    if folders:
        current = folders[0]

        for folder in folders:
            if str(folder.pk) == request.GET.get("c"):
                current = folder

                break

    homes = []

    if current:
        homes = (
            Listing.objects.public()
            .filter(savedBy__collection=current)
            .annotate(
                acceptedCount=Count("offers", filter=Q(offers__status="accepted"), distinct=True)
            )
            .prefetch_related(picsByRoom())
            .order_by("-savedBy__createdAt")
        )

        homes = decorate(withTier(homes))

    context = {
        "current": current,
        "savedSearches": request.user.savedSearches.order_by("createdAt", "pk"),
        "listings": homes,
        "activeSaved": [flat for flat in homes if flat.status != "sold"],
        "soldSaved": [flat for flat in homes if flat.status == "sold"],
        **save_data(request.user),
    }

    return render(request, "storefront/saved.html", context)


def saveMenu(request, flat):
    goTo = request.POST.get("next", "") if request.method == "POST" else request.GET.get("next", "")
    goTo = goTo.split("#", 1)[0]

    if not url_has_allowed_host_and_scheme(
        goTo, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        goTo = reverse("listing_detail", args=[flat.publicId])

    scope = (
        request.POST.get("scope", "") if request.method == "POST" else request.GET.get("scope", "")
    )

    context = {
        "flat": flat,
        "big": (request.POST.get("big") if request.method == "POST" else request.GET.get("big"))
        == "1",
        "saveScope": scope if scope.replace("-", "").isalnum() and len(scope) <= 20 else "",
        "saveNext": goTo,
        "open": True,
        **save_data(request.user),
    }

    return render_to_string("storefront/save_menu.html", context, request=request)


def saveAnswer(request, flat, text, failed=False):
    if request.headers.get("Accept") == "application/json":
        saved = SavedListing.objects.filter(collection__user=request.user, listing=flat).exists()
        count = (
            Listing.objects.public()
            .filter(savedBy__collection__user=request.user)
            .distinct()
            .count()
        )

        return JsonResponse(
            {
                "saved": saved,
                "count": count,
                "message": text,
                "failed": failed,
                "menu": saveMenu(request, flat),
            }
        )

    if failed:
        messages.error(request, text)
    else:
        messages.success(request, text)

    return backTo(request, "saved")


@login_required
@transaction.atomic
def toggle_save(request, publicId):
    flat = get_object_or_404(Listing, publicId=publicId)
    User.objects.select_for_update().get(pk=request.user.pk)

    if not hasRole(request.user, "buyer"):
        return notAllowed(request, "buyer")

    if request.method != "POST":
        if request.headers.get("Accept") == "application/json":
            return JsonResponse({"menu": saveMenu(request, flat)})

        return redirect("listing_detail", publicId=publicId)

    if flat.seller_id == request.user.pk:
        return saveAnswer(request, flat, "You can’t save your own flat.", True)

    if request.POST.get("remove") == "1":
        SavedListing.objects.filter(collection__user=request.user, listing=flat).delete()

        return saveAnswer(request, flat, "Removed from your saved flats.")

    newName = cleanName(request.POST.get("newName", ""))

    if "collection" not in request.POST and "newName" not in request.POST:
        saved = SavedListing.objects.filter(collection__user=request.user, listing=flat)

        if saved.exists():
            saved.delete()

            return saveAnswer(request, flat, "Removed from your saved flats.")

        collection = mainFolder(request.user)
    elif newName:
        collection = request.user.collections.filter(name__iexact=newName).first()

        if collection is None:
            collection = Collection.objects.create(user=request.user, name=newName)
    else:
        folderId = request.POST.get("collection", "")
        collection = None

        if folderId.isdecimal() and len(folderId) < 19:
            collection = request.user.collections.filter(pk=int(folderId)).first()

    if collection is None:
        return saveAnswer(request, flat, "Pick a list or type a name for a new one.", True)

    item = SavedListing.objects.filter(collection=collection, listing__publicId=publicId)

    if item.exists() and not newName:
        item.delete()

        return saveAnswer(request, flat, f"Removed from {collection.name}.")

    if item.exists():
        return saveAnswer(request, flat, f"Already in {collection.name}.")

    live = Listing.objects.public().filter(publicId=publicId, status="live").first()

    if live is None:
        return saveAnswer(
            request, flat, "This flat is no longer for sale, so it can’t be saved.", True
        )

    SavedListing.objects.get_or_create(collection=collection, listing=live)

    return saveAnswer(request, flat, f"Saved to {collection.name}.")


@login_required
@transaction.atomic
def collection_create(request):
    User.objects.select_for_update().get(pk=request.user.pk)

    if not hasRole(request.user, "buyer"):
        return notAllowed(request, "buyer")

    name = cleanName(request.POST.get("name", "")) if request.method == "POST" else ""

    if not name:
        messages.error(request, "Please give your list a name.")

        return redirect("saved")

    folder = request.user.collections.filter(name__iexact=name).first()

    if folder is None:
        folder = Collection.objects.create(user=request.user, name=name)
        messages.success(request, f"{name} created.")

    return redirect(f"{reverse('saved')}?c={folder.pk}")


@login_required
@transaction.atomic
def collection_rename(request, pk):
    User.objects.select_for_update().get(pk=request.user.pk)

    if not hasRole(request.user, "buyer"):
        return notAllowed(request, "buyer")

    folder = request.user.collections.filter(pk=pk).first()
    name = cleanName(request.POST.get("name", "")) if request.method == "POST" else ""

    if folder and name:
        if request.user.collections.filter(name__iexact=name).exclude(pk=pk).exists():
            messages.error(request, f"You already have a list called {name}. Pick another name.")
        else:
            folder.name = name
            folder.save()
            messages.success(request, f"Renamed to {name}.")

    return redirect(f"{reverse('saved')}?c={pk}")


@login_required
@transaction.atomic
def collection_delete(request, pk):
    User.objects.select_for_update().get(pk=request.user.pk)

    if not hasRole(request.user, "buyer"):
        return notAllowed(request, "buyer")

    if request.method == "POST":
        folder = request.user.collections.filter(pk=pk).first()

        if folder:
            folder.delete()
            messages.success(request, f"{folder.name} deleted. The flats are still for sale.")

    return redirect("saved")
