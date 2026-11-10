from storefront.listing_rules import (
    ABOVE_RANGE_NOTE, ONE_FLAT_MESSAGE, OPEN_LISTING_STATUSES, heldByCost,
    liveIssue, otherOpen, passPrice, smallPriceGap,
)
from adminpanel.models import AuditLog, Decision, DocumentRequest
from django.db.models import Case, Count, OuterRef, Prefetch, Q, Subquery, Value, When
from .photos import (
    DRAFTS_KEY, appealPhoto, attachPics, badAppeal,
    busyRooms, checks, clearHeld, doubtfulPhotos,
    draft, dropPaths, dropPhoto, fileGuard,
    keepFiles, movedNote, pending, photoState,
    refused, reviewNote, scanUpload, sortNote,
    syncChecks, waitChecks,
)
from django.http import Http404, JsonResponse, QueryDict
from storefront.models import (
    LISTING_DETAILS, Listing, ListingPhoto, OPTIONAL_ROOMS,
    REQUIRED_ROOMS, ROOM_LABELS, ViewingBooking, counts,
    extraRooms, hasRole, lock_user, needEmail,
    needRooms, notAllowed, sizeHints,
)
from .models import ListingDraft
from .forms import ListingForm, PriceCheckForm
from .photocheck import MODEL_VERSION, TIMEOUT, doubtQ, reviewReasons
from types import SimpleNamespace
from storefront.valuation import VERSION, band, flagReason, verdict
from django.core.cache import cache
from django.core.files.storage import default_storage
from django import forms
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.cache import never_cache
from .plans import plansNow
from django.template.loader import render_to_string
from django.views.decorators.http import require_GET, require_POST
from django.urls import reverse
from storefront.activity import sell_data
from hashlib import sha256
import time
from django.db import transaction
from .photoqueue import warmWorker
from sellerpanel.views import sell_only


def hold_note(flat, photosFlagged=None):
    reasons = []



    if flat.bandLow and flat.estimatedPrice and flat.bandHigh:
        flag = flagReason(flat.askingPrice, flat.bandLow, flat.estimatedPrice, flat.bandHigh)

        if flag == "above":
            reasons.append("the price is higher than similar flats sold for")
        elif flag == "typo":
            reasons.append("the price is much lower than similar flats, please check for a typo")

    if photosFlagged is None:
        photosFlagged = flat.photos.filter(doubtQ()).exists()

    if photosFlagged:
        if flat.photos.exclude(modelVersion=MODEL_VERSION).exists():
            reasons.append("some photo checks are out of date, so our team needs to look at them")
        else:
            reasons.append("some photos need a closer look")

    message = "We're checking this listing before buyers see it"

    if len(reasons) == 2:
        message += ": " + reasons[0] + ", and " + reasons[1]
    elif reasons:
        message += ": " + reasons[0]

    return message + ". This usually takes 1 working day."


def sellSlots(slots):
    booked = ViewingBooking.objects.filter(status__in=("requested", "confirmed")).select_related(
        "buyer"
    )

    booked = booked.order_by(
        Case(When(status="requested", then=Value(0)), default=Value(1)), "createdAt", "pk"
    )

    slots = slots.annotate(
        usedCount=Count("bookings", filter=Q(bookings__status__in=("requested", "confirmed"))),
        waitingCount=Count("bookings", filter=Q(bookings__status="requested")),
        confirmedCount=Count("bookings", filter=Q(bookings__status="confirmed")),
    )


    slots = slots.annotate(
        replyOrder=Case(When(waitingCount__gt=0, then=Value(0)), default=Value(1))
    )

    return slots.prefetch_related(Prefetch("bookings", queryset=booked, to_attr="sellerBookings"))


