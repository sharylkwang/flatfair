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


