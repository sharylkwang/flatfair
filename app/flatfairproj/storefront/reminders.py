import logging
from datetime import timedelta
from django.core.cache import caches
from django.db import transaction
from django.db.models import Count, Max, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from .listing_rules import AUTO_COMPLETE_DAYS
from .models import Deal, DealStep, Order, ViewingBooking, ViewingSlot
from .notices import _place, notify

REMINDER_KEY = "reminders:send"
log = logging.getLogger(__name__)


def visitWords(slot, now):
    starts = timezone.localtime(slot.startsAt)

    if starts.date() == timezone.localdate(now):
        day = "today"
    else:
        day = "tomorrow"

    clock = date_format(starts, "g:i A").lower()

    return f"Viewing {day}, {clock} · {_place(slot.listing)}"


def order_text(order):
    endsAt = timezone.localtime(order.lastDelivered + timedelta(days=AUTO_COMPLETE_DAYS))

    day = date_format(endsAt, "j M")

    return (
        f"Order #{order.pk} completes by itself on {day}. "
        "Check your delivery in Orders before then and tell us if anything is wrong."
    )


@transaction.atomic
def sendViewingReminders():
    now = timezone.now()

    booked = ViewingBooking.objects.filter(
        status="confirmed",
        buyer__is_active=True,
        slot__removed=False,
        slot__listing__status="live",
        slot__listing__seller__is_active=True,
        slot__startsAt__gt=now,
        slot__startsAt__lte=now + timedelta(hours=(24)),
    ).filter(Q(reminderSentAt__isnull=True) | Q(slot__reminderSentAt__isnull=True))

    sent = 0

    for booking in booked.select_related("slot__listing"):
        slot = booking.slot
        words = visitWords(slot, now)
        anchor = f"#slot-{slot.pk}"

        if ViewingBooking.objects.filter(pk=booking.pk, reminderSentAt__isnull=True).update(
            reminderSentAt=now
        ):
            notify(
                booking.buyer_id,
                "viewing_reminder",
                words,
                reverse("my_offers") + anchor,
            )

            sent += 1

        if ViewingSlot.objects.filter(pk=slot.pk, reminderSentAt__isnull=True).update(
            reminderSentAt=now
        ):
            notify(
                slot.listing.seller_id,
                "viewing_reminder",
                words,
                reverse("seller_viewings") + anchor,
            )

            sent += 1

    return sent


@transaction.atomic
def nudgeOrders():
    now = timezone.now()
    addon = Q(items__kind="addon")

    unsent = Q(items__deliveredAt__isnull=True) | Q(items__deliveryFile="", items__deliveryNote="")

    orders = (
        Order.objects.filter(status="processing", reminderSentAt__isnull=True)
        .annotate(
            lastDelivered=Max("items__deliveredAt", filter=addon),
            waiting=Count("items", filter=addon & unsent),
        )
        .filter(
            waiting=0,
            lastDelivered__gt=now - timedelta(days=AUTO_COMPLETE_DAYS),
            lastDelivered__lte=now - timedelta(days=AUTO_COMPLETE_DAYS - (1)),
        )
    )

    sent = 0

    for order in orders:
        if Order.objects.filter(pk=order.pk, reminderSentAt__isnull=True).update(
            reminderSentAt=now
        ):
            notify(
                order.seller_id,
                "order_completing",
                order_text(order),
                reverse("my_orders") + f"#order-{order.pk}",
            )

            sent += 1

    return sent


@transaction.atomic
def nudgeDeals():
    from .deals import BY_KEY

    today = timezone.localdate()
    sent = 0

    for deal in (
        Deal.objects.filter(status="open").select_related("listing").prefetch_related("steps")
    ):
        step = next((step for step in deal.steps.all() if step.doneAt is None), None)

        if step is None or step.dueOn is None:
            continue

        dueSoon = step.dueOn <= today + timedelta(days=1) and step.reminderSentOn is None

        late = step.dueOn < today and (
            step.reminderSentOn is None or step.reminderSentOn <= step.dueOn
        )

        if not (dueSoon or late):
            continue

        if (
            not DealStep.objects.filter(pk=step.pk)
            .exclude(reminderSentOn=today)
            .update(reminderSentOn=today)
        ):
            continue

        info = BY_KEY[step.key]

        if late:
            when = "is overdue"
        else:
            when = "is due " + ("today" if step.dueOn == today else "tomorrow")

        words = f"“{info['title']}” {when} · {_place(deal.listing)}"

        people = {
            "seller": [deal.listing.seller_id],
            "buyer": [deal.buyer_id],
            "both": [deal.listing.seller_id, deal.buyer_id],
        }[info["who"]]

        for person in people:
            notify(person, "deal_due", words, reverse("deal_page", args=[deal.pk]))
            sent += 1

    return sent


def nudgeAll():
    sent = 0

    from .offeradvice import coachSlow

    for send in (
        sendViewingReminders,
        nudgeOrders,
        nudgeDeals,
        coachSlow,
    ):
        try:
            sent += send()
        except Exception as error:
            log.exception("Could not send reminders in %s", send.__name__)

    return sent


def autoNudge():
    if caches["resources"].add(REMINDER_KEY, True, (600)):
        return nudgeAll()

    return 0
