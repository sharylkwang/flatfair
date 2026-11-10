from datetime import timedelta, timezone as dt_timezone
from decimal import InvalidOperation
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError, OperationalError
from django.db.models import Case, Exists, OuterRef, Value, When, Count, Max, Q, F
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.views.decorators.http import require_http_methods, require_POST
from .presentation import readyPics
from .notices import _place, _time, notify, offer_note, offer_notes, visit_note
from storefront.models import (
    lock_user, hasRole, notAllowed, needEmail,
    object_id, listing_id, ACTIVE_BOOKINGS, Listing,
    Offer, OfferMessage, Profile, ViewingAsk,
    ViewingSlot, ViewingBooking, picsByRoom, withUsed,
)
from .offeradvice import offerFloor, counterOk, stepsAfter, hintPrice
from .deals import cancelDeal, createDeal
from .reminders import autoNudge

OPEN = ("pending", "countered")
CANCEL_LIMIT = 3
CANCEL_DAYS = 30
BUYER_REASONS = [
    ("plans", "My plans changed"),
    ("time", "The time no longer works"),
    ("found", "I found another flat"),
    ("other", "Something else"),
]
SELLER_REASONS = [
    ("time", "I can’t make this time"),
    ("unavailable", "The flat is no longer available"),
    ("other", "Something else"),
]
PAST = ("withdrawn", "declined")
BUYER_ACTIONS = (
    "offer",
    "withdraw",
    "accept_counter",
    "decline_counter",
    "suggest",
    "book",
    "cancel",
    "buyer_reply",
)
AGREED_ELSEWHERE = (
    "You’ve already agreed a price on another flat. Ask that seller to undo it first."
)
OFFER_NOTICES = {
    "withdraw": ("offer_withdrawn", "seller"),
    "decline_counter": ("offer_declined", "seller"),
    "decline": ("offer_declined", "buyer"),
    "reopen": ("offer_reopened", "buyer"),
}


def money(raw):
    try:
        return forms.DecimalField(
            min_value=1, max_value=3000000, max_digits=7, decimal_places=0
        ).clean(raw)
    except ValidationError:
        raise ValidationError("Enter a whole-dollar amount between $1 and $3,000,000.")


def actionable(flat):
    if flat.status == "paused":
        raise ValidationError(
            "The seller has paused this flat for now. You can do this again once it’s back on sale."
        )

    if flat.status != "live" or not flat.seller_id or not flat.seller.is_active:
        raise ValidationError("This flat isn’t open for offers or viewings.")


def lock_flat(pk):
    owner_id = get_object_or_404(Listing, pk=pk).seller_id

    if owner_id:
        lock_user(owner_id)

    return get_object_or_404(Listing.objects.select_for_update().select_related("seller"), pk=pk)


def deal_away():
    others = Offer.objects.filter(buyer=OuterRef("buyer"), status="accepted").exclude(
        listing=OuterRef("listing")
    )

    return Exists(others.exclude(listing__status="sold"))


def take_offer(offer, buyer_counter=False):
    flat = offer.listing
    actionable(flat)

    if Offer.objects.filter(listing=flat, status="accepted").exists():
        raise ValidationError("You’ve already accepted another offer. Undo that one first.")

    others = Offer.objects.filter(buyer_id=offer.buyer_id, status="accepted").exclude(listing=flat)

    if others.exclude(listing__status="sold").exists():
        if buyer_counter:
            raise ValidationError(AGREED_ELSEWHERE)

        raise ValidationError(
            "This buyer has already agreed a price on another flat, so you can’t accept their offer yet."
        )

    wanted = "countered" if buyer_counter else "pending"



    values = {"status": "accepted", "respondedAt": timezone.now()}

    if buyer_counter:
        if not offer.counterAmount:
            raise ValidationError(
                "The seller changed their price. Please reload the page to see it."
            )

        values["amount"] = offer.counterAmount
        values["counterAmount"] = None

    if not Offer.objects.filter(pk=offer.pk, status=wanted).update(**values):
        raise ValidationError("This offer has changed. Please reload the page before you reply.")

    other_offers = Offer.objects.filter(listing=flat, status__in=OPEN).exclude(pk=offer.pk)
    offer_notes(other_offers.select_related("listing"), "offer_declined", "buyer")
    other_offers.update(status="declined", respondedAt=timezone.now())


