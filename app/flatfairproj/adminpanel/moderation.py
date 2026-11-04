from sellerpanel.photocheck import AI_HOLD_AT, HOLD_AT, MODEL_VERSION, doubtQ, manualQ, missingQ
from django.db.models import (
    BooleanField, Count, ExpressionWrapper, F,
    OuterRef, Prefetch, Q, Subquery,
)
from django.db.models.functions import Coalesce
from .models import Decision, DocumentRequest
from storefront.presentation import LOW_RATING, readyPics
from storefront.models import Listing, ListingPhoto, Review, lock_user, object_id, roomOrder
from .forms import ListingDetailsForm
from storefront.listing_rules import (
    PHOTO_REVIEW_MESSAGE, STAFF_ONE_FLAT_MESSAGE, liveIssue, otherOpen,
)
from django.core.paginator import Paginator
from django.http import QueryDict
from django.contrib.auth.models import User
from storefront.valuation import VERSION, band, bandSpots, flagReason, verdict
from storefront.notices import _place, choice_note, notify
from django.shortcuts import get_object_or_404, redirect
from sellerpanel.plans import goodPacks
from django.contrib import messages
from sellerpanel.photos import niceLabel, photoStamp, planRedo, redoStale
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST
from django.urls import reverse
from .access import staffPage
from datetime import timedelta
from django.utils import timezone
from django.db import transaction
from django.utils.http import url_has_allowed_host_and_scheme
from adminpanel.views import (
    REASONS,
    STALE_PHOTO,
    audit,
    firstName,
    joinWords,
    mailto,
    page,
    plural,
)


def queueBack(request):
    back = request.POST.get("back", "")

    if back.startswith("?") and url_has_allowed_host_and_scheme(back, allowed_hosts=None):
        return redirect(reverse("admin_queue") + back)

    return redirect("admin_queue")


def queueSets(flats):
    picHold = (
        Q(aiPhotos__gt=0) | Q(unsurePhotos__gt=0) | Q(uncheckedPhotos__gt=0) | Q(stalePhotos__gt=0)
    )

    askHold = (
        Q(askingPrice__gt=F("bandHigh"))
        | Q(askingPrice__lt=F("bandLow"))
        | Q(bandHigh__isnull=True)
    )

    return {
        "price": flats.exclude(picHold).filter(askHold),
        "photos": flats.filter(picHold),
        "other": flats.exclude(picHold).exclude(askHold),
        "documents": flats.filter(documentRequests__status="submitted").distinct(),
    }


def heldFlats():
    return (
        addHolds(Listing.objects.filter(status="review"))
        .select_related("seller")
        .prefetch_related(
            Prefetch(
                "photos",
                queryset=ListingPhoto.objects.filter(doubtQ()).order_by(roomOrder(), "pk"),
                to_attr="doubtfulPhotos",
            ),
            Prefetch(
                "decisions",
                queryset=Decision.objects.filter(decision="documents").order_by("-createdAt"),
                to_attr="documentDecisions",
            ),
            Prefetch(
                "documentRequests",
                queryset=DocumentRequest.objects.prefetch_related("files").order_by(
                    "-createdAt", "-pk"
                ),
                to_attr="moderationDocuments",
            ),
        )
    )


def holdKind(row):
    flat = row["flat"]

    if flat.aiPhotos or flat.unsurePhotos or flat.uncheckedPhotos or flat.stalePhotos:
        return "photos"

    if not row["est"] or row.get("outsideAmount"):
        return "price"

    return "other"


@staffPage("admin_queue")
def queue_item(request, pk):
    flat = heldFlats().filter(pk=pk).first()

    if not flat:
        messages.error(request, "That listing is no longer waiting for review.")

        return redirect("admin_queue")

    from .access import riskParts, withRisk

    flat = withRisk(heldFlats()).filter(pk=pk).first()
    row = queueRow(flat, request.session.pop("queueError", None))
    row["hold"] = holdKind(row)
    row["riskParts"] = riskParts(flat)
    readyPics([flat])
    query = request.GET.copy()

    return page(
        request,
        "adminpanel/queue_item.html",
        {"r": row, "backQuery": query.urlencode()},
        section="admin_queue",
    )