def photoSlots(photoErrors, kept, existing=None, flaggedRooms=(), refusedPhotos=None, results=None):
    existing = existing or {}



    slots = []

    for rooms, extra in ((REQUIRED_ROOMS, False), (OPTIONAL_ROOMS, True)):
        for key, label, needs in rooms:
            url = ""

            if key in kept:
                url = reverse("draft_photo", kwargs={"path": kept[key]})
            elif key in existing:
                url = existing[key].publicUrl

            result = (results or {}).get(key, {})
            busy = result.get("checking", False)
            checkNote = ""


            if key in kept and not busy:
                reasons = reviewReasons(result)


                checkNote = (
                    reviewNote(result, key) if reasons else sortNote(result) or "Photo accepted."
                )

            slots.append(
                {
                    "key": key,
                    "label": label,
                    "optional": extra,
                    "needs": f"{needs[0]}:{needs[1]}" if needs else "",
                    "url": url,
                    "error": photoErrors.get(key, ""),
                    "flagged": key in flaggedRooms and key not in kept,
                    "appealable": key in (refusedPhotos or {}),
                    "appealed": result.get("appeal", False),
                    "checking": busy,
                    "checkMessage": checkNote,
                    "movedNote": movedNote(kept, results or {}, key),
                }
            )

    return slots


def flatBand(flat):
    return band(
        flat.town,
        flat.flatType,
        flat.flatModel,
        flat.floorAreaSqm,
        flat.storey,
        flat.leaseCommenceYear,
        postal=flat.postalCode,
        block=flat.block,
    )


def addPrices(context, flat, lo, est, hi):
    context["lo"] = lo
    context["est"] = est
    context["hi"] = hi
    context["verdict"] = verdict(flat.askingPrice, lo, est, hi)
    context["flag"] = flagReason(flat.askingPrice, lo, est, hi)
    context["smallPriceGap"] = smallPriceGap(
        SimpleNamespace(askingPrice=flat.askingPrice, bandLow=lo, estimatedPrice=est, bandHigh=hi)
    )


def warningKey(form):
    lines = "|".join(f"{name}:{form.warnings[name]}" for name in sorted(form.warnings))

    return sha256(lines.encode()).hexdigest()[:16]


def toConfirm(request, form):
    return bool(form.warnings) and request.POST.get("warningsSeen") != warningKey(form)


def sellForm(request, form, photoErrors, context):
    if request.method != "POST":
        warmWorker()

    for room, error in syncChecks(request).items():
        photoErrors.setdefault(room, error)

    token = draft(request)

    if request.method == "POST":
        request.session[DRAFTS_KEY][token]["formData"] = {
            name: request.POST.get(name, "") for name in ListingForm.base_fields
        }


        request.session.modified = True

    context["refreshUrl"] = f"{request.path}?draftToken={token}#sell-photos"
    context["draftToken"] = draft(request)
    context["form"] = form
    context["photoSlots"] = photoSlots(
        photoErrors, pending(request), refusedPhotos=refused(request), results=checks(request)
    )
    context["photosMissing"] = bool(photoErrors)
    context["sizeHints"] = sizeHints()
    minutes = (TIMEOUT + 59) // 60
    context["photoWait"] = f"up to {minutes} minute{'s' if minutes != 1 else ''}"


    return render(request, "sellerpanel/sell.html", context)


def oldAnswers(query):
    from storefront import address

    answers = {
        name: query.get(name, "").strip()[:20]
        for name in (
            ("town", "flatType", "floorAreaSqm", "storey", "leaseCommenceYear", "postalCode")
        )
        if query.get(name, "").strip()
    }

    if answers.get("postalCode"):
        found = address.search(answers["postalCode"])[:1]


        if found and found[0]["postal"] == answers["postalCode"]:
            answers["block"] = found[0]["block"]
            answers["town"] = found[0]["town"]
            answers["leaseCommenceYear"] = str(found[0]["leaseYear"])

    return answers