def new_offer(request, pk):
    flat = lock_flat(get_object_or_404(Listing, publicId=pk).pk)


    actionable(flat)

    if Offer.objects.filter(
        listing=flat, buyer=request.user, status__in=(*OPEN, "accepted")
    ).exists():
        raise ValidationError("You already have an active offer on this flat.")

    just_now = timezone.now() - timedelta(minutes=1)

    if Offer.objects.filter(buyer=request.user, createdAt__gte=just_now).count() >= 5:
        raise ValidationError("Please wait a minute before sending another offer.")

    message = request.POST.get("message", "").replace("\r\n", "\n").strip()

    if len(message) > Offer._meta.get_field("message").max_length:
        raise ValidationError({"message": "Please keep your message to 300 characters or fewer."})

    offer = Offer(
        listing=flat,
        buyer=request.user,
        amount=money(request.POST.get("amount")),
        message=message,
        hasHfe=request.POST.get("hasHfe") == "yes",
    )

    offer.full_clean()


    floor = offerFloor(flat)

    if floor and offer.amount < floor:
        offer.status = "declined"
        offer.respondedAt = timezone.now()
        offer.reply = f"Thanks for your offer. This seller considers offers from ${floor:,} (what similar flats sold for), so it was declined automatically. You’re welcome to offer again."


        offer.save()
        offer_note(offer, "offer_declined_auto", "buyer")
        offer_note(offer, "offer_declined_auto", "seller")

        return (
            (
                messages.WARNING,
                f"This seller considers offers from ${floor:,}, so your offer of ${offer.amount:,.0f} was declined automatically. You can offer again.",
            ),
            f"offer-{offer.pk}",
        )

    offer.save()
    offer_note(offer, "offer_new", "seller")

    return f"Your offer of ${offer.amount:,.0f} has been sent.", f"offer-{offer.pk}"


def reply_offer(request, kind, pk):
    scope = {"buyer": request.user} if kind in BUYER_ACTIONS else {"listing__seller": request.user}
    stamp = get_object_or_404(Offer, pk=pk, **scope)
    flat = lock_flat(stamp.listing_id)



    offer = get_object_or_404(Offer.objects.select_for_update(), pk=pk, **scope)
    offer.listing = flat
    anchor = f"offer-{pk}"

    if kind == "reply":
        text = " ".join(request.POST.get("reply", "").split())[:300]

        if not text:
            raise ValidationError("Write a reply before sending.")

        if offer.status not in (*OPEN, "accepted"):
            raise ValidationError("This offer is closed, so you can’t reply to it.")

        Offer.objects.filter(pk=pk).update(reply=text)
        OfferMessage.objects.create(offer=offer, by="seller", text=text)
        offer_note(offer, "offer_reply", "buyer")

        return "Reply sent to the buyer.", anchor

    if kind == "buyer_reply":
        text = " ".join(request.POST.get("reply", "").split())[:300]

        if not text:
            raise ValidationError("Write a reply before sending.")

        if offer.status not in (*OPEN, "accepted"):
            raise ValidationError("This offer is closed, so you can’t reply to it.")

        OfferMessage.objects.create(offer=offer, by="buyer", text=text)



        offer_note(offer, "offer_buyer_reply", "seller")

        return "Reply sent to the seller.", anchor

    if kind in ("accept", "accept_counter"):
        if kind == "accept_counter":
            counter_box = forms.DecimalField(
                min_value=1,
                max_value=3000000,
                max_digits=10,
                decimal_places=2,
            )

            if counter_box.clean(request.POST.get("counterAmount")) != offer.counterAmount:
                raise ValidationError(
                    "The seller changed their price. Please reload the page to see it."
                )

        take_offer(offer, kind == "accept_counter")
        createDeal(Offer.objects.select_related("listing").get(pk=offer.pk))

        from .notices import fanAlerts

        transaction.on_commit(lambda listingId=flat.pk: fanAlerts(listingId, ["saved_under_offer"]))
        offer_note(offer, "offer_accepted", "seller" if kind == "accept_counter" else "buyer")

        if kind == "accept_counter":
            return (
                f"You accepted ${offer.counterAmount:,.0f}. "
                "Next, the seller will give you HDB’s Option to Purchase.",
                anchor,
            )

        return "Offer accepted. The other offers have been declined.", anchor

    if kind == "suggest":
        actionable(flat)

        if Offer.objects.filter(pk=pk).filter(deal_away()).exists():
            raise ValidationError(AGREED_ELSEWHERE)

        return hintPrice(offer, money(request.POST.get("amount"))), anchor

    if kind == "reopen":
        if flat.status == "sold":
            raise ValidationError("This flat is sold. Relist it first to undo the acceptance.")

        wanted, status, note = (
            ("accepted",),
            "pending",
            "Acceptance undone. The offer is open again.",
        )
    elif kind == "withdraw":
        wanted, status, note = OPEN, "withdrawn", "Your offer has been withdrawn."
    elif kind == "decline_counter":
        wanted, status, note = (
            ("countered",),
            "withdrawn",
            "You declined the seller’s price and closed your offer.",
        )
    elif kind == "decline":
        wanted, status, note = OPEN, "declined", "You declined this offer."
    else:
        actionable(flat)
        amount = money(request.POST.get("amount"))

        if amount <= offer.amount or amount > min(flat.askingPrice, 3000000):
            raise ValidationError(
                "Your new price must be more than the buyer offered and no more than your asking price."
            )

        if offer.status not in OPEN:
            raise ValidationError("This offer is no longer open.")

        if Offer.objects.filter(listing=flat, status="accepted").exists():
            raise ValidationError("You’ve already accepted another offer. Undo that one first.")

        counterOk(offer, amount)
        open_bid = Offer.objects.filter(pk=pk, status__in=OPEN)

        open_bid.update(
            counterAmount=amount,
            status="countered",
            respondedAt=timezone.now(),
            priceSteps=stepsAfter(offer, amount),
        )

        offer.counterAmount = amount
        offer_note(offer, "offer_countered", "buyer")



        return f"New price of ${amount:,.0f} sent to the buyer.", anchor

    updated = Offer.objects.filter(pk=pk, status__in=wanted).update(
        status=status,
        respondedAt=timezone.now(),
    )

    if not updated:
        raise ValidationError("This offer is no longer open.")

    if kind == "reopen":
        cancelDeal(offer)

    if kind in OFFER_NOTICES:
        offer_note(offer, *OFFER_NOTICES[kind])


    return note, anchor


