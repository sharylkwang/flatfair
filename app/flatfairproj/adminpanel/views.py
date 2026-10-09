import json
import math
from datetime import timedelta
from decimal import Decimal
from urllib.parse import quote
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.models import User, Group
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db import connection, transaction, IntegrityError
from django.db.models import Sum, Count, Q, F, OuterRef, Subquery, Exists
from django.db.models.functions import TruncWeek
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST
from accounts.forms import ChangePasswordForm
from accounts.tokens import PASSWORD_RULES
from accounts.views import tellChange
from forum.models import Post
from sellerpanel.photocheck import doubtQ
from sellerpanel.photos import readPhoto
from storefront.models import (
    lock_user, Listing, ListingPhoto, Offer,
    Order, OrderItem, Payment, Product,
    Profile, Review, SavedListing, ViewingBooking,
)
from storefront.presentation import alertRow, helloFor, readyPics
from storefront.listing_rules import STAFF_ONE_FLAT_MESSAGE, openFlats
from sellerpanel.recommend import tipStats
from storefront.notices import notify
from storefront.valuation import pastCheck, modelInfo
from pretrained import TRAINING
from .documents import DOCUMENT_TYPES
from .access import MENU_GROUPS, staffPage, staffRole, canOpen, menu
from .forms import ProductForm, ContactForm, StaffForm, ReplyForm
from .models import Decision, AuditLog, DocumentRequest, Ticket, TicketMessage