@login_required
@needEmail
def sell(request):
    if not hasRole(request.user, "seller"):
        return sell_only(request)

    keptDraft = ListingDraft.objects.filter(seller=request.user).first()

    if keptDraft is not None:
        drafts = request.session.get(DRAFTS_KEY, {})
        token = keptDraft.draftToken
        data = drafts.get(token)
        now = time.time()

        if data is None or data["created"] < now - 86400:
            if data:
                paths = list(data["photos"].values())
                paths += [photo["path"] for photo in data.get("refused", {}).values()]
                dropPaths(paths)

            drafts[token] = {"created": now, "photos": keptDraft.photos, "checks": keptDraft.checks}
            request.session[DRAFTS_KEY] = drafts

        if request.method != "POST" or request.POST.get("draftToken") == token:
            request._photoDraft = token

    context = {"savedDraft": keptDraft}
    picErrors = {}

    if request.method == "POST" and request.POST.get("action") == "discardDraft":
        with transaction.atomic():
            seller = lock_user(request.user.pk)
            keptDraft = ListingDraft.objects.filter(seller=seller).first()
            paths = list(keptDraft.photos.values()) if keptDraft else []
            ListingDraft.objects.filter(seller=seller).delete()

        clearHeld(request)


        if keptDraft:
            clearHeld(request, keptDraft.draftToken)

        dropPaths(paths)
        messages.success(request, "Your draft has been discarded.")

        return redirect("sell")

    if request.method != "POST" and otherOpen(request.user):
        messages.info(request, ONE_FLAT_MESSAGE)

        return redirect("my_listings")

    if request.method != "POST":
        token = draft(request)
        initial = request.session[DRAFTS_KEY][token].get(
            "formData", keptDraft.formData if keptDraft else None
        )

        if not initial:
            initial = oldAnswers(request.GET)

            if initial:
                messages.info(
                    request, "We’ve filled in the details from your price check. Please check them."
                )

        form = ListingForm(initial=initial)

        return sellForm(request, form, picErrors, context)

    token = request.POST.get("draftToken", "")


    if token and Listing.objects.filter(seller=request.user, publicationToken=token).exists():
        return redirect("my_listings")

    if otherOpen(request.user):
        messages.info(request, ONE_FLAT_MESSAGE)

        return redirect("my_listings")

    if request.POST.get("action") == "saveDraft":
        with transaction.atomic():
            seller = lock_user(request.user.pk)



            if not seller.is_active:
                return notAllowed(request, "active seller")

            if otherOpen(seller):
                messages.info(request, ONE_FLAT_MESSAGE)

                return redirect("my_listings")

            oldDraft = ListingDraft.objects.filter(seller=seller).first()
            paths = list(oldDraft.photos.values()) if oldDraft else []
            picErrors = keepFiles(request, syncResults=False)



            if picErrors:
                return sellForm(request, ListingForm(request.POST), picErrors, context)

            ListingDraft.objects.update_or_create(
                seller=seller,
                defaults={
                    "formData": {
                        name: request.POST.get(name, "") for name in ListingForm.base_fields
                    },
                    "draftToken": draft(request),
                    "photos": pending(request),
                    "checks": checks(request),
                },
            )


        dropPaths(paths)

        if oldDraft and oldDraft.draftToken != draft(request):
            clearHeld(request, oldDraft.draftToken)

        messages.success(request, "Your draft is saved.")

        return redirect("sell")

    form = ListingForm(request.POST)
    picErrors = keepFiles(request)

    if form.is_valid():
        for room, error in waitChecks(request).items():
            picErrors.setdefault(room, error)

    if not form.is_valid():
        return sellForm(request, form, picErrors, context)

    if toConfirm(request, form):
        return sellForm(request, form, picErrors, {**context, "warningKey": warningKey(form)})

    flat = form.save(commit=False)

    try:
        low_cost, fair_cost, top_cost = flatBand(flat)
    except ValueError as error:
        form.add_error(None, str(error))

        return sellForm(request, form, picErrors, context)

    addPrices(context, flat, low_cost, fair_cost, top_cost)
    have = counts(flat.bedrooms, flat.bathrooms, flat.hasBalcony)

    for room in needRooms(have):
        if room not in pending(request):
            picErrors.setdefault(room, f"Add a photo of the {ROOM_LABELS[room].lower()}.")


    if picErrors:
        return sellForm(request, form, picErrors, context)

    if context["flag"] and "confirm" not in request.POST:
        context["needsConfirm"] = True

        return sellForm(request, form, picErrors, context)

    rooms = needRooms(have) + extraRooms(have)
    unsure = doubtfulPhotos(request, rooms)



    flat.seller = request.user
    flat.estimatedPrice = fair_cost
    flat.bandLow = low_cost
    flat.bandHigh = top_cost
    flat.modelVersion = VERSION

    with fileGuard() as newPics, transaction.atomic():
        seller = lock_user(request.user.pk)
        token = draft(request)

        if Listing.objects.filter(seller=seller, publicationToken=token).exists():
            return redirect("my_listings")

        if not seller.is_active:
            return notAllowed(request, "active seller")

        if otherOpen(seller):
            messages.info(request, ONE_FLAT_MESSAGE)

            return redirect("my_listings")

        flat.publicationToken = token
        flat.status = "review"
        flat.save()
        attachPics(request, flat, rooms, newPics)



        if not context["flag"] and not unsure and not liveIssue(flat, flat.revision, "review"):
            flat.status = "live"


            flat.save(update_fields=["status"])

        passPrice(flat)
        keptDraft = ListingDraft.objects.filter(seller=seller).first()
        paths = list(keptDraft.photos.values()) if keptDraft else []
        ListingDraft.objects.filter(seller=seller).delete()

    clearHeld(request)

    if keptDraft:
        clearHeld(request, keptDraft.draftToken)

    dropPaths(paths)

    if flat.status == "live":
        messages.success(
            request,
            "Your listing is live. Next, add a few viewing times so buyers can visit."
            + (" " + ABOVE_RANGE_NOTE if smallPriceGap(flat) else ""),
        )

        return redirect(reverse("seller_viewings") + "?first=1#slot-form")

    messages.info(request, hold_note(flat) + " Once it’s live, we’ll ask you to add viewing times.")

    return redirect("my_listings")