def cancels(user, by):
    since = timezone.now() - timedelta(days=CANCEL_DAYS)



    counted = ViewingBooking.objects.filter(
        cancelCounts=True, cancelledBy=by, cancelledAt__gte=since
    )

    if by == "buyer":
        return list(
            counted.filter(buyer=user).order_by("cancelledAt").values_list("cancelledAt", flat=True)
        )

    slotFirst = {}

    for slotId, when in (
        counted.filter(slot__listing__seller=user)
        .order_by("cancelledAt")
        .values_list("slot_id", "cancelledAt")
    ):
        slotFirst.setdefault(slotId, when)

    return sorted(slotFirst.values())


def cool_off(user, by):
    times = cancels(user, by)

    if len(times) < CANCEL_LIMIT:
        return None

    return times[-CANCEL_LIMIT] + timedelta(days=CANCEL_DAYS)


def stopPaused(user, by, doing):
    until = cool_off(user, by)

    if until:
        day = date_format(timezone.localtime(until), "j M")

        raise ValidationError(
            f"You’ve cancelled {CANCEL_LIMIT} confirmed viewings in the last {CANCEL_DAYS} days, "
            f"so you can’t {doing} until {day}."
        )


def cancelReason(request, choices):
    reason = request.POST.get("reason", "")
    note = " ".join(request.POST.get("note", "").split())[:200]
    labels = dict(choices)

    if reason not in labels:
        raise ValidationError("Please tell us why you’re cancelling.")

    if reason == "other" and len(note) < 5:
        raise ValidationError("Please tell us in a few words why you’re cancelling.")

    return reason, note, note if reason == "other" else labels[reason]


def new_slot(request, pk):
    flat = lock_flat(get_object_or_404(Listing, publicId=pk).pk)

    if flat.seller_id != request.user.pk:
        raise Http404

    actionable(flat)
    stopPaused(request.user, "seller", "add viewing times")
    starts = forms.DateTimeField().clean(request.POST.get("startsAt"))

    try:
        starts = starts.astimezone(dt_timezone.utc)
    except (OverflowError, ValueError):
        raise ValidationError("Choose a viewing time within the next 180 days.")

    if not timezone.now() < starts <= timezone.now() + timedelta(days=180):
        raise ValidationError("Choose a viewing time within the next 180 days (Singapore time).")

    capacity = forms.IntegerField(min_value=1, max_value=50).clean(request.POST.get("capacity", 2))
    slot = ViewingSlot.objects.select_for_update().filter(listing=flat, startsAt=starts).first()

    if slot:
        if not slot.removed:
            raise ValidationError("You already have a viewing at this time.")


        slot.removed = False
        slot.capacity = capacity
        slot.save(update_fields=["removed", "capacity"])
    else:
        slot = ViewingSlot.objects.create(listing=flat, startsAt=starts, capacity=capacity)

    told = answerAsks(flat)

    if told:
        return (
            f"Viewing time added. We’ve told the {told} buyer{'s' if told > 1 else ''} who asked.",
            f"slot-{slot.pk}",
        )

    return "Viewing time added.", f"slot-{slot.pk}"


