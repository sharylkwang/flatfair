from decimal import Decimal, ROUND_HALF_UP
import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.urls import reverse
from storefront.models import Order, Payment

SESSIONS_URL = "https://api.stripe.com/v1/checkout/sessions"
TIMEOUT = 10


def stripeKey():
    key = settings.STRIPE_SECRET_KEY

    if key.startswith("sk_test_"):
        return key

    return ""


def cents(amount):
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def newSession(request, items, gst, token, listing):
    if not stripeKey():
        return None

    data = {
        "mode": "payment",
        "success_url": request.build_absolute_uri(reverse("stripe_return"))
        + "?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": request.build_absolute_uri(reverse("cart")),
        "client_reference_id": token,
        "metadata[seller]": str(request.user.pk),
    }

    if listing:
        data["metadata[listing]"] = str(listing.publicId)

    lines = []

    for item in items:
        lines.append([item.name, item.price])

    if gst:
        lines.append(["GST (9%)", gst])

    for index, line in enumerate(lines):
        data[f"line_items[{index}][quantity]"] = 1



        data[f"line_items[{index}][price_data][currency]"] = "sgd"
        data[f"line_items[{index}][price_data][unit_amount]"] = cents(line[1])
        data[f"line_items[{index}][price_data][product_data][name]"] = line[0]

    try:
        reply = requests.post(
            SESSIONS_URL,
            data=data,
            headers={
                "Authorization": f"Bearer {stripeKey()}",
                "Idempotency-Key": f"checkout-{request.user.pk}-{token}-{listing.pk if listing else 0}",
            },
            timeout=TIMEOUT,
        )


        session = reply.json()
    except (requests.RequestException, ValueError):
        return None


    if reply.status_code != 200 or not isinstance(session, dict):
        return None

    url = session.get("url")

    if not isinstance(url, str) or not url.startswith("https://checkout.stripe.com/"):
        return None

    if not isinstance(session.get("id"), str) or not session["id"].startswith("cs_"):
        return None

    return session


def getSession(sessionId):
    if (
        not stripeKey()
        or not sessionId.startswith("cs_")
        or not sessionId.replace("_", "").isalnum()
    ):
        return None



    try:
        reply = requests.get(
            f"{SESSIONS_URL}/{sessionId}",
            headers={"Authorization": f"Bearer {stripeKey()}"},
            timeout=TIMEOUT,
        )
        session = reply.json()
    except (requests.RequestException, ValueError):
        return None

    if reply.status_code != 200 or not isinstance(session, dict):
        return None

    return session


def endSession(sessionId):
    if (
        not stripeKey()
        or not sessionId.startswith("cs_")
        or not sessionId.replace("_", "").isalnum()
    ):
        return ""

    try:
        reply = requests.post(
            f"{SESSIONS_URL}/{sessionId}/expire",
            headers={"Authorization": f"Bearer {stripeKey()}"},
            timeout=TIMEOUT,
        )
        session = reply.json()
    except (requests.RequestException, ValueError):
        return ""


    if reply.status_code != 200 or not isinstance(session, dict):
        return ""



    return session.get("status", "")


def makeRefund(refund, payment):
    if not stripeKey() or not payment.paymentIntentId:
        return None



    headers = {"Authorization": f"Bearer {stripeKey()}", "Idempotency-Key": f"refund-{refund.pk}"}



    try:
        if refund.providerRefundId:
            reply = requests.get(
                f"https://api.stripe.com/v1/refunds/{refund.providerRefundId}",
                headers=headers,
                timeout=TIMEOUT,
            )
        else:
            reply = requests.post(
                "https://api.stripe.com/v1/refunds",
                data={"payment_intent": payment.paymentIntentId, "amount": cents(refund.amount)},
                headers=headers,
                timeout=TIMEOUT,
            )


        result = reply.json()
    except (requests.RequestException, ValueError):
        return None

    if reply.status_code in (400, 401, 403, 404) and not refund.providerRefundId:
        return {"id": "", "status": "failed"}

    if reply.status_code != 200 or not isinstance(result, dict):
        return None

    if (
        not isinstance(result.get("id"), str)
        or not result["id"].startswith("re_")
        or (refund.providerRefundId and result["id"] != refund.providerRefundId)
        or result.get("payment_intent") != payment.paymentIntentId
        or result.get("amount") != cents(refund.amount)
        or result.get("currency") != payment.currency
    ):
        return None



    return result


