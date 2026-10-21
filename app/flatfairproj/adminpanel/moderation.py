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


