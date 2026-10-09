from .models import AuditLog, Ticket, TicketMessage
from storefront.models import Deal, Order, Payment, Refund, lock_user
from django.db.models import Exists, OuterRef, Q
from django.http import HttpResponseBadRequest
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from sellerpanel.reconciliation import autoSync, endRefund, payAlertQ, syncPay
from .access import canOpen, staffPage
from .documents import checkFile
from django.utils.formats import date_format
from sellerpanel.stripepay import endSession, makeRefund
from sellerpanel.photos import fileGuard
from sellerpanel.billing import finishPay
from django.contrib import messages
from storefront.notices import notify
from storefront.presentation import readyPics
from django.shortcuts import redirect
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone
from django.db import transaction
from django.utils.http import url_has_allowed_host_and_scheme
from adminpanel.views import (
    DELIVERY_TYPES,
    ORDER_TYPES,
    audit,
    dateRange,
    firstName,
    joinWords,
    mailto,
    page,
)


@staffPage("admin_orders")
def orders(request):
    from sellerpanel.reconciliation import payAlertQ

    found, problem = findOrders(request)
    orderSet = Paginator(found, 25).get_page(request.GET.get("page"))

    for order in orderSet:
        addActions(order)

    readyPics([item.listing for order in orderSet for item in order.items.all()])

    context = {
        "orders": orderSet,
        "q": request.GET.get("q", ""),
        "attentionPayments": Payment.objects.filter(payAlertQ()).select_related("seller"),
        "statuses": Order.STATUS_CHOICES,
        "status": request.GET.get("status", "needs"),
        "types": ORDER_TYPES,
        "type": request.GET.get("type", ""),
        "start": request.GET.get("from", ""),
        "end": request.GET.get("to", ""),
        "dateProblem": problem,
        "query": request.GET.urlencode(),
    }

    return page(request, "adminpanel/orders.html", context)


@staffPage("admin_deals")
def deals(request):
    deal_tabs = [
        ("open", "In progress"),
        ("stuck", "Stuck"),
        ("complete", "Complete"),
        ("cancelled", "Cancelled"),
    ]

    from storefront.deals import STUCK_DAYS, progress, stuckDeals

    tab = request.GET.get("tab", "open")

    if tab not in dict(deal_tabs):
        tab = "open"

    base = Deal.objects.select_related("listing__seller", "buyer").prefetch_related("steps")

    stuck = stuckDeals()

    queries = {
        "open": base.filter(status="open"),
        "stuck": base.filter(pk__in=stuck.values("pk")),
        "complete": base.filter(status="complete"),
        "cancelled": base.filter(status="cancelled"),
    }

    counts = {}

    for key, query in queries.items():
        counts[key] = query.count()

    stuckIds = set(stuck.values_list("pk", flat=True))
    rows = []

    for deal in queries[tab].order_by("-createdAt", "-pk")[:100]:
        row = {**progress(deal)}
        row["stuck"] = deal.pk in stuckIds
        rows.append(row)

    tabs = []

    for key, label in deal_tabs:
        tabs.append((key, label, counts[key]))

    context = {"rows": rows, "tab": tab, "tabs": tabs, "stuckDays": STUCK_DAYS}

    return page(request, "adminpanel/deals.html", context)


def endUnpaid(request, payment):
    if payment.status != "pending":
        messages.error(request, "Only a checkout that was never paid can be closed.")

        return

    if (
        payment.method == "stripe"
        and payment.providerSessionId
        and endSession(payment.providerSessionId) != "expired"
    ):
        messages.error(
            request,
            "Stripe would not close this checkout. It may have just been paid, so press Check with Stripe.",
        )

        return

    with transaction.atomic():
        lock_user(payment.seller_id)
        payment = Payment.objects.select_for_update().get(pk=payment.pk)

        if payment.status != "pending":
            return

        payment.status = "failed"
        payment.needsAttention = False

        payment.snapshot = {
            **payment.snapshot,
            "providerStatus": "expired / unpaid",
            "closedByStaff": True,
        }

        payment.save()
        audit(request, "payment closed", f"Payment #{payment.pk}", "Closed as unpaid.")

        payNote(
            payment,
            "Your checkout was closed because it wasn’t finished. No money was taken.",
        )

    messages.success(request, f"Payment #{payment.pk} was closed as unpaid. No money was taken.")