def editForm(request, flat, form, photoErrors, existing, context):
    if request.method != "POST":
        warmWorker()

    for room, error in syncChecks(request).items():
        photoErrors.setdefault(room, error)

    token = draft(request)

    if request.method == "POST":
        request.session[DRAFTS_KEY][token]["formData"] = {
            name: request.POST.get(name, "") for name in ListingForm.base_fields
        }
        request.session.modified = True

    context["refreshUrl"] = f"{request.path}?draftToken={token}#sell-photos"



    flagRooms = set()

    if flat.status != "live":
        flagRooms = set(flat.photos.filter(doubtQ()).values_list("room", flat=True))
        choice, reason = staffNotes([flat]).get(flat.pk, (None, ""))

        if flat.status == "review":
            context["holdNote"] = hold_note(flat, bool(flagRooms))



        if reason and (flat.status, choice) in (
            ("review", "documents"),
            ("rejected", "rejected"),
            ("hidden", "rejected"),
        ):
            context["staffNote"] = reason

    context["editing"] = flat
    context["form"] = form
    context["photoSlots"] = photoSlots(
        photoErrors, pending(request), existing, flagRooms, refused(request), checks(request)
    )
    context["photosMissing"] = bool(photoErrors)
    context["draftToken"] = draft(request)
    context["sizeHints"] = sizeHints()


    minutes = (TIMEOUT + 59) // 60
    context["photoWait"] = f"up to {minutes} minute{'s' if minutes != 1 else ''}"


    return render(request, "sellerpanel/sell.html", context)