@staffPage("admin_queue")
def queue(request):
    from .access import passSafe, withRisk

    autoNum = passSafe(heldFlats())

    if autoNum:
        messages.success(
            request,
            f"{autoNum} low-risk listing{'s' if autoNum > 1 else ''} went live automatically.",
        )

    if request.GET.get("sort") == "wait":
        sort = "wait"
    else:
        sort = "risk"

    flats = withRisk(heldFlats())

    if sort == "wait":
        flats = flats.order_by("reviewSince", "createdAt", "pk")
    else:
        flats = flats.order_by("-risk", "reviewSince", "pk")

    groups = queueSets(flats)

    whyNums = {
        "all": flats.count(),
        **{key: group.count() for key, group in groups.items()},
    }

    reason = request.GET.get("reason", "")

    if reason in groups:
        flats = groups[reason]
    else:
        reason = ""

    if request.GET.get("per") == "100":
        perPage = 100
    else:
        perPage = 25

    rows = Paginator(flats, perPage).get_page(request.GET.get("page"))
    rowList = []
    noReason = request.session.pop("queueError", None)

    for flat in rows:
        row = queueRow(flat, noReason)
        row["hold"] = holdKind(row)
        rowList.append(row)

    if noReason and not any(row["missingReason"] for row in rowList):
        messages.error(request, "Add a reason before you reject or ask for documents.")

    readyPics([row["flat"] for row in rowList])
    rows.object_list = rowList

    context = {
        "rows": rows,
        "log": Decision.objects.select_related("listing", "staff").order_by("-createdAt")[:15],
        "approvedCount": Decision.objects.filter(decision="approved").count(),
        "rejectedCount": Decision.objects.filter(decision="rejected").count(),
        "reason": reason,
        "reasonCounts": whyNums,
        "perPage": perPage,
        "sort": sort,
    }

    return page(request, "adminpanel/queue.html", context)


@staffPage("admin_queue")
def decide(request, pk):
    if request.method != "POST":
        return redirect("admin_queue")

    flat = Listing.objects.filter(pk=pk).first()

    if not flatFresh(request, flat):
        messages.error(
            request,
            "This listing changed since you reviewed it. Reload the queue and review the latest details. Nothing changed.",
        )

        return redirect("admin_queue")

    if request.POST.get("decision") == "approved":
        ready = planRedo(flat)
    else:
        ready = None

    return saveChoice(request, pk, ready)


@staffPage("admin_queue")
@require_POST
@transaction.atomic
def pic_choice(request, pk, photoId):
    from sellerpanel.photos import dropPhoto

    flat = get_object_or_404(Listing.objects.select_for_update(), pk=pk, status="review")

    photo = get_object_or_404(ListingPhoto, pk=photoId, listing=flat)
    back = reverse("admin_queue_item", args=[flat.pk]) + "#review-photos"
    room = photo.get_room_display().lower()

    if request.POST.get("action") == "keep":
        ListingPhoto.objects.filter(pk=photo.pk).update(
            staffReviewedBy=request.user,
            staffReviewedAt=timezone.now(),
            staffReviewedRevision=flat.revision,
        )

        audit(
            request,
            "photo kept",
            f"Listing #{flat.pk}",
            f"The {room} photo was checked and kept.",
        )

        messages.success(request, f"The {room} photo is kept.")

        return redirect(back)

    reason = REASONS.get(request.POST.get("reason", ""))

    if not reason:
        messages.error(request, "Choose why you’re removing this photo.")

        return redirect(back)

    dropPhoto(flat, photo)

    audit(
        request,
        "photo removed",
        f"Listing #{flat.pk}",
        f"The {room} photo was removed because {reason}.",
    )

    notify(
        flat.seller_id,
        "photo_refused",
        f"Our team removed the {room} photo from {_place(flat)} because {reason}. Please add a new one; your other photos are fine",
        reverse("edit_listing", args=[flat.publicId]),
    )

    messages.success(request, f"The {room} photo is removed and the seller is told why.")

    return redirect(back)


