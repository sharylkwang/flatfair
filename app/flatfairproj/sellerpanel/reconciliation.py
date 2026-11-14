import logging
from datetime import timedelta
from types import SimpleNamespace
from django.core.cache import caches
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from storefront.models import lock_user, Order, Payment, Refund
from .stripepay import checkPay, makeRefund
from sellerpanel.billing import finishPay

resourceCache = caches["resources"]
AUTO_RECONCILE_SECONDS = 600
AUTO_RECONCILE_KEY = "payments:auto-reconcile"
log = logging.getLogger(__name__)


def autoSync(report=None):
    if not resourceCache.add(AUTO_RECONCILE_KEY, True, AUTO_RECONCILE_SECONDS):
        return

    return syncBatch(report)


def stale_q():
    return Q(
        method="stripe", status="pending", createdAt__lte=timezone.now() - timedelta(minutes=30)
    )


def payAlertQ():
    return Q(needsAttention=True) | stale_q()


def syncPay(payment):
    result = checkPay(payment) if payment.method == "stripe" else None

    with transaction.atomic():
        lock_user(payment.seller_id)
        payment = (
            Payment.objects.select_for_update()
            .filter(payAlertQ() | Q(status="pending"), pk=payment.pk)
            .first()
        )


        if payment is None:
            return ""

        if result is None:
            return "Stripe could not confirm a matching test payment. Nothing changed."



        if payment.status not in ("paid", "refunded", "partly_refunded"):
            payment.status = result["status"]

        payment.paymentIntentId = result["paymentIntentId"]
        payment.snapshot = {
            **payment.snapshot,
            "providerStatus": result["providerStatus"],
            "providerCheckedAt": timezone.now().isoformat(),
        }

        if payment.status == "paid" and not payment.paidAt:
            payment.paidAt = timezone.now()

        if payment.status in ("failed", "refunded") or payment.orders.exists():
            payment.needsAttention = False
        elif payment.status == "paid":
            payment.needsAttention = True

        payment.save()


        text = f"Stripe reports {result['providerStatus']}. Payment is {payment.get_status_display().lower()}."

        if payment.status == "paid" and result["status"] == "paid":
            placed, problem = finishPay(payment)
            text += " " + (
                problem
                or "Purchase placed: " + ", ".join(f"order #{order.pk}" for order in placed) + "."
            )


        return text


def refunds_due():
    return (
        Refund.objects.filter(status="pending")
        .filter(Q(payment__method="stripe") | Q(order__payment__method="stripe"))
        .select_related("payment", "order__payment", "staff")
        .order_by("pk")
    )


def syncRefund(refund):
    payment = refund.payment if refund.payment_id else refund.order.payment
    result = makeRefund(refund, payment)



    status, changed = endRefund(refund, result)

    return f"Refund is {status}." if changed else ""


def endRefund(refund, result, staff=None):
    from adminpanel.orders import payNote
    from adminpanel.views import audit
    from adminpanel.orders import refundNext


    payId = refund.payment_id or refund.order.payment_id
    ownerId = refund.payment.seller_id if refund.payment_id else refund.order.seller_id



    with transaction.atomic():
        lock_user(ownerId)
        payment = Payment.objects.select_for_update().filter(pk=payId).first()
        order = Order.objects.select_for_update().filter(pk=refund.order_id).first()
        refund = Refund.objects.select_for_update().select_related("staff").get(pk=refund.pk)

        if refund.status != "pending":
            return refund.status, False


        if result is not None:
            refund.providerRefundId = result["id"]
            refund.status = (
                result.get("status")
                if result.get("status") in ("succeeded", "failed")
                else "pending"
            )


        refund.save(update_fields=["providerRefundId", "status"])
        request = SimpleNamespace(user=staff or refund.staff)

        if order:
            if refund.status != "succeeded":
                audit(request, "refund unconfirmed", f"Order #{order.pk}", refund.status)

                return refund.status, True

            order.status = "refunded"
            order.save(update_fields=["status"])


            if payment:
                repaid = Refund.objects.filter(
                    order__payment=payment, status="succeeded"
                ).aggregate(total=Sum("amount"))["total"]
                payment.status = "refunded" if repaid >= payment.amount else "partly_refunded"
                payment.save(update_fields=["status"])

            audit(request, "order status", f"Order #{order.pk}", "Paid → Refunded")
            refundNext(request, order)
        else:
            if refund.status == "succeeded":
                payment.status = "refunded"
                payment.needsAttention = False
                payment.save(update_fields=["status", "needsAttention"])
                text = f"We couldn’t place your order, so we’ve refunded your payment in full (${payment.amount:,.2f})."
            else:
                text = "Stripe hasn’t confirmed your refund yet. Our team will check it again."

            audit(
                request,
                "payment refund",
                f"Payment #{payment.pk}",
                f"Refund #{refund.pk}: {refund.status}",
            )
            payNote(payment, text)

        return refund.status, True


def syncBatch(report=None):
    from storefront.listing_rules import closeSent


    from storefront.reminders import nudgeAll

    nudged = nudgeAll()

    if nudged and report:
        report(f"Sent {nudged} viewing and order reminders.")

    closed = closeSent()

    if closed and report:
        report(f"Completed {closed} delivered service orders.")

    checked = 0
    failed = 0
    refunds = 0

    for payment in list(Payment.objects.filter(stale_q()).order_by("pk")):
        try:
            message = syncPay(payment)

            if message:
                checked += 1

                if report:
                    report(f"Payment #{payment.pk}: {message}")
        except Exception:
            failed += 1
            log.exception("Reconciliation failed for payment %s", payment.pk)

    for refund in list(refunds_due()):
        try:
            message = syncRefund(refund)

            if message:
                refunds += 1

                if report:
                    report(f"Refund #{refund.pk}: {message}")
        except Exception:
            failed += 1
            log.exception("Reconciliation failed for refund %s", refund.pk)

    return checked, refunds, failed
