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


def warningKey(form):
    lines = "|".join(f"{name}:{form.warnings[name]}" for name in sorted(form.warnings))

    return sha256(lines.encode()).hexdigest()[:16]


def toConfirm(request, form):
    return bool(form.warnings) and request.POST.get("warningsSeen") != warningKey(form)


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

    context = {
        "r": rows[0],
        "flat": rows[0]["flat"],
        "canListAnother": not otherOpen(request.user),
    }

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