@staffPage("admin_reviews")
def reviews(request):
    rows = Review.objects.select_related(
        "seller", "orderItem__product", "orderItem__order"
    ).order_by("-createdAt", "-pk")

    low = request.GET.get("rating") == "low"
    recent = request.GET.get("days") == "30"

    if recent:
        rows = rows.filter(createdAt__gte=timezone.now() - timedelta(days=30))

    counts = {
        "all": rows.count(),
        "low": rows.filter(rating__lte=LOW_RATING).count(),
    }

    if low:
        rows = rows.filter(rating__lte=LOW_RATING)

    if low:
        if recent:
            noItems = "No low ratings in the last 30 days."
        else:
            noItems = "No low ratings."
    else:
        if recent:
            noItems = "No reviews in the last 30 days."
        else:
            noItems = "No reviews yet. Sellers can rate a service once its order is done."

    context = {
        "emptyText": noItems,
        "rows": rows,
        "low": low,
        "recent": recent,
        "lowRating": LOW_RATING,
        "counts": counts,
    }

    return page(request, "adminpanel/reviews.html", context)


@staffPage("admin_reviews")
def see_review(request, pk):
    review = (
        Review.objects.select_related("seller", "orderItem__product", "orderItem__order")
        .filter(pk=pk)
        .first()
    )

    if not review:
        messages.error(request, "That review no longer exists.")

        return redirect("admin_reviews")

    context = {
        "r": review,
        "low": request.GET.get("rating") == "low",
        "recent": request.GET.get("days") == "30",
        "lowRating": LOW_RATING,
    }

    return page(request, "adminpanel/review.html", context, section="admin_reviews")


@staffPage("admin_reviews")
@require_POST
@transaction.atomic
def flip_review(request, pk):
    review = Review.objects.select_for_update().select_related("orderItem").filter(pk=pk).first()

    back = reverse("admin_reviews")

    if request.POST.get("rating") == "low":
        back += "?rating=low"

    if request.POST.get("days") == "30":
        back += "&days=30" if "?" in back else "?days=30"

    if not review:
        messages.error(request, "That review no longer exists.")

        return redirect(back)

    wanted = request.POST.get("hide")

    if wanted not in ("0", "1") or (wanted == "1") == review.hidden:
        return redirect(back)

    reason = request.POST.get("reason", "").strip()

    if wanted == "1" and (not reason or len(reason) > 200):
        messages.error(request, "Add a reason of 200 characters or fewer to hide this review.")

        return redirect(back)

    review.hidden = wanted == "1"
    review.save(update_fields=["hidden"])

    audit(
        request,
        "review hidden" if review.hidden else "review shown",
        f"Review #{review.pk}",
        (reason if review.hidden else f"{review.orderItem.name} · {review.rating} out of 5"),
    )

    if review.hidden:
        from storefront.notices import notify

        notify(
            review.seller_id,
            "review_hidden",
            f"Our team hid your review of {review.orderItem.name}. Reason: {reason}",
            reverse("my_orders"),
        )

        messages.success(request, f"Review #{review.pk} is hidden from the Packages page.")
    else:
        messages.success(request, f"Review #{review.pk} is shown on the Packages page again.")

    return redirect(back)


def pricey_q():
    return (
        Q(
            estimatedPrice__isnull=False,
            bandHigh__isnull=False,
            askingPrice__gt=F("bandHigh"),
        )
        & ~Q(estimatedPrice=0)
        & ~Q(askingPrice=0)
    )


def add_pack(flats):
    newest = (
        goodPacks(timezone.now())
        .filter(listing_id=OuterRef("pk"))
        .order_by("-order__createdAt", "-pk")
        .values("name")[:1]
    )

    return flats.annotate(packageName=Subquery(newest))


def theSeller(request):
    try:
        return object_id(request.GET.get("seller"))
    except Exception as error:
        return None


def findFlats(request):
    flats = Listing.objects.select_related("seller")
    status = request.GET.get("status", "")

    if status in dict(Listing.STATUS_CHOICES):
        flats = flats.filter(status=status)

    town = request.GET.get("town", "")

    if town:
        flats = flats.filter(town=town)

    if request.GET.get("above") == "1":
        flats = flats.filter(pricey_q())

    term = request.GET.get("q", "").strip()

    if term.lower().startswith("blk"):
        term = term[3:].strip()

    if term:
        flats = flats.filter(Q(block__iexact=term) | Q(postalCode__startswith=term))

    seller = theSeller(request)

    if seller:
        flats = flats.filter(seller_id=seller)

    return add_pack(flats).order_by("-createdAt", "-pk")


def priceCheck(flat):
    flag_labels = {
        "above": "Above range",
        "typo": "Below range",
        None: "Fair price",
    }
    flag_tones = {"above": "amber", "typo": "bad", None: "good"}

    if not (flat.estimatedPrice and flat.bandLow and flat.bandHigh):
        return {"key": "none", "label": "Not checked", "tone": "brand"}

    key = flagReason(flat.askingPrice, flat.bandLow, flat.estimatedPrice, flat.bandHigh)

    return {"key": key or "fair", "label": flag_labels[key], "tone": flag_tones[key]}