def edit_visit(request, kind, pk):
    if kind in ("book", "remove_slot"):
        stamp = get_object_or_404(ViewingSlot, pk=pk)
    else:
        scope = (
            {"buyer": request.user} if kind == "cancel" else {"slot__listing__seller": request.user}
        )
        stamp = get_object_or_404(
            ViewingBooking.objects.select_related("slot"), pk=pk, **scope
        ).slot

    flat = lock_flat(stamp.listing_id)
    slot = get_object_or_404(ViewingSlot.objects.select_for_update(), pk=stamp.pk)
    anchor = f"slot-{slot.pk}"

    if kind == "remove_slot":
        if flat.seller_id != request.user.pk:
            raise Http404

        if slot.startsAt <= timezone.now():
            raise ValidationError("This viewing time has already passed, so it can’t be removed.")

        if (
            slot.bookings.filter(status="confirmed").exists()
            and request.POST.get("confirm") != "yes"
        ):
            raise ValidationError(
                "Please confirm that you want to cancel this viewing. We’ll let the buyers know."
            )

        open_visits = slot.bookings.filter(status__in=("requested", "confirmed"))
        reason, note, words = "", "", ""

        if open_visits.filter(status="confirmed").exists():
            reason, note, words = cancelReason(request, SELLER_REASONS)

        visit_note(
            slot,
            "viewing_cancelled",
            open_visits.values_list("buyer_id", flat=True),
            "seller",
            words,
        )
        now = timezone.now()
        open_visits.filter(status="confirmed").update(cancelCounts=True)
        told = open_visits.update(
            status="cancelled",
            cancelledBy="seller",
            cancelReason=reason,
            cancelNote=note,
            cancelledAt=now,
        )

        if slot.bookings.exists():
            slot.removed = True
            slot.save(update_fields=["removed"])
        else:
            slot.delete()
            anchor = "viewing-calendar"

        if told == 1:
            return "Viewing time removed. We’ve let the buyer know.", "viewing-calendar"

        if told:
            return f"Viewing time removed. We’ve let the {told} buyers know.", "viewing-calendar"


        return "Viewing time removed.", "viewing-calendar"

    if kind == "book":
        actionable(flat)

        if flat.seller_id == request.user.pk:
            raise ValidationError("You can’t book a viewing of your own flat.")

        stopPaused(request.user, "buyer", "book another viewing")
        booking = ViewingBooking.objects.filter(slot=slot, buyer=request.user).first()

        if booking and booking.status != "cancelled":
            raise ValidationError("You’ve already booked this viewing time.")

        if slot.removed or slot.startsAt <= timezone.now() or slot.used >= slot.capacity:
            raise ValidationError(
                "This viewing time is full or no longer available. Please pick another time."
            )

        if booking:
            booking.status = "requested"
            booking.cancelledBy = booking.cancelReason = booking.cancelNote = ""


            booking.cancelledAt = None


            booking.cancelCounts = False
            booking.movedFromStatus = ""
            booking.save(
                update_fields=[
                    "status",
                    "cancelledBy",
                    "cancelReason",
                    "cancelNote",
                    "cancelledAt",
                    "cancelCounts",
                    "movedFromStatus",
                ]
            )
        else:
            ViewingBooking.objects.create(slot=slot, buyer=request.user)

        visit_note(slot, "viewing_booked", [flat.seller_id], "buyer")


        local = timezone.localtime(slot.startsAt)
        when = date_format(local, "j M, g:i") + date_format(local, "A").lower()

        return (
            f"Viewing requested for {when}. " "Waiting for the seller to confirm.",
            anchor,
        )

    booking = get_object_or_404(ViewingBooking.objects.select_for_update(), pk=pk, **scope)

    if booking.status not in ("requested", "confirmed") or slot.startsAt <= timezone.now():
        raise ValidationError("This viewing has already passed or been cancelled.")

    if kind == "confirm":
        if booking.status != "requested":
            raise ValidationError("This viewing has already been confirmed.")

        if flat.status != "paused":
            actionable(flat)

        if slot.removed or slot.used > slot.capacity:
            raise ValidationError("This viewing time is full or was removed. Remove a buyer first.")

    status = "confirmed" if kind == "confirm" else "cancelled"
    change = {"status": status}
    words = ""

    if kind == "cancel" or (kind == "seller_cancel" and booking.status == "confirmed"):
        reason, note, words = cancelReason(
            request, BUYER_REASONS if kind == "cancel" else SELLER_REASONS
        )

        change.update(
            cancelledBy="buyer" if kind == "cancel" else "seller",
            cancelReason=reason,
            cancelNote=note,
            cancelledAt=timezone.now(),
            cancelCounts=booking.status == "confirmed" or booking.movedFromStatus == "confirmed",
        )

    if not ViewingBooking.objects.filter(pk=pk, status=booking.status).update(**change):
        raise ValidationError("This viewing has changed. Please reload the page and try again.")

    if kind == "cancel":
        visit_note(slot, "viewing_cancelled", [flat.seller_id], "buyer", words)
    elif kind == "seller_cancel":
        visit_note(slot, "viewing_cancelled", [booking.buyer_id], "seller", words)
    elif kind == "confirm":
        visit_note(slot, "viewing_confirmed", [booking.buyer_id], "seller")


    if kind == "seller_cancel":
        if booking.status == "requested":
            return "You turned down this viewing request. We’ll let the buyer know.", anchor

        return "Buyer removed from this viewing. We’ll let them know.", anchor

    return "Viewing confirmed." if kind == "confirm" else "Viewing cancelled.", anchor


def mutate(request, kind, pk):
    if kind == "accept":
        offer = get_object_or_404(Offer, pk=pk, listing__seller=request.user)


        buyer = lock_user(offer.buyer_id)

        if not buyer or not buyer.is_active:
            raise ValidationError("This buyer’s account is no longer active.")

    lock_user(request.user.pk)

    if kind == "offer":
        return new_offer(request, pk)

    if kind in (
        (
            "withdraw",
            "accept_counter",
            "decline_counter",
            "suggest",
            "accept",
            "decline",
            "counter",
            "reopen",
            "reply",
            "buyer_reply",
        )
    ):
        return reply_offer(request, kind, pk)

    if kind == "slot":
        return new_slot(request, pk)

    return edit_visit(request, kind, pk)


def cancelUrl(request):
    name = "slot" if request.POST.get("action") == "remove_slot" else "booking"

    return reverse("cancel_viewing") + f"?{name}={request.POST.get('id', '')}"


