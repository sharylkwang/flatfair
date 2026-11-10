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

WEEKS_SHOWN = 8
priceBandEvalPath = TRAINING / "price_band_eval.json"

ORDER_TYPES = [
    ("service", "Extra services"),
    ("package", "Packages only"),
]
REASONS = {
    "room": "it shows a different room",
    "ai": "it looks computer-made",
    "flat": "it does not show the flat",
    "quality": "it is too dark or blurry",
}
REPLY_CHOICES = [
    (
        "checking",
        "Looking into it",
        "Thanks for getting in touch. We are looking into this and will reply here when we have an update.",
    ),
    (
        "details",
        "Ask for details",
        "Could you share your listing or order number and tell us what happened? Please leave out passwords and payment card details.",
    ),
    (
        "resolved",
        "Check it worked",
        "We have made the requested update. Please check again and let us know if you still need help.",
    ),
]


def plural(n, word):
    if n == 1:
        return f"{n} {word}"

    return f"{n} {word}s"


def joinWords(words):
    words = list(words)

    if len(words) <= 2:
        return " and ".join(words)

    return ", ".join(words[:-1]) + " and " + words[-1]


def firstName(user):
    return (user.first_name or user.email or user.username).split()[0]


def mailto(email, subject, body=""):
    link = f"mailto:{email}?subject={quote(subject)}"

    if body:
        link += f"&body={quote(body)}"

    return link


def dateRange(request):
    dates = []
    problem = ""

    for key in ("from", "to"):
        raw = request.GET.get(key, "")

        try:
            if raw:
                value = parse_date(raw)
            else:
                value = None
        except Exception as error:
            value = None

        if raw and value is None:
            problem = (
                "Those dates did not work. Pick dates from the calendar and try again."
            )

        dates.append(value)

    start, end = dates

    if start and end and start > end:
        problem = "The start date must be on or before the end date. Showing every date instead."


        start = end = None

    return start, end, problem


def page(request, template, context=None, section=None):
    from adminpanel.orders import todoQ

    context = context or {}

    for key in ("rows", "orders", "people", "products", "team", "entries"):
        if key in context:
            if not hasattr(context[key], "paginator"):
                context[key] = Paginator(context[key], 25).get_page(
                    request.GET.get("page")
                )

            context["pageObj"] = context[key]

    query = request.GET.copy()
    query.pop("page", None)
    context["pageQuery"] = query.urlencode()

    if "query" in context:
        context["query"] = query.urlencode()

    context["menu"] = menu(request.user)
    context["staffRole"] = staffRole(request.user)
    context["section"] = section or request.resolver_match.url_name
    context["queueCount"] = Listing.objects.filter(status="review").count()
    ticketNum = Ticket.objects.exclude(status="resolved").count()

    reviewNum = Review.objects.filter(
        createdAt__gte=timezone.now() - timedelta(days=30)
    ).count()

    postNum = (
        Post.objects.filter(hidden=False, reports__resolved=False).distinct().count()
    )

    orderNum = 0

    from sellerpanel.reconciliation import payAlertQ

    payNum = Payment.objects.filter(payAlertQ()).count()


    if canOpen(request.user, "admin_orders"):
        orderNum = Order.objects.filter(todoQ()).distinct().count()

    from storefront.deals import stuckDeals

    counts = {
        "admin_deals": stuckDeals().count(),
        "admin_queue": context["queueCount"],
        "admin_orders": orderNum + payNum,
        "admin_tickets": ticketNum,
        "admin_reviews": reviewNum,
        "admin_community": postNum,
    }

    groups = MENU_GROUPS

    items = {
        item["page"]: {**item, "count": counts.get(item["page"])}
        for item in context["menu"]
    }


    context["menuGroups"] = [
        {
            "label": label,
            "items": [
                items[name]
                for name in names
                if name in items and not items[name]["more"]
            ],
        }
        for label, names in groups
        if any(name in items and not items[name]["more"] for name in names)
    ]

    context["moreMenu"] = [item for item in items.values() if item["more"]]


    return render(request, template, context)