@login_required
@needEmail
def edit_listing(request, publicId):
    statuses = ("live", "review", "rejected", "hidden")

    if not hasRole(request.user, "seller"):
        return sell_only(request)

    flat = get_object_or_404(Listing, publicId=publicId, seller=request.user, status__in=statuses)

    token = draft(request)
    request.session[DRAFTS_KEY][token]["listingId"] = flat.pk
    request.session.modified = True
    before = flat.status


    existing = {photo.room: photo for photo in flat.photos.all()}
    context = {}
    picErrors = {}

    if request.method != "POST":
        initial = {
            "hasBalcony": "yes" if flat.hasBalcony else "no",
            "floorAreaSqm": f"{flat.floorAreaSqm:g}",
        }



        token = draft(request)
        initial.update(request.session[DRAFTS_KEY][token].get("formData", {}))
        form = ListingForm(instance=flat, initial=initial)

        return editForm(request, flat, form, picErrors, existing, context)

    if before in ("rejected", "hidden") and otherOpen(request.user, flat):
        messages.info(request, ONE_FLAT_MESSAGE)

        return redirect("my_listings")

    form = ListingForm(request.POST, instance=flat)
    picErrors = keepFiles(request)

    if form.is_valid():
        for room, error in waitChecks(request).items():
            picErrors.setdefault(room, error)

    if not form.is_valid():
        return editForm(request, flat, form, picErrors, existing, context)

    if toConfirm(request, form):
        return editForm(
            request, flat, form, picErrors, existing, {**context, "warningKey": warningKey(form)}
        )

    try:
        low_cost, fair_cost, top_cost = flatBand(flat)
    except ValueError as error:
        form.add_error(None, str(error))

        return editForm(request, flat, form, picErrors, existing, context)

    addPrices(context, flat, low_cost, fair_cost, top_cost)
    have = counts(flat.bedrooms, flat.bathrooms, flat.hasBalcony)
    kept = pending(request)

    for room in needRooms(have):
        if room not in kept and room not in existing:
            picErrors.setdefault(room, f"Add a photo of the {ROOM_LABELS[room].lower()}.")

    if picErrors:
        return editForm(request, flat, form, picErrors, existing, context)

    if context["flag"] and "confirm" not in request.POST:
        context["needsConfirm"] = True

        return editForm(request, flat, form, picErrors, existing, context)

    rooms = needRooms(have) + extraRooms(have)
    removed = [room for room in request.POST.getlist("removePhoto") if room in extraRooms(have)]
    replaced = [room for room in rooms if room in kept]



    unsure = doubtfulPhotos(request, replaced)

    with fileGuard() as newPics, transaction.atomic():
        seller = lock_user(request.user.pk)

        if not seller.is_active:
            return notAllowed(request, "active seller")

        current = (
            Listing.objects.select_for_update().filter(pk=flat.pk, status__in=statuses).first()
        )


        if not current:
            return redirect("my_listings")

        if current.status != before:
            messages.error(
                request,
                "This listing changed while we were checking your photos, so nothing was saved. Please reload the listing and try again.",
            )

            return redirect("my_listings")

        if current.revision != form.cleaned_data["revision"]:
            form.add_error(
                None,
                "This listing was changed in another tab or by our team. Please reload the page, check the latest details, then save again.",
            )

            return editForm(request, flat, form, picErrors, existing, context)


        picErrors = badAppeal(request)

        if picErrors:
            return editForm(request, flat, form, picErrors, existing, context)

        if otherOpen(seller, current):
            messages.info(request, ONE_FLAT_MESSAGE)

            return redirect("my_listings")

        for room in replaced:
            if not default_storage.exists(kept[room]):
                return redirect(f"{reverse('my_listings')}#listing-{flat.publicId}")



        roomsNow = set(current.photos.values_list("room", flat=True))

        for room in needRooms(have):
            if room not in roomsNow and room not in kept:
                messages.error(
                    request, "Your photos changed in another tab. Please check them and save again."
                )



                return redirect("edit_listing", publicId=flat.publicId)



        dropped = [
            room for room in roomsNow if room not in rooms or room in replaced or room in removed
        ]

        flat.estimatedPrice = fair_cost



        flat.bandLow = low_cost
        flat.bandHigh = top_cost
        flat.modelVersion = VERSION
        keptUnsure = current.photos.exclude(room__in=dropped).filter(doubtQ()).exists()

        priceOk = current.status == "live" or (
            heldByCost(current)
            and not dropped
            and not replaced
            and all(
                getattr(flat, field) == getattr(current, field)
                for field in LISTING_DETAILS - {"askingPrice"}
            )
        )

        stayLive = (
            current.status == "live" and not context["flag"] and not unsure and not keptUnsure
        )



        flat.status = "review"

        edited = [
            field.name for field in Listing._meta.concrete_fields if field.name in form.fields
        ]

        flat.save(
            update_fields=[
                *edited,
                "estimatedPrice",
                "bandLow",
                "bandHigh",
                "modelVersion",
                "status",
            ]
        )

        for photo in flat.photos.filter(room__in=dropped):
            dropPhoto(flat, photo)

        attachPics(request, flat, replaced, newPics)

        if stayLive and not liveIssue(flat, flat.revision, "review"):
            flat.status = "live"


            flat.save(update_fields=["status"])

        if current.status == "review" and not priceOk:
            AuditLog.objects.create(
                actor=seller,
                action="listing sent to review",
                target=f"Listing #{flat.pk} ({flat.town.title()})",
                detail="Listing edited while a fresh review was required.",
            )

        passPrice(flat, wasHeld=priceOk)

    clearHeld(request)

    if flat.status == "live":
        messages.success(
            request,
            "Your changes are saved. Buyers can see them now."
            + (" " + ABOVE_RANGE_NOTE if smallPriceGap(flat) else ""),
        )
    elif before in ("rejected", "hidden"):
        messages.success(request, "We’ve sent your listing back to our team. " + hold_note(flat))
    else:
        messages.success(request, "Your changes are saved. " + hold_note(flat))

    return redirect(f"{reverse('my_listings')}#listing-{flat.publicId}")