@login_required
@needEmail
def cancel_viewing(request):
    try:
        if request.GET.get("slot"):
            slot = get_object_or_404(
                ViewingSlot.objects.select_related("listing"),
                pk=object_id(request.GET.get("slot")),
                listing__seller=request.user,
                removed=False,
            )

            people = list(
                slot.bookings.filter(status__in=("requested", "confirmed")).select_related("buyer")
            )
            kind, target, by_whom = "remove_slot", slot.pk, "seller"
            counts = any(booking.status == "confirmed" for booking in people)
        else:
            booking = get_object_or_404(
                ViewingBooking.objects.select_related("slot__listing", "buyer"),
                Q(buyer=request.user) | Q(slot__listing__seller=request.user),
                pk=object_id(request.GET.get("booking")),
                status__in=("requested", "confirmed"),
            )

            slot = booking.slot


            by_whom = "buyer" if booking.buyer_id == request.user.pk else "seller"
            kind, target, people = (
                "cancel" if by_whom == "buyer" else "seller_cancel",
                booking.pk,
                [booking],
            )
            counts = booking.status == "confirmed" or (
                by_whom == "buyer" and booking.movedFromStatus == "confirmed"
            )
    except ValidationError:
        raise Http404

    back = reverse("my_offers" if by_whom == "buyer" else "seller_viewings") + f"#slot-{slot.pk}"



    if slot.startsAt <= timezone.now():
        messages.error(request, "This viewing has already passed, so it can’t be cancelled.")

        return redirect(back)

    used = len(cancels(request.user, by_whom))

    context = {
        "slot": slot,
        "flat": slot.listing,
        "people": people,
        "by": by_whom,
        "kind": kind,
        "target": target,
        "reasons": BUYER_REASONS if by_whom == "buyer" else SELLER_REASONS,
        "back": back,
        "counts": counts,
        "cancelNumber": used + 1,
        "cancelLimit": CANCEL_LIMIT,
        "cancelDays": CANCEL_DAYS,
        "lastAllowed": used + 1 >= CANCEL_LIMIT,
        "chosen": request.GET.get("reason", ""),
    }

    return render(request, "storefront/cancel_viewing.html", context)


@login_required
@require_POST
@needEmail
def action(request):
    kind = request.POST.get("action")
    buyer = kind in BUYER_ACTIONS
    want_role = "buyer" if buyer else "seller"

    if not hasRole(request.user, want_role) or kind not in (
        *BUYER_ACTIONS,
        *(
            (
                "accept",
                "decline",
                "counter",
                "reopen",
                "reply",
                "slot",
                "remove_slot",
                "confirm",
                "seller_cancel",
            )
        ),
    ):
        return HttpResponseForbidden("Turn on buying or selling on your account to do this.")

    target = reverse("my_offers" if buyer else "seller_viewings")

    try:
        if kind in ("offer", "slot"):
            to_id = listing_id(request.POST.get("id"))
        else:
            to_id = object_id(request.POST.get("id"))

        with transaction.atomic():
            note, anchor = mutate(request, kind, to_id)

        if isinstance(note, tuple):
            messages.add_message(request, *note)
        else:
            messages.success(request, note)


        if kind == "offer":
            return redirect(reverse("listing_detail", args=[to_id]) + "#listing-action-card")

        if kind == "book":
            flatPage = flat_back(kind, request.POST.get("id"))

            if flatPage:
                return redirect(flatPage.replace("#listing-action-card", "#your-viewing"))

        return redirect(target + "#" + anchor)
    except (ValidationError, InvalidOperation, OverflowError) as error:
        if isinstance(error, ValidationError):
            message = " ".join(error.messages)
        else:
            message = "Please check the amount or date and try again."

        back = flat_back(kind, request.POST.get("id"))

        if kind == "offer" and back:
            from .views import listing_detail

            field = "message" if "message" in getattr(error, "error_dict", {}) else "amount"
            request.offer_error = {"field": field, "text": message}

            return listing_detail(request, to_id)

        messages.error(request, message)

        if request.POST.get("cancelPage") and kind in ("cancel", "seller_cancel", "remove_slot"):
            back = cancelUrl(request)
    except (IntegrityError, OperationalError):
        messages.error(
            request, "Something changed while you were on this page. Please reload and try again."
        )
        back = flat_back(kind, request.POST.get("id"))

    if not back and kind == "slot":
        back = target + "#slot-form"


    return redirect(back or target)


def flat_back(kind, raw_id):
    try:
        if kind == "offer":
            flats = Listing.objects.public().filter(publicId=listing_id(raw_id))
        elif kind == "book":
            flats = Listing.objects.public().filter(viewingSlots__pk=object_id(raw_id))
        else:
            return None
    except ValidationError:
        return None

    publicId = flats.values_list("publicId", flat=True).first()

    if not publicId:
        return None



    return reverse("listing_detail", args=[publicId]) + "#listing-action-card"


