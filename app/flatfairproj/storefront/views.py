import logging
import time
from urllib.parse import urlencode, urlsplit
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import prefetch_related_objects, Count, Exists, F, OuterRef, Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET
from accounts.tokens import addTry
from adminpanel.access import canOpen, canSeeFlat
from sellerpanel.models import ListingDraft
from sellerpanel.photos import DRAFTS_KEY
from vision.models import DetectedItem
from . import address
from storefront.models import (
    needEmail, currentMode, hasRole, notAllowed, flatIssues, sizeHint, sizeHints,
    Listing, ListingPhoto, FlatView, Product, SavedListing, ViewingBooking,
    picsByRoom, withUsed,
)
from .activity import deal_away, buy_data
from .forms import ValuationForm, MOP_YEARS
from .interiors import picCredits, roomPhotos
from .presentation import (
    save_data, flatGlance, hasDeal, decorate, withTier, starFirst,
    readyPics, helloFor, budgetShelf, typeShelf,
)
from sellerpanel.stripepay import mightSave
from .recommend_flats import picksData
from .offeradvice import usualSales
from .search import (
    LEASE_CHOICES,
    SEARCH_FIELDS,
    correctedTown,
    typeCounts,
    costRange,
    tidySearch,
    zoneCounts,
    searchedEstate,
    findHomes,
    hintEstate,
    hintSearch,
    hintSector,
    hintTown,
)
from .valuation import band, TOWNS

LISTINGS_PER_PAGE = 12