def get_intent(intentId):
    if not stripeKey() or not intentId.startswith("pi_") or not intentId.replace("_", "").isalnum():
        return None

    try:
        reply = requests.get(
            f"https://api.stripe.com/v1/payment_intents/{intentId}",
            headers={"Authorization": f"Bearer {stripeKey()}"},
            timeout=TIMEOUT,
        )
        intent = reply.json()
    except (requests.RequestException, ValueError):
        return None


    if reply.status_code != 200 or not isinstance(intent, dict):
        return None

    return intent


def checkPay(payment):
    if payment.providerSessionId:
        session = getSession(payment.providerSessionId)

        if not session:
            return None


        meta = session.get("metadata")

        if (
            session.get("id") != payment.providerSessionId
            or session.get("livemode") is not False
            or session.get("mode") != "payment"
            or session.get("client_reference_id") != payment.checkoutToken
            or session.get("amount_total") != cents(payment.amount)
            or session.get("currency") != payment.currency
            or not isinstance(meta, dict)
            or meta.get("seller") != str(payment.seller_id)
            or meta.get("listing", "") != payment.snapshot.get("listingPublicId", "")
        ):
            return None

        intentId = session.get("payment_intent")

        if intentId and (not isinstance(intentId, str) or not intentId.startswith("pi_")):
            return None

        if payment.paymentIntentId and intentId != payment.paymentIntentId:
            return None

        if (
            session.get("payment_status") == "paid"
            and session.get("status") == "complete"
            and intentId
        ):
            status = "paid"
        elif session.get("status") == "expired":
            status = "failed"
        else:
            status = "pending"

        return {
            "status": status,
            "paymentIntentId": intentId or "",
            "providerStatus": f"{session.get('status', '')} / {session.get('payment_status', '')}",
        }


    if not payment.paymentIntentId:
        return None

    intent = get_intent(payment.paymentIntentId)

    if not intent or (
        intent.get("id") != payment.paymentIntentId
        or intent.get("livemode") is not False
        or intent.get("amount") != cents(payment.amount)
        or intent.get("currency") != payment.currency
        or (
            intent.get("status") == "succeeded"
            and intent.get("amount_received") != cents(payment.amount)
        )
    ):
        return None

    status = {"succeeded": "paid", "canceled": "failed"}.get(intent.get("status"), "pending")

    return {
        "status": status,
        "paymentIntentId": payment.paymentIntentId,
        "providerStatus": intent.get("status", "unknown"),
    }


def max_amount(model, name):
    field = model._meta.get_field(name)

    return (
        Decimal(10) ** (field.max_digits - field.decimal_places)
        - Decimal(10) ** -field.decimal_places
    )


MAX_PURCHASE_TOTAL = min(
    max_amount(Payment, "amount"),
    *(max_amount(Order, name) for name in ("subtotal", "gst", "total")),
)
MAX_PRODUCT_PRICE = int(MAX_PURCHASE_TOTAL / Decimal("1.09"))


def checkSums(groups):
    amounts = [group[name] for group in groups for name in ("subtotal", "gst", "total")]
    amounts.append(sum(group["total"] for group in groups))

    if any(
        not amount.is_finite() or amount < 0 or amount > MAX_PURCHASE_TOTAL for amount in amounts
    ):
        raise ValidationError(
            "Your cart total including GST is too high. Remove an item or ask our team for help."
        )


def savings(estimate, cost=0):
    fee = Decimal(str(estimate)) * Decimal("0.02") * Decimal("1.09")
    fee = fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cost = Decimal(str(cost)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return {"agentFee": fee, "cost": cost, "saved": max(fee - cost, Decimal("0.00"))}


def mightSave(estimate=None):
    isOwn = estimate is not None
    estimate = estimate if isOwn else 620000
    sample = dict(name="Free listing", examplePrice=estimate, **savings(estimate))

    if isOwn:
        sample["agentFee"] = sample["agentFee"].quantize(Decimal("1E2"), rounding=ROUND_HALF_UP)

    return sample