def buy_flats(offers, bookings):
    groups = {}

    for offer in offers:
        group = groups.setdefault(
            offer.listing_id, {"flat": offer.listing, "offers": [], "bookings": [], "rank": 2}
        )
        group["offers"].append(offer)

        if offer.status == "countered":
            group["rank"] = 0

    for booking in bookings:
        flat = booking.slot.listing
        group = groups.setdefault(flat.pk, {"flat": flat, "offers": [], "bookings": [], "rank": 2})
        group["bookings"].append(booking)

        if booking.status == "confirmed" and group["rank"] == 2:
            group["rank"] = 1


    return sorted(groups.values(), key=lambda group: group["rank"])


def buy_data(user, params=None):
    autoNudge()
    params = params or {}
    offer_set = Offer.objects.filter(buyer=user)

    summary = offer_set.aggregate(
        pendingOffers=Count("pk", filter=Q(status="pending")),
        acceptedOffers=Count("pk", filter=Q(status="accepted")),
    )

    bid_rows = offer_set.select_related("listing__seller")
    due_first = Case(When(status="accepted", then=Value(1)), default=Value(0))

    live = bid_rows.exclude(status__in=PAST).annotate(
        waitingFirst=due_first, agreedElsewhere=deal_away()
    )


    live = live.order_by("waitingFirst", "-createdAt", "-pk")
    offers = Paginator(live, 10).get_page(params.get("offers_page"))
    past_rows = bid_rows.filter(status__in=PAST).order_by("-createdAt", "-pk")
    old_bids = Paginator(past_rows, 10).get_page(params.get("past_offers_page"))
    booked = ViewingBooking.objects.filter(buyer=user).select_related("slot__listing")
    booked = booked.order_by("slot__startsAt", "pk")
    now = timezone.now()
    soon = booked.filter(slot__startsAt__gte=now).exclude(status="cancelled")
    past = booked.filter(slot__startsAt__lt=now).exclude(status="cancelled")


    dropped = booked.filter(status="cancelled")
    soon_page = Paginator(soon, 10).get_page(params.get("upcoming_page"))
    old_page = Paginator(past, 10).get_page(params.get("past_page"))
    drop_page = Paginator(dropped, 10).get_page(params.get("cancelled_page"))

    readyPics(
        [offer.listing for offerPage in (offers, old_bids) for offer in offerPage]
        + [
            booking.slot.listing
            for bookingPage in (soon_page, old_page, drop_page)
            for booking in bookingPage
        ]
    )


    next_up = soon.first()

    return {
        **summary,
        "offers": offers,
        "offerPage": offers,
        "pastOffers": old_bids,
        "nextViewing": next_up,
        "buyerFlats": buy_flats(offers, soon_page),
        "pastFlats": buy_flats(old_bids, [*old_page, *drop_page]),
        "upcomingBookings": soon_page,
        "pastBookings": old_page,
        "cancelledBookings": drop_page,
    }


def sell_data(user, params=None):
    params = params or {}
    now = timezone.now()
    flats = Listing.objects.filter(seller=user).exclude(status="withdrawn")


    flat_nums = flats.aggregate(
        live=Count("pk", filter=Q(status="live")),
        review=Count("pk", filter=Q(status="review")),
        paused=Count("pk", filter=Q(status="paused")),
        sold=Count("pk", filter=Q(status="sold")),
        hidden=Count("pk", filter=Q(status="hidden")),
        rejected=Count("pk", filter=Q(status="rejected")),
    )

    flat_total = flats.count()

    if params.get("status") in (("live", "review", "paused", "sold", "hidden", "rejected")):
        flats = flats.filter(status=params["status"])

    flats = flats.prefetch_related(picsByRoom()).annotate(
        upcomingSlotCount=Count(
            "viewingSlots",
            filter=Q(viewingSlots__removed=False, viewingSlots__startsAt__gte=now),
            distinct=True,
        ),
        acceptedCount=Count("offers", filter=Q(offers__status="accepted"), distinct=True),
        offerCount=Count("offers", distinct=True),
        savedCount=Count("savedBy__collection__user", distinct=True),
        openOfferCount=Count("offers", filter=Q(offers__status__in=OPEN), distinct=True),
        bestOffer=Max("offers__amount", filter=Q(offers__status__in=(*OPEN, "accepted"))),
    )

    flat_page = Paginator(flats.order_by("-createdAt", "-pk"), 20).get_page(
        params.get("listings_page")
    )

    slots = ViewingSlot.objects.filter(
        listing__seller=user,
        removed=False,
        startsAt__gte=now,
        startsAt__lt=now + timedelta(days=10),
    ).order_by("startsAt", "pk")



    today = timezone.localdate()
    days = []

    for offset in range(10):
        day = today + timedelta(days=offset)
        day_slots = []

        for slot in slots:
            if timezone.localdate(slot.startsAt) == day:
                day_slots.append(slot)



        days.append({"date": day, "slots": day_slots})

    soon = ViewingBooking.objects.filter(
        slot__listing__seller=user,
        slot__removed=False,
        slot__startsAt__gte=now,
        status__in=("requested", "confirmed"),
    )

    return {
        "activityFlats": list(flat_page),
        "listingCounts": flat_nums,
        "totalListingCount": flat_total,
        "listingPage": flat_page,
        "openOffers": Offer.objects.filter(listing__seller=user, status__in=OPEN).count(),
        "upcomingCount": soon.count(),
        "days": days,
    }


