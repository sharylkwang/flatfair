import logging
from datetime import timedelta
from decimal import Decimal
from django.core.cache import caches
from django.db import transaction
from django.utils import timezone
from .models import Listing, Order
from .valuation import flagReason

OPEN_LISTING_STATUSES = ("live", "review", "paused")
ONE_FLAT_MESSAGE = "You can sell one flat at a time. Mark your other flat as sold, or delete it, before you list a new one."
STAFF_ONE_FLAT_MESSAGE = "This seller already has an open flat. Mark it as sold before reopening another listing."
PHOTO_REVIEW_MESSAGE = "Photo checks need staff review before this listing can go live."
def openFlats(user):
    return Listing.objects.filter(seller=user, status__in=OPEN_LISTING_STATUSES)


def otherOpen(user, flat=None):
    others = openFlats(user)

    if flat is not None:
        others = others.exclude(pk=flat.pk)

    return others.exists()


def liveIssue(flat, expectedRevision, expectedStatus, staffAccepted=False, acceptingDocument=None):
    from sellerpanel.photocheck import doubtQ, manualQ, missingQ

    if (
        flat.revision != expectedRevision
        or flat.status != expectedStatus
        or flat.status not in ("live", "review", "hidden", "sold")
    ):
        return "This listing changed since it was reviewed. Reload the page and try again. Nothing changed."

    docs = flat.documentRequests.order_by("-createdAt", "-pk").first()

    if (
        docs
        and docs.status != "accepted"
        and not (docs.pk == acceptingDocument and docs.status == "submitted")
    ):
        return "Review and accept the submitted documents before approving this listing."

    if flat.photos.filter(missingQ()).exclude(manualQ(flat.revision)).exists() or (
        not staffAccepted and flat.photos.filter(doubtQ()).exists()
    ):
        return PHOTO_REVIEW_MESSAGE

    return ""