def audit(request, action, target, detail=""):
    AuditLog.objects.create(
        actor=request.user, action=action, target=target[:80], detail=detail[:200]
    )


def user_tag(person):
    return f"Customer #{person.pk} ({person.email})"


def flatWeeks(today):
    monday = (today - timedelta(days=today.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    first = monday - timedelta(weeks=WEEKS_SHOWN - 1)

    rows = (
        Listing.objects.filter(createdAt__gte=first)
        .annotate(week=TruncWeek("createdAt"))
        .values("week")
        .annotate(count=Count("pk"))
    )

    counts = {}


    for row in rows:
        counts[row["week"].date()] = row["count"]

    weeks = []

    for number in range(WEEKS_SHOWN):
        start = first + timedelta(weeks=number)

        weeks.append(
            {"label": start.strftime("%-d %b"), "count": counts.get(start.date(), 0)}
        )

    busiest = max(week["count"] for week in weeks) or 1

    for week in weeks:
        week["height"] = round(week["count"] / busiest * 100)

    return weeks


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

    parts = [
        {"key": key, "label": label, "count": counts.get(key, 0)}
        for key, label in named
    ]

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


def itemSales():
    rows = list(
        OrderItem.objects.exclude(order__status="refunded")
        .values("name", "kind")
        .annotate(total=Sum("price"), sold=Count("id"))
        .order_by("-total")
    )

    for row in rows:
        row["width"] = (
            round(row["total"] / rows[0]["total"] * 100) if rows[0]["total"] else 0
        )


    return rows


def idleActions(user):
    idle = []

    for urlName, label, words in (
        ("admin_customers", "Find a customer", "look up a customer"),
        ("admin_orders", "View orders", "look through past orders"),
        ("admin_catalogue", "Open catalogue", "check the catalogue"),
    ):
        if canOpen(user, urlName) and len(idle) < 2:
            idle.append({"url": reverse(urlName), "label": label, "words": words})


    if len(idle) == 2:
        return (
            idle,
            f"Nothing waiting. You can {idle[0]['words']} or {idle[1]['words']}.",
        )

    if idle:
        return idle, f"Nothing waiting. You can {idle[0]['words']}."

    return idle, "Nothing waiting."


def hotTasks(waiting):
    from adminpanel.orders import refundQ, todoQ



    from storefront.deals import stuckDeals

    now = timezone.now()

    queries = [
        Listing.objects.filter(status="review"),
        Order.objects.filter(todoQ()).distinct(),
        Order.objects.filter(refundQ()).distinct(),
        Ticket.objects.exclude(status="resolved"),
        Post.objects.filter(hidden=False, reports__resolved=False).distinct(),
        Review.objects.filter(createdAt__gte=now - timedelta(days=30)),
        stuckDeals(),
    ]

    details = [
        (
            "home",
            "Review queue",
            "Check the oldest waiting flat first.",
            "admin_queue",
            "",
            "Open review queue",
        ),
        (
            "receipt",
            "Orders to act on",
            "Start or finish a seller’s service.",
            "admin_orders",
            "?status=needs",
            "Check orders",
        ),
        (
            "receipt",
            "Refunds waiting",
            "Check requests and refund eligible orders.",
            "admin_orders",
            "?status=needs&refunds=1",
            "Check refunds",
        ),
        (
            "support",
            "Open tickets",
            "Read the customer’s message and reply.",
            "admin_tickets",
            "",
            "Reply to tickets",
        ),
        (
            "chat",
            "Reported posts",
            "Check posts reported by the community.",
            "admin_community",
            "",
            "Check posts",
        ),
        (
            "star",
            "New reviews",
            "Read feedback received in the last 30 days.",
            "admin_reviews",
            "?days=30",
            "Read reviews",
        ),
        (
            "offer",
            "Stuck deals",
            "A step is more than 3 days late. Check in with the buyer and seller.",
            "admin_deals",
            "?tab=stuck",
            "Check deals",
        ),
    ]

    actions = []

    for rank, (query, detail) in enumerate(zip(queries, details)):
        icon, title, text, urlName, queryString, label = detail



        oldest = (
            query.order_by("createdAt", "pk")
            .values_list("createdAt", flat=True)
            .first()
        )

        actions.append(
            {
                "icon": icon,
                "title": title,
                "text": text,
                "count": query.count(),
                "url": reverse(urlName) + queryString,
                "label": label,
                "oldest": oldest or now,
                "priority": rank,
                "flat": waiting[0] if rank == 0 and waiting else None,
            }
        )

    actions.sort(
        key=lambda action: (-action["count"], action["oldest"], action["priority"])
    )



    return {"startActions": actions[:3], "alsoWaiting": actions[3:]}


def todoActions(tasks, paymentCount):
    todo = [
        action
        for action in tasks["startActions"] + tasks["alsoWaiting"]
        if action["count"]
    ]


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


def mainAction(canQueue, canTickets, attention):
    if canQueue:
        urlName, label = "admin_queue", "Open review queue"
    elif canTickets:
        urlName, label = "admin_tickets", "Open tickets"
    else:
        urlName, label = "admin_orders", "View orders"

    url = reverse(urlName)

    if any(row["url"].split("?")[0] == url for row in attention):
        url = None

    return url, label


# Show the admin dashboard
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
        toStart = (
            Order.objects.filter(status="paid", items__kind="addon").distinct().count()
        )

        doing = (
            Order.objects.filter(status="processing", items__kind="addon")
            .distinct()
            .count()
        )

    openHelp = Ticket.objects.none()



    if ticketOk:
        openHelp = Ticket.objects.exclude(status="resolved")

    ticketNum = openHelp.count()
    weekDone = 0

    if ticketOk:
        weekAgo = timezone.now() - timedelta(days=7)


        weekDone = Ticket.objects.filter(
            status="resolved", resolvedAt__gte=weekAgo
        ).count()

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

            checkText = (
                "Each one says why it was held. Approve, reject or ask for documents."
            )

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
            Listing.objects.filter(status="review").order_by(
                "reviewSince", "createdAt"
            )[:5]
        )

    readyPics(
        waiting + [item.listing for order in latest for item in order.items.all()]
    )

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
            for key, group in queueSets(
                addHolds(Listing.objects.filter(status="review"))
            ).items()
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


DELIVERY_TYPES = {**DOCUMENT_TYPES, ".zip": ("application/zip", b"PK", None)}


def peopleCan(people):
    return people.annotate(
        isSeller=(
            Exists(Profile.objects.filter(user_id=OuterRef("pk"), canSell=True))
            | Exists(Listing.objects.filter(seller_id=OuterRef("pk")))
            | Exists(Order.objects.filter(seller_id=OuterRef("pk")))
        ),
        isBuyer=(
            Exists(Profile.objects.filter(user_id=OuterRef("pk"), canBuy=True))
            | Exists(Offer.objects.filter(buyer_id=OuterRef("pk")))
            | Exists(ViewingBooking.objects.filter(buyer_id=OuterRef("pk")))
            | Exists(SavedListing.objects.filter(collection__user_id=OuterRef("pk")))
        ),
    )


def customerRoles(person):
    return ", ".join(
        label
        for enabled, label in ((person.isBuyer, "Buyer"), (person.isSeller, "Seller"))
        if enabled
    )


def findUsers(request):
    people = peopleCan(User.objects.filter(is_staff=False)).order_by("first_name", "pk")



    term = request.GET.get("q", "").strip()

    if term:
        people = people.filter(
            Q(first_name__icontains=term)
            | Q(last_name__icontains=term)
            | Q(email__icontains=term)
        )

    role = request.GET.get("role", "")

    if role in ("buyer", "seller"):
        people = people.filter(**{"isSeller" if role == "seller" else "isBuyer": True})

    if request.GET.get("state") == "suspended":
        people = people.filter(is_active=False)
    elif request.GET.get("state") == "active":
        people = people.filter(is_active=True)

    return people


@staffPage("admin_customers")
def user_list(request):
    context = {
        "people": findUsers(request).annotate(
            listingCount=Count("listings", distinct=True),
            orderCount=Count("orders", distinct=True),
        ),
        "q": request.GET.get("q", ""),
        "role": request.GET.get("role", ""),
        "state": request.GET.get("state", ""),
        "query": request.GET.urlencode(),
    }


    return page(request, "adminpanel/customers.html", context)


@staffPage("admin_customers")
@transaction.atomic
def see_user(request, pk):
    person = lock_user(pk)

    if person and person.is_staff:
        person = None

    if not person:
        return redirect("admin_customers")

    canDo = peopleCan(User.objects.filter(pk=person.pk)).get()



    sells = canDo.isSeller
    form = ContactForm(instance=person)

    if request.method == "POST":
        form = ContactForm(request.POST, instance=person)

        if form.is_valid():
            try:
                with transaction.atomic():
                    form.save()
            except IntegrityError:
                if (
                    not User.objects.exclude(pk=person.pk)
                    .filter(username=person.email)
                    .exists()
                ):
                    raise

                form.add_error("email", "Another account already uses this email.")
            else:
                audit(
                    request,
                    "customer edited",
                    user_tag(person),
                    "Contact details updated",
                )

                messages.success(request, "Contact details saved.")

                return redirect("admin_customer", pk=pk)


    history = (
        AuditLog.objects.filter(target__startswith=f"Customer #{person.pk} ")
        .select_related("actor")
        .order_by("-createdAt")[:10]
    )

    context = {
        "person": person,
        "form": form,
        "listings": person.listings.annotate(
            isPublic=Exists(Listing.objects.public().filter(pk=OuterRef("pk")))
        ).order_by("-createdAt"),
        "orders": person.orders.prefetch_related(
            "items__product", "items__listing"
        ).order_by("-createdAt"),
        "history": history,
        "tickets": person.tickets.order_by("-createdAt")[:10],
        "offers": person.offers.select_related("listing").order_by("-createdAt")[:10],
        "bookings": person.viewingBookings.select_related("slot__listing").order_by(
            "-slot__startsAt"
        )[:10],
        "canTickets": canOpen(request.user, "admin_tickets"),
        "canSuspend": True,
        "isSeller": sells,
        "customerRoles": customerRoles(canDo),
        "liveCount": person.listings.filter(status__in=["live", "review"]).count(),
        "customerMailto": mailto(person.email, "Your FlatFair account"),
    }

    return page(request, "adminpanel/customer.html", context, section="admin_customers")


@staffPage("admin_customers")
@transaction.atomic
def user_block(request, pk):
    if request.method != "POST":
        return redirect("admin_customers")

    person = lock_user(pk)

    if not person or person.is_staff:
        return redirect("admin_customers")

    reason = request.POST.get("reason", "").strip()
    wanted = request.POST.get("action")

    if wanted not in ("suspend", "reactivate"):
        return HttpResponseBadRequest("Choose suspend or reactivate.")

    if not reason or len(reason) > 200:
        messages.error(
            request, "Add a reason of 200 characters or fewer for this account change."
        )

        return redirect("admin_customer", pk=pk)

    sells = peopleCan(User.objects.filter(pk=person.pk)).get().isSeller

    if (wanted == "suspend" and not person.is_active) or (
        wanted == "reactivate" and person.is_active
    ):
        return redirect("admin_customer", pk=pk)

    if wanted == "suspend":
        person.is_active = False
        person.save(update_fields=["is_active"])


        hidden = person.listings.filter(status__in=["live", "review"]).update(
            status="hidden"
        )

        if sells:
            audit(
                request,
                "account suspended",
                user_tag(person),
                f"{reason} ({hidden} listings hidden)",
            )

            if hidden == 1:
                flatWord = "listing"
            else:
                flatWord = "listings"

            messages.success(
                request,
                f"{person.first_name} is suspended. {hidden} {flatWord} hidden.",
            )
        else:
            audit(request, "account suspended", user_tag(person), reason)

            messages.success(
                request, f"{person.first_name} is suspended and can no longer sign in."
            )
    else:
        person.is_active = True
        person.save(update_fields=["is_active"])
        Profile.objects.filter(user=person).update(reactivatedAt=timezone.now())

        hidden = (
            person.listings.select_for_update()
            .filter(status="hidden")
            .order_by("-createdAt", "-pk")
        )

        gotBack = 0

        if not openFlats(person).exists():
            flat = hidden.first()

            if flat:
                flat.status = "review"


                flat.save(update_fields=["status"])
                gotBack = 1

        if hidden.exists():
            messages.info(
                request,
                "Other listings remain hidden. " + STAFF_ONE_FLAT_MESSAGE,
            )

        if sells:
            audit(
                request,
                "account reactivated",
                user_tag(person),
                f"{reason} ({gotBack} listings sent to review)",
            )

            if gotBack == 1:
                flatWord = "listing"
            else:
                flatWord = "listings"



            messages.success(
                request,
                f"{person.first_name} is active again. {gotBack} {flatWord} back in review.",
            )
        else:
            audit(request, "account reactivated", user_tag(person), reason)

            messages.success(
                request, f"{person.first_name} is active again and can sign in."
            )

    return redirect("admin_customer", pk=pk)


@staffPage("admin_catalogue")
def catalogue(request):
    goods = Product.objects.annotate(
        sold=Count("orderItems", filter=~Q(orderItems__order__status="refunded"))
    ).order_by("kind", "price")

    return page(request, "adminpanel/catalogue.html", {"products": goods})


@staffPage("admin_catalogue")
@transaction.atomic
def item_edit(request, pk=None):
    if pk:
        product = Product.objects.select_for_update().filter(pk=pk).first()
    else:
        product = None

    if pk and not product:
        return redirect("admin_catalogue")

    form = ProductForm(request.POST or None, instance=product)

    if request.method == "POST" and form.is_valid():
        saved = form.save()

        audit(
            request,
            "product edited" if product else "product added",
            saved.name,
            f'${saved.price} · {"in the shop" if saved.isActive else "hidden"}',
        )

        messages.success(request, f"{saved.name} saved.")


        return redirect("admin_catalogue")

    return page(
        request,
        "adminpanel/product_form.html",
        {"form": form, "product": product},
        section="admin_catalogue",
    )


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


@staffPage("admin_catalogue")
@transaction.atomic
def item_flip(request, pk):
    product = Product.objects.select_for_update().filter(pk=pk).first()

    if request.method == "POST" and product:
        wanted = request.POST.get("show")

        if wanted not in ("0", "1") or (wanted == "1") == product.isActive:
            return redirect("admin_catalogue")

        product.isActive = wanted == "1"



        product.save()

        audit(
            request,
            "product shown" if product.isActive else "product hidden",
            product.name,
        )

        messages.success(
            request,
            f'{product.name} is {"back in" if product.isActive else "taken out of"} the shop.',
        )

    return redirect("admin_catalogue")


@staffPage("admin_staff")
@transaction.atomic
def staff(request):
    if request.method == "POST" and not lockStaff(request.user):
        return HttpResponseForbidden("Your staff access has changed.")

    form = StaffForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data

        try:
            with transaction.atomic():
                member = User.objects.create_user(
                    username=data["email"],
                    email=data["email"],
                    password=data["password"],
                    first_name=data["fullName"],
                    is_staff=True,
                )

                member.groups.add(Group.objects.get_or_create(name="Admin")[0])
        except IntegrityError:
            if not User.objects.filter(username=data["email"]).exists():
                raise

            form.add_error("email", "An account with this email already exists.")
        else:
            audit(request, "staff added", member.email, "Admin")

            messages.success(
                request, f"{member.first_name} can now sign in as an Admin."
            )

            return redirect("admin_staff")

    staffUsers = User.objects.filter(is_staff=True).order_by("first_name", "pk")
    team = Paginator(staffUsers, 25).get_page(request.GET.get("page"))



    context = {"form": form, "team": team, "rules": PASSWORD_RULES}

    return page(request, "adminpanel/staff.html", context)


def lockStaff(user):
    if connection.vendor == "sqlite":
        Group.objects.filter(name="Admin").update(name=F("name"))

    Group.objects.get_or_create(name="Admin")
    Group.objects.select_for_update().get(name="Admin")

    return User.objects.filter(pk=user.pk, is_staff=True, is_active=True).exists()


@staffPage("admin_staff")
@transaction.atomic
def staff_drop(request, pk):
    if request.method != "POST":
        return redirect("admin_staff")

    if not lockStaff(request.user):
        return HttpResponseForbidden("Your staff access has changed.")

    member = (
        User.objects.select_for_update()
        .filter(pk=pk, is_staff=True)
        .exclude(pk=request.user.pk)
        .first()
    )

    if (
        member
        and member.is_active
        and User.objects.filter(is_staff=True, is_active=True).count() <= 1
    ):
        messages.error(request, "You cannot remove the last active admin.")

        return redirect("admin_staff")

    if member:
        member.is_staff = False
        member.is_superuser = False
        member.is_active = False
        member.save()
        member.groups.remove(*Group.objects.filter(name="Admin"))
        audit(request, "staff removed", member.email)



        messages.success(request, f"{member.first_name} no longer has staff access.")

    return redirect("admin_staff")


@staffPage("admin_overview")
def account(request):
    from adminpanel.reports import auditRows

    form = ChangePasswordForm(request.user, request.POST or None)
    form.fields["new_password2"].label = "Type new password again"



    if request.method == "POST" and form.is_valid():
        form.save()
        update_session_auth_hash(request, form.user)
        audit(request, "password changed", request.user.email)


        tellChange(request, request.user)
        messages.success(request, "Password changed.")

        return redirect("admin_account")

    context = {
        "form": form,
        "rules": PASSWORD_RULES,
        "pages": menu(request.user),
        "recent": auditRows(
            list(AuditLog.objects.filter(actor=request.user).order_by("-createdAt")[:5])
        ),
    }

    return page(request, "adminpanel/account.html", context)


def priceFlagQ():
    return (
        Q(bandHigh__isnull=False, bandLow__isnull=False)
        & ~Q(askingPrice=0)
        & (Q(askingPrice__gt=F("bandHigh")) | Q(askingPrice__lt=F("bandLow") * 0.7))
    )


def bandCheck():
    try:
        report = json.loads(priceBandEvalPath.read_text(encoding="utf-8"))

        if report["schemaVersion"] != 1 or report["evaluationPeriod"] != {
            "start": "2025-11",
            "end": "2026-10",
        }:
            return None



        overall, towns = report["overall"], report["byTown"]

        if not isinstance(towns, dict) or not towns:
            return None

        shares = (
            "servedCoverage",
            "rawCoverage",
            "flaggedShare",
            "aboveShare",
            "typoShare",
        )


        amounts = (
            "meanWidthDollars",
            "meanWidthPercent",
            "mapePercent",
            "medianAbsolutePercentError",
        )

        for summary in [overall, *towns.values()]:
            if type(summary["count"]) is not int or summary["count"] <= 0:
                return None

            for key in (*shares, *amounts):
                value = summary[key]

                if (
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or value < 0
                ):
                    return None

                if key in shares and value > 1:
                    return None

        return {
            **overall,
            **{key: overall[key] * 100 for key in shares},
            "worstTowns": [
                {
                    "town": town,
                    "coverage": summary["servedCoverage"] * 100,
                    "count": summary["count"],
                }
                for town, summary in sorted(
                    towns.items(), key=lambda item: (item[1]["servedCoverage"], item[0])
                )[:5]
            ],
        }
    except Exception as error:
        return None


@staffPage("admin_price_model")
def price_page(request):
    checked = Listing.objects.exclude(estimatedPrice=None)

    seen = Decision.objects.filter(
        listing=OuterRef("listing"),
        decision="approved",
        createdAt__gte=OuterRef("createdAt"),
    )



    unseen = ListingPhoto.objects.filter(doubtQ(), listing__status="live").exclude(
        Exists(seen)
    )

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
            round(approved / (approved + rejected) * 100)
            if approved + rejected
            else None
        ),
        "photosChecked": ListingPhoto.objects.exclude(aiIrrelevant=None).count(),
        "photosDoubtful": ListingPhoto.objects.filter(
            doubtQ(), listing__status="review"
        ).count(),
        "liveDoubtful": unseen.count(),
        "liveFlats": (
            Listing.objects.filter(pk__in=unseen.values("listing"))
            .annotate(doubtful=Count("photos", filter=Q(photos__in=unseen)))
            .order_by("-doubtful", "town", "block")
        ),
    }

    return page(request, "adminpanel/price_model.html", context)




