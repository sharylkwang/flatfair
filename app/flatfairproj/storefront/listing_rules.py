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
AUTO_APPROVE_PRICE_GAP = Decimal("0.05")
AUTO_COMPLETE_DAYS = 7
AUTO_COMPLETE_KEY = "orders:auto-complete"
ABOVE_RANGE_NOTE = "The price is above the usual range."
log = logging.getLogger("storefront.automation")


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


def heldByCost(flat):
    from accounts.verification import emailVerified
    from adminpanel.models import AuditLog
    from .models import Profile
    from .valuation import flagReason

    if flat.status != "review" or not all((flat.bandLow, flat.estimatedPrice, flat.bandHigh)):
        return False

    if flagReason(flat.askingPrice, flat.bandLow, flat.estimatedPrice, flat.bandHigh) != "above":
        return False

    profile = (
        Profile.objects.select_related("user").filter(user_id=flat.seller_id, canSell=True).first()
    )

    if (
        not profile
        or not profile.user.is_active
        or profile.user.is_staff
        or not emailVerified(profile.user)
    ):
        return False

    if liveIssue(flat, flat.revision, "review"):
        return False

    if flat.documentRequests.exclude(status="accepted").exists():
        return False

    latest = flat.decisions.order_by("-createdAt", "-pk").first()

    if latest and latest.decision != "approved":
        return False

    if latest:
        seenAt = latest.createdAt
    else:
        seenAt = flat.createdAt

    if AuditLog.objects.filter(
        action="listing sent to review",
        target__startswith=f"Listing #{flat.pk} (",
        createdAt__gte=seenAt,
    ).exists():
        return False

    return not profile.reactivatedAt or profile.reactivatedAt < seenAt


def smallPriceGap(flat):
    if not all((flat.bandLow, flat.estimatedPrice, flat.bandHigh)):
        return False

    return (
        flagReason(flat.askingPrice, flat.bandLow, flat.estimatedPrice, flat.bandHigh) == "above"
        and 0 < flat.askingPrice - flat.bandHigh <= Decimal(flat.bandHigh) * AUTO_APPROVE_PRICE_GAP
    )


def passPrice(flat, wasHeld=None):
    from adminpanel.models import AuditLog, Decision

    if flat.status != "review" or not all((flat.bandLow, flat.estimatedPrice, flat.bandHigh)):
        return False

    flag = flagReason(flat.askingPrice, flat.bandLow, flat.estimatedPrice, flat.bandHigh)

    if flat.askingPrice < flat.bandLow or (flag and not smallPriceGap(flat)):
        return False

    if wasHeld is None:
        wasHeld = heldByCost(flat)

    if not wasHeld or liveIssue(flat, flat.revision, "review"):
        return False

    flat.status = "live"
    flat.save(update_fields=["status"])

    reason = (
        "Price is at most 5% above the usual range; photo and document checks passed."
        if flag
        else "Price hold cleared; price is within the usual range and photo and document checks passed."
    )

    Decision.objects.create(
        listing=flat,
        staff=None,
        decision="approved",
        reason=reason,
        askingPrice=flat.askingPrice,
        estimatedPrice=flat.estimatedPrice,
        bandLow=flat.bandLow,
        bandHigh=flat.bandHigh,
        modelVersion=flat.modelVersion,
    )

    AuditLog.objects.create(
        actor=None,
        action="listing auto-approved",
        target=f"Listing #{flat.pk} ({flat.town.title()})",
        detail=reason,
    )

    return True


@transaction.atomic
def closeOrder(orderId, seller=None):
    from adminpanel.models import AuditLog

    order = Order.objects.select_for_update().filter(pk=orderId, status="processing").first()

    if not order or (seller is not None and (order.seller_id != seller.pk or not seller.is_active)):
        return False

    items = list(order.items.select_for_update().filter(kind="addon"))
    cutoff = timezone.now() - timedelta(days=AUTO_COMPLETE_DAYS)

    if not items or any(
        not item.deliveredAt or not (item.deliveryFile or item.deliveryNote) for item in items
    ):
        return False

    expired = all(item.deliveredAt <= cutoff for item in items)

    if seller is None and not expired:
        return False

    order.status = "completed"
    order.save(update_fields=["status"])
    reason = (
        "Seller confirmed receipt of all deliveries"
        if seller is not None
        else f"{AUTO_COMPLETE_DAYS} days since delivery"
    )
    change = (
        "Processing → Completed" if seller is not None else "Processing → Completed automatically"
    )

    AuditLog.objects.create(
        actor=seller,
        action="order status",
        target=f"Order #{order.pk}",
        detail=f"{change}: {reason}.",
    )

    return True


def closeSent():
    orders = (
        Order.objects.filter(
            status="processing",
            items__kind="addon",
            items__deliveredAt__lte=timezone.now() - timedelta(days=AUTO_COMPLETE_DAYS),
        )
        .values_list("pk", flat=True)
        .distinct()
    )

    closed = 0

    for order_id in orders:
        try:
            closed += closeOrder(order_id)
        except Exception:
            log.exception("Could not complete delivered order %s", order_id)

    return closed


def autoClose():
    if caches["resources"].add(AUTO_COMPLETE_KEY, True, (600)):
        return closeSent()

    return 0
