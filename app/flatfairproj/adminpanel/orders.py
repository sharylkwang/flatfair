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


def flatName(flat):
    if flat.block:
        return f"Blk {flat.block} {flat.town.title()}"

    return flat.town.title()


def orderMoves(order):
    transitions = {
        "paid": ("processing", "refunded"),
        "processing": ("completed",),
        "completed": (),
        "refunded": (),
    }

    items = list(order.items.all())
    packages = any(item.kind == "package" for item in items)
    services = any(item.kind == "addon" for item in items)
    allowed = transitions.get(order.status, ())

    if Refund.objects.filter(order=order, status="pending").exists():
        allowed = [value for value in allowed if value == "refunded"]

    if packages and not services:
        allowed = ()

    if not order.canRefund():
        allowed = [value for value in allowed if value != "refunded"]

    return [(value, label) for value, label in Order.STATUS_CHOICES if value in allowed]


def refundQuestion(order):
    items = list(order.items.all())
    names = joinWords(dict.fromkeys(item.name or item.product.name for item in items))
    who = firstName(order.seller)

    return f"Refund ${order.total:,.2f} to {who} for {names}?"


def jobGuide(order):
    guide = {
        "Pro photography": (
            "You book a photographer and email the seller a time.",
            "Upload the photos as a ZIP and mark it completed.",
        ),
        "Floor plan": (
            "You email the seller for their HDB layout so the plan can be drawn.",
            "Upload the PDF and mark it completed.",
        ),
        "Viewing concierge": (
            "You book a host and email the seller the viewing time.",
            "Add a note on how the viewing went and mark it completed.",
        ),
    }

    steps = []

    for item in order.items.all():
        if item.kind != "addon":
            continue

        start, finish = guide.get(
            item.name,
            (
                (
                    "You email the seller to arrange the service.",
                    "Add a note or a file for the seller and mark it completed.",
                )
            ),
        )

        if order.status == "paid":
            steps.append({"name": item.name, "text": start})
        elif order.status == "processing":
            steps.append({"name": item.name, "text": finish})

    return steps


def addActions(order):
    work_actions = {
        "processing": "Start work",
        "completed": "Mark completed",
    }

    moves = dict(orderMoves(order))

    order.workActions = [
        {"status": value, "label": label} for value, label in work_actions.items() if value in moves
    ]

    order.refundAsk = refundQuestion(order) if "refunded" in moves else ""
    order.guide = jobGuide(order)
    order.hasServices = any(item.kind == "addon" for item in order.items.all())

    return order


def refundQ():
    return Q(refunds__status="pending") | Q(tickets__topic="refund", tickets__status="open")


def todoQ():
    return Q(status__in=["paid", "processing"], items__kind="addon") | refundQ()


def findOrders(request):
    orders = Order.objects.select_related("seller", "payment").prefetch_related(
        "items__product", "items__listing"
    )

    orders = orders.order_by("-createdAt", "-pk")
    term = request.GET.get("q", "").strip()

    if term:
        search = (
            Q(seller__email__icontains=term)
            | Q(seller__first_name__icontains=term)
            | Q(seller__last_name__icontains=term)
        )

        number = term.removeprefix("#")

        if number.isascii() and number.isdigit() and len(number) <= 18:
            search |= Q(pk=int(number))

        orders = orders.filter(search)

    status = request.GET.get("status", "needs")

    if status == "needs":
        orders = orders.filter(todoQ()).distinct()

    if request.GET.get("refunds") == "1":
        orders = orders.filter(refundQ()).distinct()

    if status in dict(Order.STATUS_CHOICES):
        orders = orders.filter(status=status)

    kind = request.GET.get("type", "")

    if kind == "service":
        orders = orders.filter(items__kind="addon").distinct()
    elif kind == "package":
        orders = orders.exclude(items__kind="addon")

    start, end, problem = dateRange(request)

    if start:
        orders = orders.filter(createdAt__date__gte=start)

    if end:
        orders = orders.filter(createdAt__date__lte=end)

    return orders, problem


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


def saveFiles(request, order, savedFiles):
    extras = list(order.items.filter(kind="addon"))

    for item in extras:
        note = request.POST.get(f"note-{item.pk}", "").strip()
        upload = request.FILES.get(f"file-{item.pk}")

        if not note and not upload and not item.deliveryNote and not item.deliveryFile:
            return f"Add a note or a file for {item.name} before you mark it completed."

        if upload:
            try:
                checkFile(upload, DELIVERY_TYPES, (20) * 1024 * 1024)
            except ValidationError as error:
                return error.messages[0]

    for item in extras:
        note = request.POST.get(f"note-{item.pk}", "").strip()
        upload = request.FILES.get(f"file-{item.pk}")

        changed = (
            bool(upload) or bool(note and note != item.deliveryNote) or item.deliveredAt is None
        )

        if note:
            item.deliveryNote = note[:1000]

        if upload:
            old = item.deliveryFile.name
            storage = item.deliveryFile.storage
            item.deliveryFile.save(upload.name, upload, save=False)
            savedFiles.append(item.deliveryFile)

            if old and old != item.deliveryFile.name:
                transaction.on_commit(
                    lambda name=old, storage=storage: storage.delete(name),
                    robust=True,
                )

        if changed:
            item.deliveredAt = timezone.now()
            item.downloadedAt = None

        item.save(
            update_fields=[
                "deliveryNote",
                "deliveryFile",
                "deliveredAt",
                "downloadedAt",
            ]
        )

    return ""