def browseReturn(request, value):
    backup = reverse("listings")

    if not url_has_allowed_host_and_scheme(
        value, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return backup

    if value != backup and not value.startswith(backup + "?"):
        return backup

    parts = urlsplit(value)

    if parts.scheme or parts.netloc or parts.path != backup or parts.fragment:
        return backup

    return value


def home(request):
    if any(key in request.GET for key in SEARCH_FIELDS):
        return redirect(reverse("listings") + "?" + request.META.get("QUERY_STRING", ""))

    live = Listing.objects.public().filter(status="live")
    packs = Product.objects.filter(kind="package", isActive=True).order_by("price")
    towns = list(live.values("town").annotate(count=Count("pk")).order_by("town"))

    gallery = starFirst(live).annotate(acceptedCount=hasDeal()).prefetch_related(picsByRoom())

    buyerHome = (
        request.user.is_authenticated
        and not request.user.is_staff
        and currentMode(request) == "buyer"
    )
    promo = []

    if not buyerHome:
        promo = decorate(
            gallery.filter(Exists(ListingPhoto.objects.filter(listing_id=OuterRef("pk"))))[:1]
        )

    if promo:
        gallery = gallery.exclude(pk=promo[0].pk)

    gallery = list(gallery[:(4)])
    sells = hasRole(request.user, "seller")
    minAsk, maxAsk = costRange()

    context = {
        "total": sum(row["count"] for row in towns),
        "priceFloor": minAsk,
        "priceCap": maxAsk,
        "budgetFloor": minAsk if minAsk is not None else 0,
        "budgetCap": maxAsk if maxAsk is not None else 3000000,
        "budgetMin": minAsk if minAsk is not None else 0,
        "budgetMax": maxAsk if maxAsk is not None else 3000000,
        "liveListings": decorate(gallery),
        "promoFlat": promo[0] if promo else None,
        "liveTowns": towns,
        "typeShelf": typeShelf(live, [flat.pk for flat in promo + gallery]),
        "budgetShelf": budgetShelf(live),
        "townCount": len(towns),
        "quickTowns": TOWNS,
        "verifiedPhotoCount": ListingPhoto.objects.exclude(aiIrrelevant=None).count(),
        "startingPackage": packs.first(),
        "startingAddon": Product.objects.filter(kind="addon", isActive=True)
        .order_by("price")
        .first(),
        "illustration": mightSave(),
        "canList": sells or not request.user.is_authenticated,
        "listUrl": reverse("sell") if sells else reverse("register") + "?role=seller",
        "typical": usualSales("SENGKANG", "4 ROOM"),
        "roomPhotos": roomPhotos(),
        **save_data(request.user),
        **picksData(request),
    }

    if buyerHome:
        faves = Listing.objects.public().filter(savedBy__collection__user=request.user).distinct()

        mySaved = decorate(
            withTier(
                faves.annotate(
                    acceptedCount=Count(
                        "offers", filter=Q(offers__status="accepted"), distinct=True
                    ),
                ).order_by("-createdAt", "-pk")[:3]
            )
        )

        hints = list(context.get("recommendedFlats") or context["liveListings"])[:3]
        readyPics(mySaved + hints)

        context.update(
            {
                "buyerLanding": True,
                "buyerGreeting": helloFor(request.user),
                "buyerSaved": mySaved,
                "buyerSavedCount": faves.count(),
                "buyerSuggestions": hints,
                "buyerReplyCount": request.user.offers.filter(status="countered").count(),
                "buyerViewingCount": request.user.viewingBookings.filter(
                    slot__startsAt__gte=timezone.now(), status__in=("requested", "confirmed")
                ).count(),
            }
        )

    return render(request, "storefront/home.html", context)


def listings(request):
    sorts = {
        "price_asc": ("askingPrice", "pk"),
        "price_desc": ("-askingPrice", "-pk"),
        "newest": ("-createdAt", "-pk"),
    }

    ranked, filters = findHomes(request.GET)
    ranked = ranked.annotate(acceptedCount=hasDeal())
    sort = request.GET.get("sort", "recommended")

    if sort in sorts:
        ranked = ranked.order_by(*sorts[sort])
    else:
        sort = "recommended"
        ranked = ranked.annotate(
            hasPhotos=Exists(ListingPhoto.objects.filter(listing_id=OuterRef("pk")))
        )
        ranked = ranked.order_by("-rank", "-hasPhotos", "taken", "-createdAt", "-pk")

    page = Paginator(ranked, LISTINGS_PER_PAGE).get_page(request.GET.get("page"))
    flats = list(page.object_list)
    prefetch_related_objects(flats, picsByRoom())
    page.object_list = decorate(flats)
    query = request.GET.copy()
    query.pop("page", None)
    query.pop("map", None)

    if filters["searchQuery"]:
        query["q"] = filters["searchQuery"]
    else:
        query.pop("q", None)

    for field in ("town", "region", "flatType"):
        if filters[field]:
            query[field] = filters[field]
        else:
            query.pop(field, None)

    for field in ("minPrice", "maxPrice"):
        if filters[field] != "":
            query[field] = str(filters[field])
        else:
            query.pop(field, None)

    labels = {
        "q": filters["searchQuery"],
        "town": str(filters["town"]).title(),
        "region": filters["region"],
        "flatType": str(filters["flatType"]).replace(" ROOM", "-room").capitalize(),
        "minPrice": f"From ${filters['minPrice']:,}" if filters["minPrice"] != "" else "",
        "maxPrice": f"Up to ${filters['maxPrice']:,}" if filters["maxPrice"] != "" else "",
        "minSqm": f"From {float(filters['minSqm']):g} sqm" if filters["minSqm"] else "",
        "maxSqm": f"Up to {float(filters['maxSqm']):g} sqm" if filters["maxSqm"] else "",
        "bedrooms": f"{filters['bedrooms']}+ bedrooms" if filters["bedrooms"] else "",
        "bathrooms": f"{filters['bathrooms']}+ bathrooms" if filters["bathrooms"] else "",
        "minLease": f"{filters['minLease']}+ years left on lease" if filters["minLease"] else "",
        "balcony": "Balcony" if filters["balcony"] else "",
    }

    chips = []
    placeOk = ""

    for key, label in labels.items():
        if label:
            left = query.copy()
            left.pop(key, None)
            chips.append({"label": label, "url": "?" + left.urlencode()})

            if key in ("town", "region"):
                placeOk = chips[-1]["url"]

    sort_by = []

    for key, values in query.lists():
        if key == "sort":
            continue

        for value in values:
            sort_by.append((key, value))

    typeRows, typeAll = typeCounts(query, filters["priceCap"])
    areaRows, areaAll = ([], 0) if filters["town"] else zoneCounts(query, filters["priceCap"])

    context = {
        **filters,
        "correctedTown": correctedTown(request.GET),
        "searchedEstate": searchedEstate(request.GET),
        "locationClear": placeOk,
        "typeChoices": typeRows,
        "areaChoices": areaRows,
        "areaTotal": areaAll,
        "typeTotal": typeAll,
        "listings": page.object_list,
        "pageObj": page,
        "sort": sort,
        "filterChips": chips,
        "sortFields": sort_by,
        "total": page.paginator.count,
        "pageQuery": query.urlencode(),
        "browseReturn": browseReturn(request, request.get_full_path()),
        "searchParams": tidySearch(query),
        **(picksData(request) if not chips else {}),
        **save_data(request.user),
    }

    return render(request, "storefront/listings.html", context)


def layoutUrl(form):
    names = {
        "flatType": "flatType",
        "area": "floorAreaSqm",
        "flatModel": "flatModel",
        "lease": "leaseCommenceYear",
        "storey": "storey",
    }

    query = {}

    for name, key in names.items():
        try:
            if form.fields[name].clean(form[name].value()):
                query[key] = form[name].value()
        except ValidationError:
            pass

    if "flatType" not in query or "floorAreaSqm" not in query:
        return ""

    return reverse("floor_plan_preview") + "?" + urlencode(query)


log = logging.getLogger(__name__)

VALUATION_STEPS = [
    ("Where is the flat?", "Town", ["town", "postal"]),
    ("What type of flat?", "Flat type", ["flatType"]),
    ("How big is it?", "Size", ["area"]),
    ("Which floor is it on?", "Floor", ["storey"]),
    ("What year did the lease start?", "Lease start", ["lease"]),
    ("Anything else? (optional)", "Extras", ["flatModel", "ask"]),
]


def likeUrl(data):
    year = timezone.localdate().year
    area = int(data["area"])
    base = {"town": data["town"], "flatType": data["flatType"]}
    size = {**base, "minSqm": max(area - 10, 0) // 5 * 5, "maxSqm": -(-(area + 10) // 5) * 5}
    leaseLeft = max(
        (years for years in LEASE_CHOICES if years <= 99 - (year - data["lease"])), default=0
    )
    choices = [{**size, "minLease": leaseLeft}] if leaseLeft else []

    for params in choices + [size, base]:
        flats = Listing.objects.public().filter(
            status="live", town=params["town"], flatType=params["flatType"]
        )

        if "minSqm" in params:
            flats = flats.filter(
                floorAreaSqm__gte=params["minSqm"], floorAreaSqm__lte=params["maxSqm"]
            )

        if "minLease" in params:
            flats = flats.filter(leaseCommenceYear__gte=year - 99 + params["minLease"])

        if flats.exists():
            break

    return reverse("listings") + "?" + urlencode(params)


def showWorth(request, context):
    units = {"area": "sqm", "storey": "floor", "lease": "year"}
    form = context["form"]
    steps = []
    start = 0

    for number, (legend, short, names) in enumerate(VALUATION_STEPS, 1):
        fields = [form[name] for name in names]

        for field in fields:
            field.unit = units.get(field.name, "")

        if form.is_bound:
            stuck = any(field.errors for field in fields)
        else:
            stuck = number < len(VALUATION_STEPS) and not any(field.value() for field in fields)

        if stuck and not start:
            start = number

        steps.append({"number": number, "legend": legend, "short": short, "fields": fields})

    context["steps"] = steps
    context["startStep"] = start or 1
    context["layout"] = layoutUrl(form)
    context["sizeHints"] = sizeHints()

    return render(request, "storefront/valuation.html", context)


def filledForm(answers):
    form = ValuationForm(answers)

    try:
        form.is_valid()
    except Exception:
        log.exception("Address check failed")
        answers = answers.copy()
        answers["postal"] = ""
        form = ValuationForm(answers)
        form.is_valid()

    return form


def fairRange(data, address):
    return band(
        data["town"],
        data["flatType"],
        data["flatModel"],
        data["area"],
        data["storey"],
        data["lease"],
        postal=data.get("postal"),
        block=address["block"] if address else None,
    )


@require_GET
def valuation_check(request):
    key = f"valuation-check:{request.META.get('REMOTE_ADDR', '')}"

    if addTry(key, 60) > 240:
        reply = JsonResponse({"errors": {}, "warnings": {}, "fixes": {}}, status=429)
        reply["Retry-After"] = "60"

        return reply

    form = filledForm(request.GET)
    data = form.cleaned_data
    wanted = [name for name in form.fields if request.GET.get(name)]
    reached = request.GET.get("step", "")

    if reached.isdigit():
        for legend, short, names in VALUATION_STEPS[: int(reached)]:
            wanted.extend(names)

    errors = {}

    for name in wanted:
        if name in form.errors:
            errors[name] = form.errors[name][0]

    if data.get("flatType") and data.get("flatModel"):
        issues = flatIssues(data["flatType"], data["flatModel"], data.get("area"))

        for name, message in issues.items():
            errors.setdefault(name, message)

    warns = dict(form.warnings)
    sizeTip = sizeHint(data.get("flatType"), data.get("area"))

    if sizeTip:
        warns.setdefault("area", sizeTip)

    lease = data.get("lease")

    if lease and lease + MOP_YEARS > timezone.localdate().year:
        warns["lease"] = (
            f"This flat can’t be resold until {lease + MOP_YEARS}. You can still see a price."
        )

    if data.get("ask") and not form.errors and not errors:
        try:
            low_cost, fair_cost, top_cost = fairRange(data, form.address)

            if data["ask"] < low_cost * 0.7 or data["ask"] > top_cost * 1.3:
                warns["ask"] = (
                    f"Similar flats may sell between ${round(low_cost, -3):,} and ${round(top_cost, -3):,}. "
                    "Please check the price you typed."
                )
        except Exception:
            pass

    for name in errors:
        warns.pop(name, None)

    fixes = {}

    if form.address:
        fixes = {"town": form.address["town"], "townLabel": form.address["townLabel"]}

    return JsonResponse({"errors": errors, "warnings": warns, "fixes": fixes})


def valuation(request):
    asked = False

    for name in (*ValuationForm.base_fields, *ValuationForm.aliases):
        if name in request.GET:
            asked = True

    unsized = "area" not in request.GET and "floorAreaSqm" not in request.GET

    if asked and (unsized or "edit" in request.GET):
        form = ValuationForm(initial=request.GET.dict())

        return showWorth(request, {"form": form})

    form = filledForm(request.GET) if asked else ValuationForm(None)
    context = {"form": form}

    if not asked or not form.is_valid():
        return showWorth(request, context)

    data = form.cleaned_data
    issues = flatIssues(data["flatType"], data["flatModel"], data["area"])

    if issues:
        for field, message in issues.items():
            form.add_error(field, message)

            form.fields[field].widget.attrs.update(
                {"aria-invalid": "true", "aria-describedby": f"id_{field}_error"}
            )

        return showWorth(request, context)

    answers = {"edit": "1"}

    for name in form.fields:
        if form.data.get(name):
            answers[name] = form.data.get(name)

    context["editUrl"] = reverse("valuation") + "?" + urlencode(answers)
    context["done"] = True
    context["similarUrl"] = likeUrl(data)
    carried = {
        "town": data["town"],
        "flatType": data["flatType"],
        "floorAreaSqm": f"{data['area']:g}",
        "storey": data["storey"],
        "leaseCommenceYear": data["lease"],
    }

    if data.get("postal"):
        carried["postalCode"] = data["postal"]

    context["listUrl"] = reverse("sell") + "?" + urlencode(carried)

    if data["lease"] + MOP_YEARS > timezone.localdate().year:
        context["mopEnds"] = data["lease"] + MOP_YEARS

    try:
        low_cost, fair_cost, top_cost = fairRange(data, form.address)
        context["illustration"] = mightSave(fair_cost)
    except Exception as error:
        if not isinstance(error, ValueError):
            log.exception("Price check failed")

        context["typical"] = usualSales(data["town"], data["flatType"])

        return showWorth(request, context)

    sizeTip = sizeHint(data["flatType"], data["area"])

    if sizeTip:
        messages.info(request, sizeTip)

    context["lo"] = low_cost
    context["est"] = fair_cost
    context["hi"] = top_cost
    context["asking"] = data["ask"] or 0

    if fair_cost and context.get("similarUrl"):
        context["similarUrl"] += "&" + urlencode({"est": int(round(fair_cost, -3))})

    return showWorth(request, context)


def countView(request, flat):
    if request.user.is_staff or (
        request.user.is_authenticated and flat.seller_id == request.user.pk
    ):
        return

    seen = request.session.get("seenListings", [])

    if flat.pk in seen:
        return

    Listing.objects.filter(pk=flat.pk).update(viewCount=F("viewCount") + 1)
    seen.append(flat.pk)
    request.session["seenListings"] = seen[-200:]


def listing_detail(request, publicId):
    flat = get_object_or_404(withTier(Listing.objects.select_related("seller")), publicId=publicId)
    prefetch_related_objects([flat], picsByRoom())
    decorate([flat])
    public = Listing.objects.public().filter(pk=flat.pk).exists()
    teamPeek = canOpen(request.user, "admin_queue") and not public

    if not canSeeFlat(request.user, flat):
        if flat.status == "withdrawn":
            messages.info(
                request, "This flat is no longer for sale. The seller has taken it off FlatFair."
            )

        return redirect("home")

    countView(request, flat)

    if hasRole(request.user, "buyer") and flat.seller_id != request.user.pk:
        now = timezone.now()

        with transaction.atomic():
            FlatView.objects.update_or_create(
                buyer=request.user,
                listing=flat,
                defaults={"count": F("count") + 1, "lastViewedAt": now},
                create_defaults={"count": 1, "firstViewedAt": now, "lastViewedAt": now},
            )

    if request.GET.get("save") == "1" and request.user.is_authenticated:
        canSave = (
            hasRole(request.user, "buyer")
            and flat.status == "live"
            and public
            and flat.seller_id != request.user.pk
        )

        if (
            canSave
            and not SavedListing.objects.filter(
                collection__user=request.user, listing=flat
            ).exists()
        ):
            request.session["openSaveMenu"] = str(flat.publicId)

        return redirect(reverse("listing_detail", args=[flat.publicId]) + f"#save-{flat.publicId}")

    low_cost, fair_cost, top_cost = flat.bandLow, flat.estimatedPrice, flat.bandHigh

    if not (low_cost and fair_cost and top_cost):
        low_cost = fair_cost = top_cost = None

    nextSlots = flat.viewingSlots.filter(removed=False, startsAt__gt=timezone.now())
    myVisits = {}

    if request.user.is_authenticated:
        booked = ViewingBooking.objects.filter(
            slot__listing=flat,
            slot__removed=False,
            slot__startsAt__gt=timezone.now(),
            buyer=request.user,
            status__in=("requested", "confirmed"),
        ).select_related("slot")

        myVisits = {booking.slot_id: booking for booking in booked}

    mySlots = set(myVisits)

    free = withUsed(nextSlots).filter(usedCount__lt=F("capacity")).exclude(pk__in=mySlots)

    seenIds = list(free.order_by("startsAt", "pk").values_list("pk", flat=True)[:4]) + list(mySlots)

    slots = list(withUsed(nextSlots.filter(pk__in=seenIds)).order_by("startsAt", "pk"))

    for slot in slots:
        slot.myBooking = myVisits.get(slot.pk)

    context = {
        "browseReturn": browseReturn(request, request.GET.get("return", "")),
        "activitySlots": slots,
        "canBook": any(slot.placesLeft and slot.pk not in mySlots for slot in slots),
        "offer97": int(round(flat.askingPrice * 0.97, -3)),
        "offer95": int(round(flat.askingPrice * 0.95, -3)),
        "canTransact": bool(flat.status == "live" and flat.seller_id and flat.seller.is_active),
        "isBuyer": hasRole(request.user, "buyer") if request.user.is_authenticated else False,
        "myOffer": (
            flat.offers.filter(buyer=request.user, status__in=("pending", "countered", "accepted"))
            .annotate(agreedElsewhere=deal_away())
            .first()
            if request.user.is_authenticated
            else None
        ),
        "myBookedSlots": mySlots,
        "staffPreview": teamPeek,
        "glance": flatGlance(flat),
        "flat": flat,
        "lo": low_cost,
        "est": fair_cost,
        "hi": top_cost,
        "saveOpenFor": (
            flat.pk if request.session.pop("openSaveMenu", "") == str(flat.publicId) else None
        ),
        **save_data(request.user),
    }

    bidError = getattr(request, "offer_error", None)

    if bidError and (context["myOffer"] or not context["canTransact"]):
        messages.error(request, bidError["text"])
        bidError = None

    context["offerError"] = bidError

    return render(request, "storefront/listing_detail.html", context)


@require_GET
def listing_photo(request, publicId, room):
    flat = get_object_or_404(Listing, publicId=publicId)
    public = Listing.objects.public().filter(pk=flat.pk).exists()

    if not public and not canSeeFlat(request.user, flat):
        raise Http404

    photo = get_object_or_404(flat.photos, room=room)

    try:
        reply = FileResponse(photo.image.open("rb"))
    except (OSError, ValueError):
        raise Http404

    reply["Cache-Control"] = "public, max-age=0, must-revalidate" if public else "private, no-store"

    reply["X-Content-Type-Options"] = "nosniff"

    return reply


@require_GET
def address_search(request):
    return JsonResponse({"results": address.search(request.GET.get("q", "")[:60])})


def terms(request):
    return render(request, "storefront/terms.html")


def credits(request):
    return render(
        request,
        "storefront/credits.html",
        {"photos": sorted(picCredits()["photos"], key=lambda photo: photo["usedOn"])},
    )


# Show the listing plans



@login_required
@needEmail
def my_offers(request):
    if not hasRole(request.user, "buyer"):
        return notAllowed(request, "buyer")

    return render(request, "storefront/my_offers.html", buy_data(request.user, request.GET))





def lensDemo():
    from collections import Counter
    from . import lens as photoSearch
    from storefront.templatetags.estate_tags import FIXED_FEATURES

    photos = (
        ListingPhoto.objects.filter(
            listing__in=Listing.objects.public().filter(status="live"),
            modelVersion=photoSearch.MODEL_VERSION,
            embedding__isnull=False,
        )
        .exclude(image="")
        .select_related("listing")
        .only(
            "pk",
            "room",
            "image",
            "tags",
            "embedding",
            "aiIrrelevant",
            "listing__publicId",
            "listing__town",
        )
        .order_by("pk")
    )

    byGroup, byRoom, traitPics = {}, {}, {}
    morePics = []
    traitNums = Counter()

    for photo in photos.iterator(chunk_size=100):
        if photoSearch.validEmbedding(photo.embedding) is None or not photoSearch.scoreOk(
            photo.aiIrrelevant
        ):
            continue

        group = photo.room

        if photo.room.startswith(("master_bedroom", "bedroom_")):
            group = "bedroom"
        elif "toilet" in photo.room:
            group = "bathroom"
        elif photo.room not in ("living_room", "kitchen", "balcony"):
            group = "other"

        byGroup.setdefault(group, photo)
        byRoom.setdefault(photo.room, photo)

        if len(morePics) < 6:
            morePics.append(photo)

        labels = {
            feature[0]
            for tag in photo.tags or []
            if (feature := FIXED_FEATURES.get(str(tag).strip().lower()))
        }

        traitNums.update(labels)

        for label in labels:
            traitPics.setdefault(label, photo)

    groups = ("living_room", "kitchen", "bedroom", "bathroom", "balcony", "other")
    preferred = [byGroup[group] for group in groups if group in byGroup]
    samples = list(
        {photo.pk: photo for photo in [*preferred, *byRoom.values(), *morePics]}.values()
    )[:6]
    popular = sorted(traitNums, key=lambda label: (-traitNums[label], label))[:4]

    return {
        "lensSamples": samples,
        "popularLooks": [{"label": label, "photo": traitPics[label]} for label in popular],
    }


def lens(request):
    from base64 import b64encode
    from io import BytesIO
    from PIL import Image, ImageOps
    from . import lens as photoSearch

    context = {"form": photoSearch.LensForm()}

    if request.method == "POST":
        photoSearch.fromUpload(request, context)

        if context.get("searched"):
            upload = context["form"].cleaned_data["photo"]
            upload.seek(0)

            with Image.open(upload) as source:
                image = ImageOps.exif_transpose(source)
                image.thumbnail((320, 240))
                output = BytesIO()
                image.convert("RGB").save(output, "JPEG", quality=75, optimize=True)

            context["submittedPhoto"] = "data:image/jpeg;base64," + b64encode(
                output.getvalue()
            ).decode("ascii")
    elif "photo" in request.GET:
        photoSearch.fromPic(request, context)
    else:
        from sellerpanel.photoqueue import warmWorker

        warmWorker()

    readyPics([match["flat"] for match in context.get("matches", [])])

    for match in context.get("matches", []):
        rooms = [photo.pk for photo in match["flat"].orderedPhotos()]

        if match["photo"].pk in rooms:
            match["number"] = rooms.index(match["photo"].pk) + 1

    context["form"].fields["photo"].widget.attrs["accept"] = "image/jpeg,image/png"
    context.update(lensDemo())
    context.update(picksData(request))

    return render(request, "storefront/lens.html", context)



def picReply(file, public=False):
    try:
        reply = FileResponse(file.open("rb"))
    except (OSError, ValueError):
        raise Http404

    reply["Cache-Control"] = "public, max-age=0, must-revalidate" if public else "private, no-store"
    reply["X-Content-Type-Options"] = "nosniff"

    return reply


@require_GET
def draftPhoto(request, path):
    if not request.user.is_authenticated or not path.startswith("pending/"):
        raise Http404

    saved = ListingDraft.objects.filter(seller=request.user).first()
    paths = set(saved.photos.values()) if saved else set()

    for data in request.session.get(DRAFTS_KEY, {}).values():
        if data["created"] >= time.time() - 86400:
            paths.update(data["photos"].values())

    if path not in paths:
        raise Http404

    try:
        reply = FileResponse(default_storage.open(path, "rb"))
    except (OSError, ValueError):
        raise Http404

    reply["Cache-Control"] = "private, no-store"
    reply["X-Content-Type-Options"] = "nosniff"

    return reply


@require_GET
def visionCrop(request, pk):
    item = get_object_or_404(DetectedItem.objects.select_related("analysis__photo__listing"), pk=pk)
    flat = item.analysis.photo.listing

    if not canSeeFlat(request.user, flat):
        raise Http404

    public = Listing.objects.public().filter(pk=flat.pk).exists()

    return picReply(item.crop, public)