@never_cache
@login_required
@require_POST
@needEmail
def photo_check(request):
    if not hasRole(request.user, "seller"):
        return JsonResponse({"error": "Sign in with a seller account to add photos."}, status=403)

    token = request.POST.get("draftToken")


    room = request.POST.get("room")

    if token not in request.session.get(DRAFTS_KEY, {}) or room not in ROOM_LABELS:
        return JsonResponse(
            {"error": "Please reload the page, then add this photo again."}, status=400
        )

    upload = request.FILES.get("photo")
    appeal = request.POST.get("appeal") == "1"

    if not upload and not appeal:
        return JsonResponse({"error": "Please choose a photo."}, status=400)

    busy = f"photo-check:{request.user.pk}"


    if not cache.add(busy, 1, 10):
        return JsonResponse(
            {"error": "Please wait for your last photo to finish uploading."}, status=429
        )

    try:
        error, result = appealPhoto(request, room) if appeal else scanUpload(request, room, upload)
    finally:
        cache.delete(busy)

    if error and result:
        return JsonResponse({"verdict": "block", "message": error, "appealable": True})



    if error:
        if appeal:
            return JsonResponse(
                {"verdict": "block", "message": error, "appealable": room in refused(request)},
                status=400,
            )

        return JsonResponse({"verdict": "invalid", "message": error}, status=400)

    if appeal:
        return JsonResponse(
            {
                "verdict": "review",
                "message": "Our team will check this photo before your listing can go live.",
                "url": reverse("draft_photo", kwargs={"path": pending(request)[room]}),
            }
        )

    return JsonResponse(photoState(request, room))


@login_required
@needEmail
def cost_check(request):
    if not hasRole(request.user, "seller"):
        return JsonResponse(
            {"error": "Sign in with a seller account to check a price."}, status=403
        )



    form = PriceCheckForm(request.GET)
    context = {}

    if form.is_valid():
        data = form.cleaned_data

        try:
            low_cost, fair_cost, top_cost = band(
                data["town"],
                data["flatType"],
                data["flatModel"],
                data["floorAreaSqm"],
                data["storey"],
                data["leaseCommenceYear"],
                postal=data["postalCode"] or None,
                block=data["block"].upper() or None,
            )

            context["lo"] = low_cost



            context["est"] = fair_cost
            context["hi"] = top_cost
            context["asking"] = data["askingPrice"]



            if data["askingPrice"]:
                context["flag"] = flagReason(data["askingPrice"], low_cost, fair_cost, top_cost)
                context["smallPriceGap"] = smallPriceGap(
                    SimpleNamespace(
                        askingPrice=data["askingPrice"],
                        bandLow=low_cost,
                        estimatedPrice=fair_cost,
                        bandHigh=top_cost,
                    )
                )
        except ValueError:
            context["failed"] = True

    html = render_to_string("sellerpanel/price_check.html", context)


    return JsonResponse(
        {
            "lo": context.get("lo"),
            "hi": context.get("hi"),
            "flag": context.get("flag"),
            "html": html,
        }
    )