@staffPage("admin_listings")
def listings(request):
    rows = Paginator(findFlats(request), 25).get_page(request.GET.get("page"))

    for flat in rows:
        flat.priceCheck = priceCheck(flat)

    readyPics(list(rows))
    ownerId = theSeller(request)
    status = request.GET.get("status", "")
    query = request.GET.copy()
    query.pop("page", None)

    context = {
        "rows": rows,
        "statuses": Listing.STATUS_CHOICES,
        "status": status,
        "statusLabel": dict(Listing.STATUS_CHOICES).get(status, ""),
        "towns": Listing.objects.order_by("town").values_list("town", flat=True).distinct(),
        "town": request.GET.get("town", ""),
        "above": request.GET.get("above") == "1",
        "q": request.GET.get("q", ""),
        "seller": User.objects.filter(pk=ownerId).first() if ownerId else None,
        "filtered": any(request.GET.get(key) for key in ("status", "town", "above", "q", "seller")),
        "returnQuery": query.urlencode(),
    }

    return page(request, "adminpanel/listings.html", context)


@staffPage("admin_listings")
def flat_view(request, pk):
    flat = add_pack(Listing.objects.select_related("seller").filter(pk=pk)).first()

    if not flat:
        messages.error(request, "That listing could not be found.")

        return redirect("admin_listings")

    flat.priceCheck = priceCheck(flat)
    readyPics([flat])
    context = {"flat": flat, "returnQuery": request.GET.urlencode()}

    return page(request, "adminpanel/listing.html", context, section="admin_listings")


def backToList(request, pk):
    if request.POST.get("next") == "page":
        return redirect("admin_listing", pk=pk)

    query = QueryDict(request.POST.get("returnQuery", "")).urlencode()

    return redirect(f"{reverse('admin_listings')}{'?' + query if query else ''}#listing-{pk}")


@staffPage("admin_listings")
def flat_do(request, pk):
    from adminpanel.orders import flatName

    actions = {
        "takedown": ("live",),
        "relist": ("hidden",),
        "review": ("live", "hidden", "rejected"),
    }

    if request.method != "POST":
        return redirect("admin_listings")

    action = request.POST.get("action")
    reason = request.POST.get("reason", "").strip()[:200]
    ownerId = Listing.objects.filter(pk=pk).values_list("seller_id", flat=True).first()
    checked = Listing.objects.filter(pk=pk, seller_id=ownerId).first()

    if checked and action == "relist":
        ready = planRedo(checked)
    else:
        ready = None

    with transaction.atomic():
        if ownerId:
            seller = lock_user(ownerId)
        else:
            seller = None

        flat = Listing.objects.select_for_update().filter(pk=pk, seller_id=ownerId).first()

        if not flat or action not in actions:
            messages.error(request, "That listing could not be found. Nothing changed.")

            return redirect("admin_listings")

        name = flatName(flat)

        if flat.revision != checked.revision or flat.status != checked.status:
            messages.error(
                request,
                "This listing changed during photo checks. Reload the page and try again. Nothing changed.",
            )

            return backToList(request, pk)

        if flat.status == "sold":
            messages.error(request, f"{name} is sold, so it cannot be changed.")

            return backToList(request, pk)

        if flat.status not in actions[action]:
            messages.error(
                request,
                f"{name} has already changed. Nothing changed, check its status and try again.",
            )

            return backToList(request, pk)

        if action == "takedown" and not reason:
            messages.error(request, f"Add a reason the seller will see, then take down {name}.")

            return backToList(request, pk)

        if action == "relist" and (seller is None or not seller.is_active):
            messages.error(
                request,
                f"The seller of {name} is suspended. Reactivate them before putting it back live.",
            )

            return backToList(request, pk)

        if action in ("relist", "review") and seller and otherOpen(seller, flat):
            messages.error(request, STAFF_ONE_FLAT_MESSAGE)

            return backToList(request, pk)

        target = f"Listing #{flat.pk} ({name})"

        if action == "takedown":
            flat.status = "hidden"
            logChoice(request.user, flat, "takendown", reason)

            notify(
                flat.seller_id,
                "listing_taken_down",
                f"Our team took down {name}: {reason}",
                reverse("my_listings") + f"#listing-{flat.publicId}",
            )

            audit(request, "listing taken down", target, reason)

            messages.success(request, f"{name} is taken down. Buyers can no longer see it.")
        elif action == "relist":
            problem = liveIssue(flat, checked.revision, checked.status, staffAccepted=True)

            picIssue = ""

            if not problem or problem == PHOTO_REVIEW_MESSAGE:
                picIssue = reviewPics(request, flat, ready)

            if picIssue:
                flat.status = "review"
                flat.save(update_fields=["status"])
                messages.error(request, picIssue)

                return backToList(request, pk)

            problem = liveIssue(flat, checked.revision, checked.status, staffAccepted=True)

            if problem:
                messages.error(request, problem)

                return backToList(request, pk)

            flat.status = "live"
            flat.photos.update(staffChecked=True)
            logChoice(request.user, flat, "approved", reason)

            notify(
                flat.seller_id,
                "listing_relisted",
                f"Our team put {name} back live. Buyers can see it again.",
                reverse("my_listings") + f"#listing-{flat.publicId}",
            )

            audit(request, "listing put back live", target, reason)
            messages.success(request, f"{name} is live again. Buyers can see it.")
        else:
            old = flat.get_status_display()
            flat.status = "review"

            audit(
                request,
                "listing sent to review",
                target,
                reason or f"Was {old.lower()}",
            )

            messages.success(request, f"{name} is in the review queue.")

        flat.save(update_fields=["status"])

    return backToList(request, pk)