def sentNote(order):
    names = joinWords(order.items.filter(kind="addon").values_list("name", flat=True))
    url = reverse("receipt", args=[order.pk])

    notify(
        order.seller_id,
        "service_delivered",
        f"Your {names} is ready. Open the receipt to see the file or note.",
        url,
    )


def refundNext(request, order):
    url = reverse("my_orders") + f"#order-{order.pk}"

    notify(
        order.seller_id,
        "order_refunded",
        f"Order #{order.pk} is refunded. ${order.total:,.2f} goes back to you.",
        url,
    )

    today = date_format(timezone.localdate(), "j M Y")

    openHelp = list(
        Ticket.objects.select_for_update()
        .filter(order=order, topic="refund")
        .exclude(status="resolved")
        .order_by("pk")
    )

    replies = []

    for ticket in openHelp:
        replies.append(
            TicketMessage(
                ticket=ticket,
                author=request.user,
                body=f"Refunded on {today}.",
                fromStaff=True,
            )
        )

        audit(
            request,
            "ticket resolved",
            f"Ticket #{ticket.pk}",
            f"Refunded order #{order.pk}",
        )

    TicketMessage.objects.bulk_create(replies)

    Ticket.objects.filter(pk__in=[ticket.pk for ticket in openHelp]).update(
        status="resolved", resolvedAt=timezone.now(), assignee=request.user
    )


def doRefund(request, pk):
    with transaction.atomic():
        order = Order.objects.select_for_update().filter(pk=pk).first()

        if not order or not order.canRefund():
            messages.error(request, "That order has already moved on. Nothing changed.")

            return

        refund = Refund.objects.filter(order=order).exclude(status="failed").first()

        if refund is None:
            refund = Refund.objects.create(order=order, amount=order.total, staff=request.user)

        if refund.status == "succeeded":
            return

        refund.status = "pending"
        refund.save(update_fields=["status"])
        payment = order.payment

    if payment and payment.method == "stripe":
        result = makeRefund(refund, payment)
    else:
        result = {"status": "succeeded", "id": ""}

    from sellerpanel.reconciliation import endRefund

    status, changed = endRefund(refund, result, request.user)

    if not changed:
        return

    if status != "succeeded":
        messages.error(
            request,
            "Stripe has not confirmed the refund. The order was not marked refunded. Retry the refund to check it again.",
        )

        return

    messages.success(request, f"Order #{order.pk} refunded. The seller was told.")


@staffPage("admin_orders")
def order_set(request, pk):
    if request.method == "POST" and request.POST.get("status") == "refunded":
        doRefund(request, pk)
    else:
        return moveOrder(request, pk)

    return orderBack(request)


def moveOrder(request, pk):
    with fileGuard() as saved:
        with transaction.atomic():
            return saveOrder(request, pk, saved)


def saveOrder(request, pk, savedFiles):
    if request.method == "POST":
        order = Order.objects.select_for_update().filter(pk=pk).first()
        status = request.POST.get("status")

        if (
            order
            and status == "delivered"
            and order.status == "processing"
            and order.items.filter(kind="addon").exists()
        ):
            problem = saveFiles(request, order, savedFiles)

            if problem:
                request.session["deliveryProblem"] = problem

                return redirect(reverse("admin_order", args=[order.pk]) + "#delivery")

            audit(
                request,
                "order status",
                f"Order #{order.pk}",
                "Delivery sent to seller; work remains processing.",
            )

            sentNote(order)
            messages.success(request, f"Delivery sent for order #{order.pk}.")
        elif order and status == order.status:
            pass
        elif order and status in dict(orderMoves(order)):
            if status == "completed":
                problem = saveFiles(request, order, savedFiles)
            else:
                problem = ""

            if problem:
                request.session["deliveryProblem"] = problem

                return redirect(reverse("admin_order", args=[order.pk]) + "#delivery")

            labels = dict(Order.STATUS_CHOICES)
            old = labels[order.status]
            order.status = status
            order.save()

            audit(
                request,
                "order status",
                f"Order #{order.pk}",
                f"{old} → {labels[status]}",
            )

            if status == "processing":
                messages.success(request, f"Order #{order.pk}: work started.")
            elif status == "completed":
                sentNote(order)
                messages.success(request, f"Order #{order.pk} is completed.")
        else:
            messages.error(request, "That order has already moved on. Nothing changed.")

    return orderBack(request)