def stamp(moment):
    return moment.astimezone(dt_timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def escape(text):
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def dateFile(slot, title, uid):
    flat = slot.listing

    if flat.block:
        place = f"Blk {flat.block} {flat.town.title()}"
    else:
        place = flat.town.title()

    where = ", ".join(
        part
        for part in (place, f"Singapore {flat.postalCode}" if flat.postalCode else "")
        if part
    )


    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//FlatFair//Viewings//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uid}@flatfair",
        f"DTSTAMP:{stamp(timezone.now())}",
        f"DTSTART:{stamp(slot.startsAt)}",
        f"DTEND:{stamp(slot.startsAt + timedelta(minutes=30))}",
        f"SUMMARY:{escape(title + ' ' + place)}",
        f"LOCATION:{escape(where)}",
        "DESCRIPTION:Booked on FlatFair.",
        "BEGIN:VALARM",
        "TRIGGER:-PT2H",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{escape(title)}",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]

    reply = HttpResponse(
        "\r\n".join(lines) + "\r\n", content_type="text/calendar; charset=utf-8"
    )

    reply["Content-Disposition"] = (
        f'attachment; filename="flatfair-viewing-{slot.pk}.ics"'
    )


    reply["Cache-Control"] = "private, no-store"



    return reply


@login_required
def booking_calendar(request, pk):
    booking = get_object_or_404(
        ViewingBooking.objects.select_related("slot__listing"),
        pk=pk,
        buyer=request.user,
        status="confirmed",
    )

    return dateFile(booking.slot, "Flat viewing at", f"booking-{booking.pk}")


@login_required
def slot_calendar(request, pk):
    slot = get_object_or_404(
        ViewingSlot.objects.select_related("listing"),
        pk=pk,
        listing__seller=request.user,
        removed=False,
    )

    if not slot.bookings.filter(status="confirmed").exists():
        raise Http404

    return dateFile(slot, "Host a viewing at", f"slot-{slot.pk}")


def openAsk(flat, buyer):
    since = timezone.now() - timedelta(days=(7))

    return ViewingAsk.objects.filter(
        listing=flat, buyer=buyer, answeredAt__isnull=True, createdAt__gte=since
    ).first()


def answerAsks(flat):
    asks = list(ViewingAsk.objects.filter(listing=flat, answeredAt__isnull=True))

    ViewingAsk.objects.filter(pk__in=[ask.pk for ask in asks]).update(answeredAt=timezone.now())

    for ask in asks:
        notify(
            ask.buyer_id,
            "viewing_times_added",
            f"The seller added viewing times for {_place(flat)}. Pick one now",
            reverse("listing_detail", args=[flat.publicId]) + "#request-viewing",
        )

    return len(asks)


@login_required
@require_POST
@needEmail
def ask_viewing(request, publicId):
    flat = get_object_or_404(Listing.objects.public(), publicId=publicId)
    back = reverse("listing_detail", args=[flat.publicId]) + "#request-viewing"

    if not hasRole(request.user, "buyer") or flat.seller_id == request.user.pk:
        messages.error(request, "Only buyers can ask for a viewing time.")

        return redirect(back)

    with transaction.atomic():
        if openAsk(flat, request.user):
            messages.info(
                request,
                "You’ve already asked. We’ll tell you as soon as the seller adds times.",
            )

            return redirect(back)

        ViewingAsk.objects.create(listing=flat, buyer=request.user)

        waiting = ViewingAsk.objects.filter(
            listing=flat, answeredAt__isnull=True
        ).count()

        if waiting == 1:
            people = "A buyer would"
        else:
            people = f"{waiting} buyers would"

        notify(
            flat.seller_id,
            "viewing_asked",
            f"{people} like to view {_place(flat)}. Add some viewing times",
            reverse("seller_viewings") + "#viewings-tab",
        )



    messages.success(
        request,
        "We’ve asked the seller for viewing times. We’ll tell you when they add some.",
    )

    return redirect(back)


def enableMode(request, mode):
    if not any(hasRole(request.user, role) for role in ("buyer", "seller")):
        return notAllowed(request, mode)

    if mode == "seller":
        target = "sell"
    else:
        target = "listings"

    if request.method == "POST":
        with transaction.atomic():
            user = lock_user(request.user.pk)


            if not user or not user.is_active or user.is_staff:
                return notAllowed(request, mode)

            if mode == "seller":
                field = "canSell"
            else:
                field = "canBuy"

            Profile.objects.filter(user=user).update(**{field: True})

        request.session["marketplaceMode"] = mode

        return redirect(target)

    return render(request, "storefront/start_mode.html", {"startingMode": mode})


@login_required
@require_http_methods(["GET", "POST"])
def startSell(request):
    return enableMode(request, "seller")


@login_required
@require_http_methods(["GET", "POST"])
def startBuy(request):
    return enableMode(request, "buyer")


@login_required
@require_POST
def switchMode(request):
    mode = request.POST.get("mode")

    if not hasRole(request.user, mode):
        return notAllowed(request, "buyer or seller")

    request.session["marketplaceMode"] = mode

    return redirect("dashboard" if mode == "seller" else "listings")


def myBooking(request, raw):
    try:
        visit_id = object_id(raw)
    except Exception as error:
        raise Http404

    return get_object_or_404(
        ViewingBooking.objects.select_related("slot__listing__seller"),
        pk=visit_id,
        buyer=request.user,
        status__in=ACTIVE_BOOKINGS,
    )


def otherTimes(booking):
    taken = ViewingBooking.objects.filter(
        slot__listing=booking.slot.listing,
        buyer=booking.buyer,
        status__in=ACTIVE_BOOKINGS,
    ).values("slot_id")


    slots = ViewingSlot.objects.filter(
        listing=booking.slot.listing, removed=False, startsAt__gt=timezone.now()
    ).exclude(pk__in=taken)

    return (
        withUsed(slots).filter(usedCount__lt=F("capacity")).order_by("startsAt", "pk")
    )


def flatPage(booking):
    return (
        reverse("listing_detail", args=[booking.slot.listing.publicId])
        + "#your-viewing"
    )


def rebook(request, booking):
    flat = lock_flat(booking.slot.listing_id)

    booking = get_object_or_404(
        ViewingBooking.objects.select_for_update().select_related("slot"),
        pk=booking.pk,
        buyer=request.user,
        status__in=ACTIVE_BOOKINGS,
    )

    oldSlot = booking.slot

    if oldSlot.startsAt <= timezone.now():
        raise ValidationError("This viewing has already passed, so it can’t be moved.")



    if booking.movedFromStatus:
        raise ValidationError(
            (
                "You’ve already moved this viewing once. To change it again, please cancel it and book another time."
            )
        )

    actionable(flat)
    stopPaused(request.user, "buyer", "move a viewing")

    try:
        slotId = object_id(request.POST.get("slot"))
    except Exception as error:
        raise ValidationError("Please pick a new time.")

    newSlot = otherTimes(booking).filter(pk=slotId).first()

    if newSlot is None:
        raise ValidationError(
            "That time is full or no longer open. Please pick another time."
        )

    now = timezone.now()

    ViewingBooking.objects.filter(pk=booking.pk, status=booking.status).update(
        status="cancelled",
        cancelledBy="buyer",
        cancelReason="moved",
        cancelNote=f"Moved to {_time(newSlot.startsAt)}",
        cancelledAt=now,
        cancelCounts=False,
    )

    moved = ViewingBooking.objects.filter(slot=newSlot, buyer=request.user).first()



    if moved is None:
        moved = ViewingBooking(slot=newSlot, buyer=request.user)

    moved.status = "requested"
    moved.cancelledBy = moved.cancelReason = moved.cancelNote = ""
    moved.cancelledAt = None
    moved.cancelCounts = False
    moved.movedFromStatus = booking.status
    moved.createdAt = now
    moved.save()
    words = f"A buyer moved their viewing of {_place(flat)} from {_time(oldSlot.startsAt)} to {_time(newSlot.startsAt)}. Please agree to the new time"

    notify(
        flat.seller_id,
        "viewing_moved",
        words,
        reverse("seller_viewings") + f"#slot-{newSlot.pk}",
    )

    return newSlot


@login_required
@needEmail
def move_viewing(request):
    if not hasRole(request.user, "buyer"):
        raise Http404

    booking = myBooking(
        request, request.GET.get("booking") or request.POST.get("booking")
    )

    back = request.GET.get("from") or request.POST.get("from")

    if back == "offers":
        backUrl = reverse("my_offers") + f"#slot-{booking.slot_id}"
    else:
        backUrl = flatPage(booking)

    if request.method == "POST":
        try:
            with transaction.atomic():
                lock_user(request.user.pk)
                newSlot = rebook(request, booking)
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))



            return redirect(
                reverse("move_viewing") + f"?booking={booking.pk}&from={back or 'flat'}"
            )
        except IntegrityError:
            messages.error(
                request,
                "Something changed while you were on this page. Please try again.",
            )

            return redirect(
                reverse("move_viewing") + f"?booking={booking.pk}&from={back or 'flat'}"
            )

        messages.success(
            request,
            f"Your viewing is now {_time(newSlot.startsAt)}. We’ve asked the seller to agree to the new time.",
        )

        if back == "offers":
            return redirect(reverse("my_offers") + f"#slot-{newSlot.pk}")

        return redirect(flatPage(booking))


    if booking.slot.startsAt <= timezone.now():
        messages.error(
            request, "This viewing has already passed, so it can’t be moved."
        )

        return redirect(backUrl)

    flat = booking.slot.listing

    context = {
        "booking": booking,
        "flat": flat,
        "times": list(otherTimes(booking)[:12]),
        "movedBefore": bool(booking.movedFromStatus),
        "flatOpen": flat.status == "live" and flat.seller.is_active,
        "pausedUntil": cool_off(request.user, "buyer"),
        "cancelLimit": CANCEL_LIMIT,
        "cancelDays": CANCEL_DAYS,
        "back": backUrl,
        "from": back or "flat",
    }

    return render(request, "storefront/move_viewing.html", context)