@staffPage("admin_listings")
@transaction.atomic
def flat_edit(request, pk):
    from adminpanel.orders import flatName

    flat = (
        Listing.objects.select_for_update()
        .filter(pk=pk)
        .exclude(status__in=("sold", "withdrawn"))
        .first()
    )

    if not flat:
        messages.error(request, "That listing could not be found or is sold. Nothing changed.")

        return redirect("admin_listings")

    size = flat.floorAreaSqm

    if size == int(size):
        size = int(size)

    form = ListingDetailsForm(request.POST or None, instance=flat, initial={"floorAreaSqm": size})

    valid = request.method == "POST" and form.is_valid()

    if valid and form.cleaned_data["revision"] != flat.revision:
        form.add_error(
            None,
            "This listing changed in another tab or by the seller. Reload the page, check the latest details, then save again.",
        )

        valid = False

    if valid:
        changed = [name for name in form.changed_data if name != "revision"]
        flat = form.save(commit=False)

        if [name for name in changed if name != "askingPrice"]:
            try:
                flat.bandLow, flat.estimatedPrice, flat.bandHigh = band(
                    flat.town,
                    flat.flatType,
                    flat.flatModel,
                    flat.floorAreaSqm,
                    flat.storey,
                    flat.leaseCommenceYear,
                    postal=flat.postalCode,
                    block=flat.block,
                )

                flat.modelVersion = VERSION
            except ValueError:
                form.add_error(
                    None,
                    "The valuation service is unavailable. Nothing was saved. Try again later.",
                )

                flat.refresh_from_db()

                return page(
                    request,
                    "adminpanel/listing_form.html",
                    {
                        "form": form,
                        "flat": flat,
                        "returnQuery": request.POST.get("returnQuery", ""),
                    },
                    section="admin_listings",
                )

        flat.save(
            update_fields=[
                *ListingDetailsForm.Meta.fields,
                "bandLow",
                "estimatedPrice",
                "bandHigh",
                "modelVersion",
            ]
        )

        name = flatName(flat)

        if changed:
            labels = [form.fields[field].label.lower() for field in changed]

            audit(
                request,
                "listing edited",
                f"Listing #{flat.pk} ({name})",
                "Changed " + joinWords(labels),
            )

            if flat.seller_id:
                from storefront.notices import notify

                notify(
                    flat.seller_id,
                    "listing_edited",
                    f"Our team changed the {joinWords(labels)} on your listing for {name}. Please check it",
                    reverse("seller_listing", args=[flat.publicId]),
                )

            messages.success(request, f"{name} saved.")

        return backToList(request, pk)

    context = {"form": form, "flat": flat, "returnQuery": request.GET.get("back", "")}

    return page(request, "adminpanel/listing_form.html", context, section="admin_listings")