def orderBack(request):
    back = request.POST.get("back", "")

    if not back.startswith("/") or not url_has_allowed_host_and_scheme(
        back, {request.get_host()}, require_https=request.is_secure()
    ):
        back = "admin_orders"

    return redirect(back)


@staffPage("admin_orders")
def see_order(request, pk):
    order = (
        Order.objects.select_related("seller", "payment")
        .prefetch_related("items__listing", "items__product")
        .filter(pk=pk)
        .first()
    )

    if not order:
        return redirect("admin_orders")

    addActions(order)

    changes = (
        AuditLog.objects.filter(action="order status", target=f"Order #{order.pk}")
        .select_related("actor")
        .order_by("createdAt", "pk")
    )

    history = [
        {
            "what": "Paid at checkout",
            "when": order.createdAt,
            "who": order.seller.first_name or order.seller.email,
        }
    ]

    for change in changes:
        history.append(
            {
                "what": change.detail,
                "when": change.createdAt,
                "who": (
                    (change.actor.first_name or change.actor.email) if change.actor else "System"
                ),
            }
        )

    context = {
        "order": order,
        "history": history,
        "deliveryProblem": request.session.pop("deliveryProblem", ""),
        "unlogged": order.status != "paid" and order.hasServices and not changes,
        "canSeeCustomer": canOpen(request.user, "admin_customers"),
        "sellerFirstName": firstName(order.seller),
        "sellerMailto": mailto(order.seller.email, f"Your FlatFair order #{order.pk}"),
    }

    return page(request, "adminpanel/order.html", context, section="admin_orders")


@staffPage("admin_orders")
def payments(request):
    notes = []
    autoSync(notes.append)

    for note in notes:
        if note.startswith(("Payment #", "Refund #")):
            messages.info(request, "Checked with Stripe. " + note)

    found = (
        Payment.objects.filter(payAlertQ())
        .select_related("seller")
        .prefetch_related("refunds")
        .annotate(hasOrders=Exists(Order.objects.filter(payment_id=OuterRef("pk"))))
        .order_by("createdAt", "pk")
    )

    return page(request, "adminpanel/payments.html", {"rows": found}, section="admin_orders")


def payNote(payment, text):
    notify(
        payment.seller_id,
        "payment_update",
        f"Payment #{payment.pk}: {text}",
        reverse("my_orders"),
    )


@staffPage("admin_orders")
@require_POST
def pay_action(request, pk):
    action = request.POST.get("action")

    if action not in ("check", "place", "refund", "close"):
        return HttpResponseBadRequest("Choose a payment action.")

    payment = Payment.objects.filter(payAlertQ(), pk=pk).first()

    if not payment:
        return redirect("admin_payments")

    if action == "check":
        askStripe(request, payment)
    elif action == "place":
        placeBuy(request, payment)
    elif action == "close":
        endUnpaid(request, payment)
    else:
        refundLost(request, payment)

    return redirect("admin_payments")


def askStripe(request, payment):
    before = payment.status
    text = syncPay(payment)

    if text:
        audit(request, "payment checked", f"Payment #{payment.pk}", text)

        if Payment.objects.filter(pk=payment.pk).exclude(status=before).exists():
            payNote(payment, text)

        messages.info(request, text)


@transaction.atomic
def placeBuy(request, payment):
    lock_user(payment.seller_id)
    payment = Payment.objects.select_for_update().get(pk=payment.pk)

    if not payment.needsAttention:
        return

    placed, problem = finishPay(payment)

    text = (
        problem or "Purchase placed: " + ", ".join(f"order #{order.pk}" for order in placed) + "."
    )

    audit(request, "payment purchase", f"Payment #{payment.pk}", text)
    payNote(payment, text)

    if problem:
        messages.error(request, text)
    else:
        messages.success(request, text)


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


def refundLost(request, payment):
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)

        if not payment.needsAttention:
            return

        if payment.orders.exists() or payment.status != "paid":
            text = "Only a paid payment with no orders can be refunded here."
            audit(request, "payment refund blocked", f"Payment #{payment.pk}", text)
            payNote(payment, text)
            messages.error(request, text)

            return

        refund = payment.refunds.exclude(status="failed").first()

        if refund is None:
            refund = Refund.objects.create(
                payment=payment, amount=payment.amount, staff=request.user
            )

        audit(
            request,
            "payment refund requested",
            f"Payment #{payment.pk}",
            f"Refund #{refund.pk}",
        )

    if payment.method == "stripe":
        result = makeRefund(refund, payment)
    else:
        result = {"status": "succeeded", "id": ""}

    status, changed = endRefund(refund, result, request.user)

    if not changed:
        return

    if status == "succeeded":
        messages.success(
            request,
            f"Payment #{payment.pk} from {payment.seller.email} was refunded in full (${payment.amount:,.2f}).",
        )
    else:
        messages.error(request, "Stripe has not confirmed the refund. Staff will check it again.")