def staffNotes(flats):
    latest = Decision.objects.filter(listing=OuterRef("pk")).order_by("-createdAt", "-pk")

    rows = Listing.objects.filter(pk__in=[flat.pk for flat in flats]).annotate(
        staffDecision=Subquery(latest.values("decision")[:1]),
        staffReason=Subquery(latest.values("reason")[:1]),
    )

    notes = {}

    for row in rows:
        notes[row.pk] = (row.staffDecision, row.staffReason)

    return notes


@login_required
def my_listings(request):
    if not hasRole(request.user, "seller"):
        return sell_only(request)

    events = sell_data(request.user, request.GET)



    rows = flatRows(request, events["activityFlats"])
    liveRows = [row for row in rows if row["flat"].status != "sold"]
    soldRows = [row for row in rows if row["flat"].status == "sold"]

    context = {
        **events,
        "counts": events["listingCounts"],
        "selectedStatus": request.GET.get("status", ""),
        "totalCount": events["totalListingCount"],
        "canListAnother": not otherOpen(request.user),
        "rows": liveRows + soldRows,
        "savedDraft": ListingDraft.objects.filter(seller=request.user).first(),
    }

    return render(request, "sellerpanel/my_listings.html", context)


@login_required
def seller_listing(request, publicId):
    if not hasRole(request.user, "seller"):
        return sell_only(request)

    events = sell_data(request.user, request.GET)
    rows = [
        row
        for row in flatRows(request, events["activityFlats"])
        if row["flat"].publicId == publicId
    ]


    if not rows:
        raise Http404

    context = {"r": rows[0], "flat": rows[0]["flat"], "canListAnother": not otherOpen(request.user)}

    return render(request, "sellerpanel/listing_page.html", context)


def flatRows(request, flats):
    flagged = set(
        ListingPhoto.objects.filter(doubtQ(), listing__in=flats).values_list(
            "listing_id", flat=True
        )
    )

    notes = staffNotes(flats)
    docAsks = {}

    for docAsk in (
        DocumentRequest.objects.filter(listing__in=flats)
        .prefetch_related("files")
        .order_by("-createdAt", "-pk")
    ):
        docAsks.setdefault(docAsk.listing_id, docAsk)

    plans = plansNow(flats)
    askError = request.session.pop("priceError", None) or {}
    picked = request.GET.get("status", "")



    rows = []

    for flat in flats:
        if picked and flat.status != picked:
            continue

        row = {
            "flat": flat,
            "plan": plans.get(flat.pk),
            "documentRequest": docAsks.get(flat.pk),
            "history": flat.decisions.select_related("staff").order_by("-createdAt", "-pk")[:10],
        }


        choice, reason = notes.get(flat.pk, (None, ""))

        if flat.status == "review":
            row["holdMessage"] = hold_note(flat, flat.pk in flagged)

            if choice == "documents" and reason:
                row["staffNote"] = reason

        if flat.status in ("rejected", "hidden") and choice in ("rejected", "takendown") and reason:
            row["staffNote"] = reason

        if flat.status == "review" and flat.estimatedPrice and flat.pk not in flagged:
            flag = flagReason(flat.askingPrice, flat.bandLow, flat.estimatedPrice, flat.bandHigh)

            if flag == "above" and choice in (None, "approved"):
                row["lowerTo"] = flat.bandHigh


        if askError.get("pk") == flat.pk:
            row["priceError"] = askError

        rows.append(row)

    return rows


def sellState(flat, action, flagged=False):
    if flat.status in ("rejected", "hidden"):
        return

    if action == "sold":
        if flat.status == "sold":
            target = flat.statusBeforeSold or "review"
            problem = liveIssue(flat, flat.revision, "sold") if target == "live" else ""
            flat.status = "review" if problem else target



            flat.statusBeforeSold = ""
        elif flat.status in ("live", "paused"):
            flat.statusBeforeSold = flat.status
            flat.status = "sold"
    elif flagged:
        if flat.status == "sold":
            if flat.statusBeforeSold not in ("rejected", "hidden"):
                flat.statusBeforeSold = "review"
        else:
            flat.status = "review"