REASONS = {
    "room": "it shows a different room",
    "ai": "it looks computer-made",
    "flat": "it does not show the flat",
    "quality": "it is too dark or blurry",
}
def nice_top(value):
    value = float(value)

    if value <= 0:
        return 1

    power = 10 ** math.floor(math.log10(value))

    for step in (1, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if value <= step * power:
            if step * power < 1:
                return step * power

            return int(step * power)

    return int(10 * power)


def shortMoney(amount):
    amount = float(amount)

    if amount >= 1000:
        return f"${amount / 1000:.1f}k".replace(".0k", "k")

    return f"${amount:,.0f}"


def sizeBars(points, key):
    top = nice_top(max(point[key] for point in points))
    peak = max(points, key=lambda point: point[key])

    for point in points:
        point["height"] = round(float(point[key]) / top * 100, 1)
        point["shown"] = point is peak or point is points[-1]

    return top


def salesWeeks(today):
    monday = (today - timedelta(days=today.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    first = monday - timedelta(weeks=WEEKS_SHOWN - 1)

    rows = (
        Order.objects.exclude(status="refunded")
        .filter(createdAt__gte=first)
        .annotate(week=TruncWeek("createdAt"))
        .values("week")
        .annotate(total=Sum("subtotal"))
    )

    totals = {row["week"].date(): row["total"] for row in rows}
    weeks = []

    for number in range(WEEKS_SHOWN):
        start = first + timedelta(weeks=number)
        total = totals.get(start.date(), Decimal("0"))

        weeks.append(
            {
                "label": start.strftime("%-d %b"),
                "total": total,
                "money": shortMoney(total),
            }
        )

    return weeks


def flatShares():
    counts = dict(Listing.objects.values_list("status").annotate(count=Count("pk")))
    total = sum(counts.values())

    named = [
        ("live", "Live"),
        ("review", "In review"),
        ("paused", "Paused"),
        ("sold", "Sold"),
    ]

    parts = [{"key": key, "label": label, "count": counts.get(key, 0)} for key, label in named]

    parts.append(
        {
            "key": "other",
            "label": "Hidden, rejected or deleted",
            "count": total - sum(part["count"] for part in parts),
        }
    )

    for part in parts:
        part["share"] = round(part["count"] / total * 100, 1) if total else 0

    return [part for part in parts if part["count"]], total


def monthNums(now):
    paid = Order.objects.exclude(status="refunded")
    month = paid.filter(createdAt__gte=now - timedelta(days=30))

    before = paid.filter(
        createdAt__gte=now - timedelta(days=60), createdAt__lt=now - timedelta(days=30)
    )

    return {
        "sales": month.aggregate(total=Sum("subtotal"))["total"] or 0,
        "salesBefore": before.aggregate(total=Sum("subtotal"))["total"] or 0,
        "orders": month.count(),
        "ordersBefore": before.count(),
    }


def todoActions(tasks, paymentCount):
    todo = [action for action in tasks["startActions"] + tasks["alsoWaiting"] if action["count"]]

    if paymentCount:
        todo.append(
            {
                "title": "Payments to check",
                "text": "Check the payment before placing or refunding a purchase.",
                "count": paymentCount,
                "url": reverse("admin_payments"),
                "label": "Check payments",
                "priority": 9,
            }
        )

    return todo


@staffPage("admin_overview")
def overview(request):
    from adminpanel.moderation import addHolds, queueSets
    from adminpanel.orders import refundQ

    user = request.user
    hello = helloFor(user)
    paid = Order.objects.exclude(status="refunded")
    orderNum = paid.count()
    addons = paid.filter(items__kind="addon").distinct().count()
    queueOk = canOpen(user, "admin_queue")
    orderOk = canOpen(user, "admin_orders")
    ticketOk = True
    reviewNum = Listing.objects.filter(status="review").count()
    liveNum = Listing.objects.filter(status="live").count()
    userNum = User.objects.filter(is_staff=False).count()
    toStart = 0
    doing = 0

    if orderOk:
        toStart = Order.objects.filter(status="paid", items__kind="addon").distinct().count()

        doing = Order.objects.filter(status="processing", items__kind="addon").distinct().count()

    openHelp = Ticket.objects.none()

    if ticketOk:
        openHelp = Ticket.objects.exclude(status="resolved")

    ticketNum = openHelp.count()
    weekDone = 0

    if ticketOk:
        weekAgo = timezone.now() - timedelta(days=7)

        weekDone = Ticket.objects.filter(status="resolved", resolvedAt__gte=weekAgo).count()

    refundNum = Order.objects.filter(refundQ()).distinct().count()
    payNum = 0
    sentNum = 0
    alerts = []

    if refundNum:
        alerts.append(
            alertRow(
                "receipt",
                "sky",
                f"{plural(refundNum, 'refund')} waiting",
                "Check the request and refund the order if eligible.",
                reverse("admin_orders") + "?status=needs&refunds=1",
                "Check refunds",
            )
        )

    status = []

    if queueOk:
        status.append(f"{plural(reviewNum, 'listing')} waiting for review")

        if reviewNum:
            sentNum = DocumentRequest.objects.filter(
                status="submitted", listing__status="review"
            ).count()

            checkText = "Each one says why it was held. Approve, reject or ask for documents."

            if sentNum:
                checkText = f"{plural(sentNum, 'document submission')} ready to check. Accept or ask the seller to upload again."

            alerts.append(
                alertRow(
                    "check",
                    "lilac",
                    f"{plural(reviewNum, 'listing')} in the review queue",
                    checkText,
                    reverse("admin_queue"),
                    "Open review queue",
                )
            )

    if orderOk:
        status.append(f"{plural(toStart + doing, 'order')} to work on")

        if toStart:
            alerts.append(
                alertRow(
                    "receipt",
                    "sky",
                    f"{plural(toStart, 'paid service order')} to start",
                    "A seller paid for an extra service. Each order says what to do first.",
                    reverse("admin_orders") + "?status=paid&type=service",
                    "Start orders",
                )
            )

        if doing:
            alerts.append(
                alertRow(
                    "clock",
                    "periwinkle",
                    f"{plural(doing, 'service order')} processing",
                    "Press Mark completed once the service is finished.",
                    reverse("admin_orders") + "?status=processing&type=service",
                    "Finish orders",
                )
            )

        from sellerpanel.reconciliation import payAlertQ

        payNum = Payment.objects.filter(payAlertQ()).count()

        if payNum:
            alerts.append(
                alertRow(
                    "receipt",
                    "lilac",
                    f"{plural(payNum, 'payment')} needing attention",
                    "Check payment status before placing or refunding a purchase.",
                    reverse("admin_payments"),
                    "Check payments",
                )
            )

    if ticketOk:
        status.append(plural(ticketNum, "open ticket"))

        if ticketNum:
            alerts.append(
                alertRow(
                    "support",
                    "sky",
                    plural(ticketNum, "open ticket"),
                    "Reply to the customers who are waiting.",
                    reverse("admin_tickets"),
                    "Open tickets",
                )
            )

    status.append(plural(liveNum, "live listing"))

    if not orderOk:
        status.append(plural(userNum, "customer"))

    idle, idleNote = idleActions(user)
    jobUrl, jobName = mainAction(queueOk, ticketOk, alerts)
    latest = []

    if orderOk:
        latest = (
            Order.objects.select_related("seller", "payment")
            .prefetch_related("items__listing")
            .order_by("-createdAt")[:5]
        )

    waiting = []

    if queueOk:
        waiting = list(
            Listing.objects.filter(status="review").order_by("reviewSince", "createdAt")[:5]
        )

    readyPics(waiting + [item.listing for order in latest for item in order.items.all()])

    weeks = flatWeeks(timezone.localtime())
    weekMax = sizeBars(weeks, "count")
    sales = salesWeeks(timezone.localtime())
    saleMax = sizeBars(sales, "total")
    shares, flatNum = flatShares()
    tasks = hotTasks(waiting)

    for action in tasks["startActions"] + tasks["alsoWaiting"]:
        if action["title"] == "Orders to act on" and toStart + doing:
            action["text"] = f"{toStart} to start · {doing} in progress"

    context = {
        "greeting": hello,
        "packageRecommendationStats": tipStats(),
        "dashboardStatus": " · ".join(status) + ".",
        "dashboardActionUrl": jobUrl,
        "dashboardActionLabel": jobName,
        "attentionRows": alerts,
        **tasks,
        "todoActions": todoActions(tasks, payNum),
        "submittedCount": sentNum,
        "idleActions": idle,
        "idleMessage": idleNote,
        "revenue": paid.aggregate(total=Sum("subtotal"))["total"] or 0,
        "attachRate": round(addons / orderNum * 100) if orderNum else 0,
        "byProduct": itemSales(),
        "weeks": weeks,
        "weeksTop": weekMax,
        "weeksHalf": weekMax / 2 if weekMax % 2 else weekMax // 2,
        "salesWeeks": sales,
        "salesTop": shortMoney(saleMax),
        "salesHalf": shortMoney(saleMax / 2),
        "statusShares": shares,
        "listingTotal": flatNum,
        "month": monthNums(timezone.now()),
        "liveCount": liveNum,
        "reviewCount": reviewNum,
        "reasonCounts": {
            key: group.count()
            for key, group in queueSets(addHolds(Listing.objects.filter(status="review"))).items()
        },
        "ticketCount": ticketNum,
        "resolvedThisWeek": weekDone,
        "userCount": userNum,
        "recentOrders": latest,
        "waiting": waiting,
        "openTickets": openHelp.order_by("createdAt")[:5],
        "canQueue": queueOk,
        "canOrders": orderOk,
        "canTickets": ticketOk,
        "canSeeRevenue": orderOk,
    }

    return page(request, "adminpanel/overview.html", context)


STALE_PHOTO = "This photo check is out of date and must be rerun before publication."


# Save a listing decision


@staffPage("admin_catalogue")
@require_POST
@transaction.atomic
def item_pic(request, pk):
    product = Product.objects.select_for_update().filter(pk=pk).first()

    if not product:
        return redirect("admin_catalogue")

    old = product.image.name

    if request.POST.get("remove"):
        if old:
            product.image = ""
            product.save(update_fields=["image"])
            transaction.on_commit(lambda: default_storage.delete(old))
            audit(request, "product photo removed", product.name, "")
            messages.success(request, f"Photo removed from {product.name}.")

        return redirect("admin_product_edit", product.pk)

    upload = request.FILES.get("image")

    if not upload:
        messages.error(request, "Choose a photo first.")

        return redirect("admin_product_edit", product.pk)

    data, problem = readPhoto(upload)

    if problem:
        messages.error(request, problem)

        return redirect("admin_product_edit", product.pk)

    product.image.save("photo.jpg", ContentFile(data), save=False)
    product.save(update_fields=["image"])

    if old:
        transaction.on_commit(lambda: default_storage.delete(old))

    audit(request, "product photo changed", product.name, "")
    messages.success(request, f"Photo saved for {product.name}.")

    return redirect("admin_product_edit", product.pk)


@staffPage("admin_price_model")
def price_page(request):
    checked = Listing.objects.exclude(estimatedPrice=None)

    seen = Decision.objects.filter(
        listing=OuterRef("listing"),
        decision="approved",
        createdAt__gte=OuterRef("createdAt"),
    )

    unseen = ListingPhoto.objects.filter(doubtQ(), listing__status="live").exclude(Exists(seen))

    flagged = Decision.objects.filter(
        priceFlagQ(), staff__isnull=False, decision__in=["approved", "rejected"]
    ).aggregate(
        approved=Count("pk", filter=Q(decision="approved")),
        rejected=Count("pk", filter=Q(decision="rejected")),
    )

    approved, rejected = flagged["approved"], flagged["rejected"]
    info = modelInfo()

    if info.get("month"):
        info["month"] = parse_date(f"{info['month']}-01")

    context = {
        "modelInfo": info,
        "bandEval": bandCheck(),
        "weakestSubgroups": (pastCheck() or {}).get("weakestSubgroups", []),
        "checkedCount": checked.count(),
        "liveCount": Listing.objects.filter(status="live").count(),
        "reviewCount": Listing.objects.filter(status="review").count(),
        "automaticCount": Decision.objects.filter(
            staff__isnull=True,
            decision="approved",
            askingPrice__gt=F("bandHigh"),
            askingPrice__lte=F("bandHigh") * Decimal("1.05"),
        ).count(),
        "decisionCount": approved + rejected,
        "approvedCount": approved,
        "rejectedCount": rejected,
        "overrideRate": (
            round(approved / (approved + rejected) * 100) if approved + rejected else None
        ),
        "photosChecked": ListingPhoto.objects.exclude(aiIrrelevant=None).count(),
        "photosDoubtful": ListingPhoto.objects.filter(doubtQ(), listing__status="review").count(),
        "liveDoubtful": unseen.count(),
        "liveFlats": (
            Listing.objects.filter(pk__in=unseen.values("listing"))
            .annotate(doubtful=Count("photos", filter=Q(photos__in=unseen)))
            .order_by("-doubtful", "town", "block")
        ),
    }

    return page(request, "adminpanel/price_model.html", context)




@staffPage("admin_tickets")
def tickets(request):
    found = add_thread(Ticket.objects.select_related("customer", "assignee", "order"))

    columns = [
        ("open", "Open", found.filter(status="open").order_by("createdAt")),
        (
            "resolved",
            "Resolved",
            found.filter(status="resolved").order_by("-resolvedAt")[:10],
        ),
    ]

    board = []

    for key, title, rows in columns:
        cards = list(rows)

        if key == "open":
            cards.sort(key=lambda ticket: bool(ticket.lastFromStaff))

        for ticket in cards:
            ticket.replyLink = replyLink(ticket)

        board.append({"key": key, "title": title, "tickets": cards})

    return page(request, "adminpanel/tickets.html", {"columns": board})