def replyLink(ticket):
    return mailto(ticket.email, f"Re: your FlatFair message #{ticket.pk}")


def add_thread(found):
    latest = TicketMessage.objects.filter(ticket=OuterRef("pk")).order_by(
        "-createdAt", "-pk"
    )

    return found.annotate(
        replyCount=Count("replies"),
        lastFromStaff=Subquery(latest.values("fromStaff")[:1]),
    )


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


@transaction.atomic
def staffReply(request, pk, body):
    ticket = Ticket.objects.select_for_update().get(pk=pk)

    if ticket.status == "resolved":
        return False

    TicketMessage.objects.create(
        ticket=ticket, author=request.user, body=body, fromStaff=True
    )



    if ticket.assignee_id is None:
        ticket.assignee = request.user
        ticket.save(update_fields=["assignee"])

    audit(request, "ticket replied", f"Ticket #{ticket.pk}", ticket.get_topic_display())

    if ticket.customer_id:
        url = reverse("my_ticket", args=[ticket.pk])

        notify(
            ticket.customer_id,
            "support_reply",
            f"Our support team replied to your message #{ticket.pk}.",
            url,
        )

    return True


@staffPage("admin_tickets")
def see_ticket(request, pk):
    ticket = get_object_or_404(
        Ticket.objects.select_related("customer", "assignee", "order").prefetch_related(
            "order__items"
        ),
        pk=pk,
    )

    replies = {key: body for key, label, body in REPLY_CHOICES}

    form = ReplyForm(
        request.POST if request.method == "POST" else None,
        initial={"body": replies.get(request.GET.get("reply"), "")},
    )


    if request.method == "POST" and not ticket.customer_id:
        messages.error(
            request,
            "This person was not signed in, so they cannot see replies here. Reply by email.",
        )

        return redirect("admin_ticket", ticket.pk)

    if request.method == "POST" and form.is_valid():
        if staffReply(request, ticket.pk, form.cleaned_data["body"]):
            messages.success(
                request, "Reply sent. The customer sees it in My messages."
            )
        else:
            messages.error(
                request,
                f"Ticket #{ticket.pk} is already resolved. Your reply was not sent.",
            )

        return redirect("admin_ticket", ticket.pk)


    context = {
        "ticket": ticket,
        "thread": ticket.replies.select_related("author").order_by("createdAt", "pk"),
        "form": form,
        "replyLink": replyLink(ticket),
        "replyChoices": REPLY_CHOICES,
    }

    return page(request, "adminpanel/ticket.html", context, section="admin_tickets")


@staffPage("admin_tickets")
@transaction.atomic
def ticket_do(request, pk):
    ticket = Ticket.objects.select_for_update().filter(pk=pk).first()


    action = request.POST.get("action")


    if request.method != "POST" or not ticket or action != "resolve":
        return redirect("admin_tickets")

    if ticket.status == "resolved":
        messages.error(
            request, f"Ticket #{ticket.pk} is already resolved. Nothing changed."
        )
    else:
        ticket.status, ticket.resolvedAt = "resolved", timezone.now()
        ticket.assignee = request.user
        ticket.save(update_fields=["status", "resolvedAt", "assignee"])

        audit(
            request,
            "ticket resolved",
            f"Ticket #{ticket.pk}",
            ticket.get_topic_display(),
        )

        if ticket.customer_id:
            notify(
                ticket.customer_id,
                "support_reply",
                f"Our team marked your message #{ticket.pk} as resolved.",
                reverse("my_ticket", args=[ticket.pk]),
            )

        messages.success(request, f"Ticket #{ticket.pk} resolved.")

    if request.POST.get("next") == "detail":
        return redirect("admin_ticket", ticket.pk)

    return redirect("admin_tickets")


# Apply a staff action