def backToFlat(request, flat, part=""):
    sent = QueryDict(request.POST.get("returnQuery", ""))
    keep = QueryDict(mutable=True)



    if sent.get("listings_page", "").isdigit():
        keep["listings_page"] = sent["listings_page"]

    if sent.get("status") and sent["status"] == flat.status:
        keep["status"] = sent["status"]

    query = ""


    if keep:
        query = "?" + keep.urlencode()

    return redirect(f"{reverse('my_listings')}{query}#listing-{flat.publicId}{part}")


def lockMyFlat(request, publicId):
    if not hasRole(request.user, "seller"):
        return None, None, sell_only(request)

    if request.method != "POST":
        return None, None, redirect("my_listings")



    seller = lock_user(request.user.pk)

    if not seller.is_active:
        return None, None, notAllowed(request, "active seller")

    flat = (
        Listing.objects.select_for_update().filter(publicId=publicId, seller=request.user).first()
    )

    if not flat:
        return None, None, redirect("my_listings")

    return seller, flat, None


@login_required
@transaction.atomic
@needEmail
def update_price(request, publicId):
    seller, flat, refused = lockMyFlat(request, publicId)

    if refused:
        return refused

    try:
        newAsk = forms.IntegerField(min_value=50000, max_value=3000000).clean(
            request.POST.get("askingPrice", "")
        )
    except forms.ValidationError:
        if request.POST.get("back") == "flat":
            messages.error(
                request, "Please enter a price from $50,000 to $3,000,000, with no cents."
            )

            return redirect(reverse("seller_listing", args=[flat.publicId]) + "#change-price")

        request.session["priceError"] = {
            "pk": flat.pk,
            "value": request.POST.get("askingPrice", "")[:12],
            "message": "Please enter a price from $50,000 to $3,000,000, with no cents.",
        }

        return backToFlat(request, flat, "-price")

    if request.POST.get("preview") == "1":
        return redirect(
            reverse("seller_listing", args=[flat.publicId]) + f"?try={newAsk}#change-price"
        )

    try:
        low_cost, fair_cost, top_cost = flatBand(flat)
    except ValueError as error:
        messages.error(request, str(error))

        return backToFlat(request, flat)

    flag = flagReason(newAsk, low_cost, fair_cost, top_cost)
    wasHeld = heldByCost(flat)
    priceOk = flat.status == "live" or wasHeld
    flat.askingPrice = newAsk
    flat.estimatedPrice = fair_cost
    flat.bandLow = low_cost
    flat.bandHigh = top_cost
    flat.modelVersion = VERSION
    sellState(flat, "price", bool(flag))

    if flat.status in OPEN_LISTING_STATUSES and otherOpen(seller, flat):
        messages.info(request, ONE_FLAT_MESSAGE)

        return backToFlat(request, flat)

    flat.save(
        update_fields=[
            "askingPrice",
            "estimatedPrice",
            "bandLow",
            "bandHigh",
            "modelVersion",
            "status",
            "statusBeforeSold",
        ]
    )

    if flat.status == "review" and not priceOk:
        AuditLog.objects.create(
            actor=seller,
            action="listing sent to review",
            target=f"Listing #{flat.pk} ({flat.town.title()})",
            detail="Price changed while another review hold was active.",
        )

    passPrice(flat, wasHeld=priceOk)

    if flat.status == "live" and smallPriceGap(flat):
        messages.success(
            request, "Your price is saved and your listing is live. " + ABOVE_RANGE_NOTE
        )
    elif flat.status == "review":
        messages.success(request, "Your price is saved. " + hold_note(flat))
    elif wasHeld and flat.status == "live":
        messages.success(
            request, "Your price is saved and your listing is live. Buyers can see it now."
        )
    else:
        messages.success(request, "Your price is saved.")

    if request.POST.get("back") == "flat":
        return redirect(reverse("seller_listing", args=[flat.publicId]) + "#change-price")

    return backToFlat(request, flat)
