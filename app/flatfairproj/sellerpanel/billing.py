from .forms import CardForm, ReviewForm
from django.db.models import Count, OuterRef, Q, Subquery, Sum
from decimal import Decimal, ROUND_HALF_UP
from django.http import FileResponse, Http404
from django.db import IntegrityError, transaction
from storefront.models import (
    Listing, Order, OrderItem, Payment,
    Product, RATE_PACKAGE_AFTER_DAYS, Review, hasRole,
    lock_user, needEmail, notAllowed,
)
from storefront.listing_rules import OPEN_LISTING_STATUSES, autoClose, closeOrder, openFlats
from .models import PackageRecommendation
from django.core.paginator import Paginator
from storefront.presentation import RATING_CHOICES, readyPics
from types import SimpleNamespace
from adminpanel.models import Ticket
from django.core.exceptions import ValidationError
from storefront.reminders import autoNudge
from adminpanel.access import canOpen
from .plans import cartFlat, packIssue, packState, packsNow
from .stripepay import cents, checkSums, getSession, newSession, stripeKey
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.urls import reverse
from .recommend import showTip
from datetime import timedelta
from django.utils import timezone
import uuid
from sellerpanel.views import STRIPE_ERROR, lowerNote, maybeId, sell_only


def cartTotals(items):
    partSum = Decimal("0.00")
    now = timezone.now()

    for item in items:
        partSum += item.price
        item.endsOn = None

        if item.durationDays:
            item.endsOn = now + timedelta(days=item.durationDays)

    gst = (partSum * Decimal("0.09")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return partSum, gst


def cartGroups(items):
    packs = []
    extras = []

    for item in items:
        if item.kind == "package":
            packs.append(item)
        else:
            extras.append(item)

    groups = []

    if packs:
        groups.append({"items": packs, "status": "completed"})

    if extras:
        groups.append({"items": extras, "status": "paid"})

    for group in groups:
        group["subtotal"], group["gst"] = cartTotals(group["items"])
        group["total"] = group["subtotal"] + group["gst"]

    return groups


def groupSums(groups):
    partSum = Decimal("0.00")
    gst = Decimal("0.00")

    for group in groups:
        partSum += group["subtotal"]
        gst += group["gst"]

    return partSum, gst


def cartSnapshot(items):
    frozen = []

    for item in items:
        frozen.append([item.pk, item.name, item.kind, item.price, item.durationDays])

    return frozen


@login_required
@needEmail
def cart_add(request, pk):
    if not hasRole(request.user, "seller"):
        return sell_only(request)

    if request.method != "POST":
        return redirect("packages")

    product = Product.objects.filter(pk=pk, isActive=True).first()

    if not product:
        return redirect("packages")

    picked = maybeId(request.POST.get("listing", ""))

    if picked and openFlats(request.user).filter(publicId=picked).exists():
        request.session["cartListing"] = str(picked)

    target = cartFlat(request.user, request.session.get("cartListing"))
    packUrl = reverse("packages")

    if target:
        packUrl += f"?listing={target.publicId}"

    if product.kind == "package" and target:
        refusal = lowerNote(target, product)

        if refusal:
            messages.error(request, refusal)

            return redirect(f"{packUrl}#package-{product.pk}")

    cart = request.session.get("cart", [])

    if product.kind == "package":
        packIds = list(Product.objects.filter(kind="package").values_list("pk", flat=True))
        cart = [product_id for product_id in cart if product_id not in packIds]

    if product.pk not in cart:
        cart.append(product.pk)

    request.session["cart"] = cart
    request.session.pop("checkoutToken", None)
    messages.success(request, f"{product.name} added to your cart.")

    if request.POST.get("next") == "cart":
        return redirect("cart")

    return redirect(f"{packUrl}#package-{product.pk}")


@login_required
def cart_remove(request, pk):
    if not hasRole(request.user, "seller"):
        return sell_only(request)

    if request.method == "POST":
        request.session["cart"] = [item for item in request.session.get("cart", []) if item != pk]
        request.session.pop("checkoutToken", None)
        messages.success(request, "Removed from your cart.")

    return redirect("cart")


@login_required
def cart(request):
    if not hasRole(request.user, "seller"):
        return sell_only(request)

    items = list(
        Product.objects.filter(pk__in=request.session.get("cart", []), isActive=True).order_by("pk")
    )

    partSum, gst = groupSums(cartGroups(items))

    if cartSnapshot(items) != request.session.get("cartSnapshot"):
        request.session.pop("checkoutToken", None)

    token = request.session.get("checkoutToken") or uuid.uuid4().hex
    request.session["checkoutToken"] = token
    request.session["cartSnapshot"] = cartSnapshot(items)

    bought = OrderItem.objects.filter(order__seller=request.user, kind="addon").exclude(
        order__status="refunded"
    )

    homes = openFlats(request.user).order_by("-createdAt")
    theFlat = cartFlat(request.user, request.session.get("cartListing"))

    context = {
        "suggested": Product.objects.filter(kind="addon", isActive=True)
        .exclude(pk__in=[item.pk for item in items])
        .exclude(pk__in=bought.values("product_id")),
        "needsFlat": any(item.kind == "package" for item in items),
        "selectedListing": str(theFlat.publicId) if theFlat else "",
        "selectedFlat": theFlat,
        "packageRecommendation": showTip(theFlat),
        "items": items,
        "subtotal": partSum,
        "gst": gst,
        "total": partSum + gst,
        "checkoutToken": token,
        "listings": homes,
        "stripeOn": stripeKey() != "",
        "card": CardForm(),
    }

    return render(request, "sellerpanel/cart.html", context)


@login_required
@needEmail
def checkout(request):
    if not hasRole(request.user, "seller"):
        return sell_only(request)

    if request.method != "POST":
        return redirect("cart")

    token = request.POST.get("checkoutToken", "")

    result = payStep(
        request, token, request.POST.get("listing", ""), request.POST.get("pay") == "yes"
    )

    if not isinstance(result, Payment):
        return result

    items, groups, listing = payLines(result)
    session = newSession(request, items, sum(group["gst"] for group in groups), token, listing)

    if not session:
        messages.error(request, STRIPE_ERROR)

        return redirect("cart")

    Payment.objects.filter(pk=result.pk).update(providerSessionId=session["id"])

    return redirect(session["url"])


@login_required
@needEmail
def stripe_return(request):
    if not hasRole(request.user, "seller"):
        return sell_only(request)

    stripeId = request.GET.get("session_id", "")
    paid = getSession(stripeId)

    if paid is None:
        messages.error(request, STRIPE_ERROR)

        return redirect("cart")

    meta = paid.get("metadata") or {}
    token = paid.get("client_reference_id") or ""

    if (
        paid.get("id") != stripeId
        or paid.get("mode") != "payment"
        or paid.get("status") != "complete"
        or paid.get("livemode") is not False
        or paid.get("payment_status") != "paid"
        or not isinstance(meta, dict)
        or meta.get("seller") != str(request.user.pk)
        or not isinstance(token, str)
        or not token
    ):
        messages.error(request, "Stripe hasn’t confirmed this payment. Your cart is still here.")

        return redirect("cart")

    payment = Payment.objects.filter(
        seller=request.user, method="stripe", checkoutToken=token
    ).first()

    if (
        not payment
        or payment.providerSessionId not in (None, stripeId)
        or paid.get("amount_total") != cents(payment.amount)
        or paid.get("currency") != payment.currency
        or meta.get("listing", "") != payment.snapshot.get("listingPublicId", "")
        or not isinstance(paid.get("payment_intent"), str)
        or not paid["payment_intent"].startswith("pi_")
    ):
        messages.error(
            request, "This Stripe payment doesn’t match your checkout. Please contact support."
        )

        return redirect("cart")

    placed, problem = finishPay(payment, paid)

    if problem:
        return payAlert(request, payment)

    return payDone(request, payment, placed)


def payAlert(request, payment):
    payment.refresh_from_db()

    if payment.status == "refunded":
        messages.info(request, "This payment has been refunded.")

        return redirect("my_orders")

    Payment.objects.filter(pk=payment.pk).exclude(status="refunded").update(needsAttention=True)
    messages.error(
        request,
        "We’ve received your payment and our team will arrange your order. Please contact support.",
    )

    return redirect("my_orders")


def payCopy(groups, listing):
    return {
        "listingId": listing.pk if listing else None,
        "listingPublicId": str(listing.publicId) if listing else "",
        "groups": [
            {
                **{key: str(group[key]) for key in ("subtotal", "gst", "total")},
                "status": group["status"],
                "items": [
                    {
                        "pk": item.pk,
                        "name": item.name,
                        "kind": item.kind,
                        "price": str(item.price),
                        "durationDays": item.durationDays,
                        "tier": item.tier,
                    }
                    for item in group["items"]
                ],
            }
            for group in groups
        ],
    }


def payLines(payment):
    groups = []
    items = []

    for saved in payment.snapshot["groups"]:
        lines = [
            SimpleNamespace(**{**item, "price": Decimal(item["price"])}) for item in saved["items"]
        ]
        items.extend(lines)

        groups.append(
            {
                **{key: Decimal(saved[key]) for key in ("subtotal", "gst", "total")},
                "status": saved["status"],
                "items": lines,
            }
        )

    listing = Listing.objects.filter(
        pk=payment.snapshot.get("listingId"), seller=payment.seller
    ).first()

    return items, groups, listing


def mayBuy(payment, seller):
    if not seller or not seller.is_active or seller.is_staff or not hasRole(seller, "seller"):
        return None, None, "The seller account must be an active seller."

    try:
        items, groups, listing = payLines(payment)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None, None, "The purchase snapshot is incomplete."

    products = {
        product.pk: product
        for product in Product.objects.select_for_update().filter(
            pk__in=[item.pk for item in items]
        )
    }

    if not items or len(products) != len(items):
        return None, None, "A product in this purchase no longer exists."

    if payment.snapshot.get("listingId"):
        listing = (
            Listing.objects.select_for_update()
            .filter(
                pk=payment.snapshot["listingId"], seller=seller, status__in=OPEN_LISTING_STATUSES
            )
            .first()
        )

        if not listing or str(listing.publicId) != payment.snapshot.get("listingPublicId"):
            return None, None, "The listing must still belong to this seller and be open."

    for item in items:
        if item.kind == "package":
            if listing is None:
                return None, None, "A package needs an eligible listing."

            item.tier = getattr(item, "tier", products[item.pk].tier)
            problem = packIssue(item) or lowerNote(listing, item)

            if problem:
                return None, None, problem

    return groups, listing, ""


@transaction.atomic
def finishPay(payment, confirmed=None):
    seller = lock_user(payment.seller_id)
    payment = Payment.objects.select_for_update().get(pk=payment.pk)

    if confirmed:
        payment.providerSessionId = confirmed["id"]
        payment.paymentIntentId = confirmed["payment_intent"]

        payment.snapshot = {
            **payment.snapshot,
            "providerStatus": f"{confirmed['status']} / {confirmed['payment_status']}",
            "providerCheckedAt": timezone.now().isoformat(),
        }

    already = list(payment.orders.order_by("pk"))

    if already:
        payment.needsAttention = False
        payment.save()

        return already, ""

    canOk = confirmed and payment.status in ("pending", "failed")

    if (payment.status != "paid" and not canOk) or payment.refunds.exclude(
        status="failed"
    ).exists():
        return [], "The payment must be paid and have no refund in progress."

    groups, listing, problem = mayBuy(payment, seller)

    if not problem:
        try:
            checkSums(groups)
        except ValidationError as error:
            problem = error.messages[0]

    if confirmed and payment.status in ("pending", "failed"):
        payment.status = "paid"
        payment.paidAt = timezone.now()

    payment.save()

    if not problem:
        try:
            with transaction.atomic():
                placed = addOrders(seller, groups, payment.checkoutToken, listing, payment)
        except IntegrityError:
            problem = "The purchase could not be placed. Please check the snapshot."

    payment.needsAttention = bool(problem)
    payment.snapshot = {**payment.snapshot, "attentionReason": problem}
    payment.save(update_fields=["needsAttention", "snapshot"])

    return ([], problem) if problem else (placed, "")


def addOrders(seller, groups, token, listing, payment):
    checkSums(groups)
    placed = []

    for group in groups:
        orderKey = token

        if placed:
            orderKey = token + "-2"

        order = Order.objects.create(
            seller=seller,
            payment=payment,
            status=group["status"],
            checkoutToken=orderKey,
            subtotal=group["subtotal"],
            gst=group["gst"],
            total=group["total"],
        )

        for item in group["items"]:
            line = OrderItem.objects.create(
                order=order,
                product_id=item.pk,
                price=item.price,
                listing=listing,
                name=item.name,
                kind=item.kind,
                tier=item.tier,
                durationDays=item.durationDays,
            )

            if item.kind == "package" and listing:
                tip = (
                    PackageRecommendation.objects.select_for_update()
                    .filter(
                        listing=listing,
                        product_id=item.pk,
                        converted=False,
                        createdAt__gte=payment.createdAt - timedelta(days=30),
                        createdAt__lte=payment.createdAt,
                    )
                    .order_by("-createdAt", "-pk")
                    .first()
                )

                if tip:
                    tip.converted = True
                    tip.orderItem = line
                    tip.save(update_fields=["converted", "orderItem"])

        placed.append(order)

    return placed


@transaction.atomic
def payStep(request, token, chosen, pay):
    seller = lock_user(request.user.pk)

    if not seller or not seller.is_active:
        return notAllowed(request, "active seller")

    already = (
        Order.objects.filter(seller=request.user, checkoutToken=token).first() if token else None
    )

    if already:
        return redirect(f"{reverse('my_orders')}#order-{already.pk}")

    if not token or token != request.session.get("checkoutToken"):
        messages.error(request, "Your cart has changed. Please check the new total.")

        return cart(request)

    items = list(
        Product.objects.select_for_update()
        .filter(pk__in=request.session.get("cart", []), isActive=True)
        .order_by("pk")
    )

    if cartSnapshot(items) != request.session.get("cartSnapshot"):
        messages.error(request, "Your cart has changed. Please check the new total.")

        return cart(request)

    if not items:
        messages.error(request, "Your cart is empty. Please add a package or service first.")

        return redirect("cart")

    package = None

    for item in items:
        if item.kind == "package":
            package = item

    homes = openFlats(request.user).select_for_update()
    listing = None

    if chosen or (package and homes.exists()):
        listing = homes.filter(publicId=maybeId(chosen)).first()

        if listing is None:
            messages.error(request, "Please choose which of your flats this order is for.")

            return redirect("cart")

    if package and listing is None:
        messages.error(request, "List your flat first (it’s free), then buy a package for it.")

        return redirect("cart")

    refusal = ""

    if package:
        refusal = packIssue(package) or lowerNote(listing, package)

    if refusal:
        messages.error(request, refusal)

        return redirect("cart")

    groups = cartGroups(items)

    try:
        checkSums(groups)
    except ValidationError as error:
        messages.error(request, error.messages[0])

        return redirect("cart")

    partSum, gst = groupSums(groups)

    context = {
        "items": items,
        "hasPackage": package is not None,
        "splitOrder": len(groups) > 1,
        "listing": listing,
        "subtotal": partSum,
        "gst": gst,
        "total": partSum + gst,
        "checkoutToken": token,
        "stripeOn": stripeKey() != "",
        "card": CardForm(),
    }

    if not pay:
        return render(request, "sellerpanel/payment.html", context)

    payWay = ""

    if not stripeKey():
        card = CardForm(request.POST)

        if not card.is_valid():
            context["card"] = card

            return render(request, "sellerpanel/payment.html", context)

        payWay = (
            f" Paid with {card.cleaned_data['brand']} ending {card.cleaned_data['last4']} (demo)."
        )

    frozen = payCopy(groups, listing)

    payment, created = Payment.objects.get_or_create(
        checkoutToken=token,
        defaults={
            "seller": request.user,
            "method": "stripe" if stripeKey() else "demo",
            "amount": partSum + gst,
            "snapshot": frozen,
        },
    )

    if payment.seller_id != request.user.pk or payment.snapshot != frozen:
        messages.error(request, "Your cart has changed. Please check the new total.")

        return redirect("cart")

    if payment.method == "stripe":
        return payment

    payment.cardBrand = card.cleaned_data["brand"]
    payment.cardLast4 = card.cleaned_data["last4"]
    payment.status = "paid"
    payment.paidAt = timezone.now()
    payment.save()
    placed, problem = finishPay(payment)

    if problem:
        return payAlert(request, payment)

    return payDone(request, payment, placed, payWay)


def payDone(request, payment, placed, paidWith=""):
    if request.session.get("checkoutToken") == payment.checkoutToken:
        request.session["cart"] = []
        request.session.pop("checkoutToken", None)

    first = placed[0]

    if len(placed) > 1:
        messages.success(
            request,
            f"Thank you. We’ve placed 2 orders: #{first.pk} for your package and #{placed[1].pk} for your services.{paidWith}",
        )
    else:
        messages.success(request, f"Thank you. Order #{first.pk} has been placed.{paidWith}")

    return redirect(f"{reverse('my_orders')}#order-{first.pk}")


def addTickets(orders):
    dueRefund = Ticket.objects.filter(
        order=OuterRef("pk"), customer=OuterRef("seller"), topic="refund"
    )

    dueRefund = dueRefund.exclude(status="resolved")

    return orders.annotate(refundTicket=Subquery(dueRefund.order_by("-pk").values("pk")[:1]))


@login_required
@require_POST
@needEmail
def checkPay(request, pk):
    from .reconciliation import syncPay, resourceCache

    if not hasRole(request.user, "seller"):
        return sell_only(request)

    payment = get_object_or_404(Payment, pk=pk, seller=request.user)
    order = payment.orders.order_by("pk").first()

    if order:
        messages.success(request, f"Thank you. Order #{order.pk} has been placed.")

        return redirect("receipt", pk=order.pk)

    if payment.status != "pending":
        return redirect("my_orders")

    if not resourceCache.add(f"payment-check:{request.user.pk}", True, 60):
        messages.info(request, "Please wait a minute before checking again.")

        return redirect("my_orders")

    syncPay(payment)
    order = payment.orders.order_by("pk").first()

    if order:
        messages.success(request, f"Thank you. Order #{order.pk} has been placed.")

        return redirect("receipt", pk=order.pk)

    return redirect("my_orders")


@login_required
def my_orders(request):
    statuses = ("paid", "processing")
    autoClose()
    autoNudge()

    if not hasRole(request.user, "seller"):
        return sell_only(request)

    orders = request.user.orders.order_by("-createdAt").prefetch_related(
        "items__product", "items__listing", "items__review"
    )

    orderNums = orders.aggregate(
        all=Count("pk"),
        active=Count("pk", filter=Q(status__in=statuses)),
        completed=Count("pk", filter=Q(status="completed")),
        refunded=Count("pk", filter=Q(status="refunded")),
    )

    spent = orders.exclude(status="refunded").aggregate(total=Sum("total"))["total"] or 0

    if spent:
        spent = spent.quantize(Decimal("0.01"))

    addons = OrderItem.objects.filter(
        order__seller=request.user, order__status__in=statuses, kind="addon"
    ).count()

    picked = request.GET.get("status", "")

    if picked == "active":
        orders = orders.filter(status__in=statuses)
    elif picked:
        orders = orders.filter(status=picked)

    orders = list(addTickets(orders))
    waitingOrders = [order for order in orders if order.canConfirmDelivery()]
    otherOrders = [order for order in orders if not order.canConfirmDelivery()]
    orders = Paginator(waitingOrders + otherOrders, 10).get_page(request.GET.get("orders_page"))
    now = timezone.now()

    pays = (
        request.user.payments.filter(orders__isnull=True)
        .filter(
            Q(status__in=("pending", "paid"))
            | Q(status="failed", createdAt__gte=now - timedelta(days=30))
        )
        .order_by("-createdAt", "-pk")
    )

    for order in orders:
        order.hasServices = False

        for line in order.items.all():
            if line.kind == "package":
                line.packageStatus = packState(line, now)
            else:
                order.hasServices = True

    plans = packsNow(request.user.listings.all())
    readyPics(
        [line.listing for order in orders for line in order.items.all()]
        + [plan["listing"] for plan in plans]
    )

    context = {
        "orderCounts": orderNums,
        "selectedStatus": picked,
        "spent": spent,
        "plans": plans,
        "addons": addons,
        "orders": orders,
        "payments": pays,
        "steps": ["paid", "processing", "completed"],
    }

    return render(request, "sellerpanel/orders.html", context)


@login_required
def receipt(request, pk):
    order = get_object_or_404(
        addTickets(
            Order.objects.select_related("seller", "payment").prefetch_related(
                "items__listing", "items__review"
            )
        ),
        pk=pk,
        seller=request.user,
    )

    canRate = []
    waiting = []

    for line in order.items.all():
        if line.canRate():
            canRate.append(line)
        elif order.status == "completed":
            waiting.append(line)

    context = {
        "order": order,
        "rateable": canRate,
        "waiting": waiting,
        "rateAfterDays": RATE_PACKAGE_AFTER_DAYS,
        "ratingChoices": RATING_CHOICES,
    }

    return render(request, "sellerpanel/receipt.html", context)


@login_required
def delivery_file(request, pk):
    item = get_object_or_404(OrderItem.objects.select_related("order"), pk=pk)

    if item.order.seller_id != request.user.pk and not canOpen(request.user, "admin_orders"):
        raise Http404

    if not item.deliveryFile:
        raise Http404

    try:
        reply = FileResponse(
            item.deliveryFile.open("rb"), as_attachment=True, filename=item.deliveryFileName
        )
    except (OSError, ValueError):
        raise Http404

    reply["Cache-Control"] = "private, no-store"

    return reply


@login_required
@require_POST
def confirmGot(request, pk):
    order = get_object_or_404(Order, pk=pk, seller=request.user)

    if closeOrder(order.pk, seller=request.user):
        messages.success(request, "Thanks for confirming. Your order is now complete.")
    else:
        order.refresh_from_db(fields=["status"])

        if order.status == "completed":
            messages.info(request, "Your order is already complete.")
        else:
            messages.error(request, "You can confirm once all your services have been delivered.")

    return redirect("receipt", pk=order.pk)


@login_required
@require_POST
@transaction.atomic
def rate_item(request, pk):
    if not hasRole(request.user, "seller"):
        return sell_only(request)

    item = get_object_or_404(
        OrderItem.objects.select_for_update().select_related("order"),
        pk=pk,
        order__seller=request.user,
    )

    back = reverse("receipt", args=[item.order_id]) + f"#rate-{item.pk}"

    if item.order.status != "completed":
        messages.error(request, "You can rate a service once its order is done.")

        return redirect(back)

    if not item.canRate():
        messages.error(
            request, f"You can rate a package once it has run for {RATE_PACKAGE_AFTER_DAYS} days."
        )

        return redirect(back)

    already = Review.objects.filter(orderItem=item).first()
    form = ReviewForm(request.POST, instance=already)

    if not form.is_valid():
        messages.error(request, list(form.errors.values())[0][0])

        return redirect(back)

    review = form.save(commit=False)
    review.seller = request.user
    review.orderItem = item
    review.save()

    if already:
        messages.success(request, "Your rating is updated.")
    else:
        messages.success(request, "Thank you. Your rating is saved.")

    return redirect(back)
