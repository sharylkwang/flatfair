from datetime import timedelta
from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, DecimalField, F, OuterRef, Prefetch, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce, Least
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from adminpanel.models import AuditLog
from storefront.models import (
    hasRole, listing_id, notAllowed, needEmail,
    lock_user, ACTIVE_BOOKINGS, PAUSE_REASONS, WITHDRAW_REASONS,
    ActivityNotice, Listing, Offer, SavedListing,
    ViewingAsk, ViewingBooking, ViewingSlot,
)
from sellerpanel.stripepay import savings
from storefront.activity import deal_away, sell_data, startSell
from storefront.listing_rules import ONE_FLAT_MESSAGE, otherOpen
from storefront.presentation import TIER_RANKS, alertRow, helloFor, readyPics, tierOf
from storefront.notices import _place, mailNotes, offer_notes, visit_note
from storefront.offeradvice import round1000
from storefront.reminders import autoNudge
from storefront.templatetags.estate_tags import buyer_name
from storefront.valuation import band, verdict
from .forms import ListingForm
from .models import ListingDraft
from .recommend import showTip
from .photos import DRAFTS_KEY, waitChecks, listingFor, moveBack
from .plans import packsNow, myExtras, focus_flat, sell_steps, stepFlats

OPEN_OFFER_STATUSES = ("pending", "countered")
STRIPE_ERROR = (
    "Sorry, we couldn’t reach Stripe just now. Your cart is still here, so please try again."
)
SKIP_KEY = "nextStepsSkip"


def sell_only(request):
    if hasRole(request.user, "buyer"):
        return render(request, "sellerpanel/buyer_account.html", status=403)

    return notAllowed(request, "seller")


def new_seller_account(request):
    return startSell(request)


def maybeId(value):
    try:
        return listing_id(value)
    except ValidationError:
        return None


def sureVisits(user):
    return ViewingBooking.objects.filter(
        slot__listing__seller=user,
        slot__removed=False,
        slot__startsAt__gte=timezone.now(),
        status="confirmed",
    ).count()


def lowerNote(listing, product):
    wanted = tierOf(product.tier)


    plans = packsNow([listing])

    for plan in plans:
        running = tierOf(plan["tier"])

        if running == "Top spot" and wanted == "Top spot":
            return "This flat already has Top spot running. It lasts until the flat is sold."

        if TIER_RANKS.get(running, 0) > TIER_RANKS.get(wanted, 0):
            return f'This flat already has the {plan["name"]} running. You can buy a cheaper package after it ends.'

    for plan in plans:
        if wanted and tierOf(plan["tier"]) == wanted:
            until = "until it is sold"

            if plan["expiry"]:
                until = "until " + date_format(timezone.localtime(plan["expiry"]), "j M")

            return f"{product.name} is already active on this flat {until}. You can add it again after it ends."



    return ""


def plural(count, word):
    if count == 1:
        return f"{count} {word}"

    return f"{count} {word}s"


# Show the seller page
@login_required
def dashboard(request):
    from sellerpanel.listings import sellSlots


    if not hasRole(request.user, "seller"):
        return sell_only(request)

    events = sell_data(request.user, request.GET)

    spent = (
        request.user.orders.exclude(status="refunded").aggregate(total=Sum("total"))["total"] or 0
    )

    packs = packsNow(request.user.listings.all())
    endPlan = None

    for package in packs:
        if package["expiry"] and (endPlan is None or package["expiry"] < endPlan["expiry"]):
            endPlan = package

    rows = []

    for flat in events["activityFlats"][:5]:
        flatVerdict = None


        if flat.status != "sold" and flat.estimatedPrice and flat.bandLow and flat.bandHigh:
            flatVerdict = verdict(
                flat.askingPrice, flat.bandLow, flat.estimatedPrice, flat.bandHigh
            )

        rows.append({"flat": flat, "verdict": flatVerdict})

    myFlat = focus_flat(request.user, maybeId(request.GET.get("flat")))
    stepPicks = list(stepFlats(request.user)[:6])
    plan = None

    if myFlat:
        status = f"Your {myFlat.town.title()} flat is {myFlat.get_status_display().lower()}"

        for package in packs:
            if package["listing"].pk == myFlat.pk:
                plan = package

                break
    else:
        status = "Listing is free. Register your Intent to Sell with HDB, then list your flat"

    if plan:
        untilEnd = "until sold"

        if plan["daysLeft"] is not None:
            untilEnd = f"{plan['daysLeft']} days left"

        status += f" · {plan['name'].replace(' package', '')} package, {untilEnd}"

    dealCost = Offer.objects.filter(listing=OuterRef("pk"), status="accepted").values("amount")[:1]


    sold = request.user.listings.filter(status="sold").annotate(
        soldPrice=Coalesce(Subquery(dealCost), F("askingPrice"), output_field=DecimalField())
    )

    soldSum = sold.aggregate(total=Sum("soldPrice"))["total"]
    fee = savings(soldSum or 0)["agentFee"]
    reviewNum = events["listingCounts"]["review"]


    alerts = []

    newOffers = Offer.objects.filter(
        listing__seller=request.user, listing__status="live", status="pending"
    ).count()

    booked = ViewingBooking.objects.filter(
        slot__listing__seller=request.user,
        slot__listing__status="live",
        slot__removed=False,
        slot__startsAt__gte=timezone.now(),
        status="requested",
    ).count()


    if newOffers:
        alerts.append(
            alertRow(
                "offer",
                "lilac",
                plural(newOffers, "offer") + " to answer",
                "Buyers are waiting for your reply.",
                reverse("seller_viewings") + "#offers-tab",
                "Review offers",
                "Answer " + plural(newOffers, "offer"),
            )
        )

    if booked:
        alerts.append(
            alertRow(
                "calendar",
                "sky",
                plural(booked, "viewing") + " to confirm",
                "Buyers have asked to view your flat.",
                reverse("seller_viewings") + "#viewings-tab",
                "Review viewings",
                "Confirm " + plural(booked, "viewing"),
            )
        )



    if endPlan and endPlan["expiry"] <= timezone.now() + timedelta(days=7):
        alerts.append(
            alertRow(
                "clock",
                "blossom",
                "Your package ends soon",
                f"{endPlan['name']} · {endPlan['daysLeft']} days left",
                reverse("my_orders"),
                "View package",
                "View my package",
            )
        )

    if reviewNum:
        word = "listing" if reviewNum == 1 else "listings"

        alerts.append(
            alertRow(
                "check",
                "periwinkle",
                f"{reviewNum} {word} being checked",
                "Buyers can’t see it until our team has checked it.",
                reverse("my_listings") + "?status=review",
                "View listings",
                f"See the {word} being checked",
            )
        )



    agenda = []

    for day in events["days"][:7]:
        for slot in day["slots"]:
            agenda.append(slot.pk)


    weekPlan = sellSlots(ViewingSlot.objects.filter(pk__in=agenda[:3]).select_related("listing"))

    weekPlan = list(weekPlan.order_by("startsAt", "pk"))

    dashOffers = list(
        addReply(Offer.objects.filter(listing__seller=request.user, status="pending")).order_by(
            "createdAt", "pk"
        )[:2]
    )

    for offer in dashOffers:
        offerAsks(offer)

    dashOrders = list(
        request.user.orders.prefetch_related("items__listing").order_by("-createdAt", "-pk")[:2]
    )

    readyPics(
        events["activityFlats"]
        + [myFlat]
        + [slot.listing for slot in weekPlan]
        + [offer.listing for offer in dashOffers]
        + [item.listing for order in dashOrders for item in order.items.all()]
        + [package["listing"] for package in packs]
    )

    steps = sell_steps(myFlat)
    ticked = 0

    for step in steps:
        if step["done"]:
            ticked += 1



    context = {
        **events,
        "greeting": helloFor(request.user),
        "dashboardStatus": status + ".",
        "attentionRows": alerts,
        "pendingOffers": newOffers,
        "weekAgenda": weekPlan,
        "dashboardOffers": dashOffers,
        "dashboardOrders": dashOrders,
        "upcomingCount": sureVisits(request.user),
        "mainFlat": myFlat,
        "packageRecommendation": showTip(myFlat),
        "stepChoices": stepPicks if len(stepPicks) > 1 else [],
        "liveCount": events["listingCounts"]["live"],
        "plan": plan,
        "packages": packs,
        "spent": spent,
        "saved": max(0, fee - spent),
        "rows": rows,
        "services": myExtras(request.user)[:3],
        "canListAnother": not otherOpen(request.user),
        "steps": steps,
        "stepsDone": ticked,
        "savedDraft": ListingDraft.objects.filter(seller=request.user).first(),
    }

    return render(request, "sellerpanel/dashboard.html", context)


# List a flat for sale


# Save the flat's changes


@login_required
@transaction.atomic
@needEmail
def toggle_sold(request, publicId):
    from sellerpanel.listings import lockMyFlat, sellState

    if request.method == "POST" and request.POST.get("sold") == "0":
        buyerId = (
            Offer.objects.filter(
                listing__publicId=publicId,
                listing__seller=request.user,
                listing__status="sold",
                status="accepted",
            )
            .values_list("buyer_id", flat=True)
            .first()
        )

        if buyerId:
            lock_user(buyerId)

    seller, flat, refused = lockMyFlat(request, publicId)

    if refused:
        return refused

    wanted = request.POST.get("sold")

    if wanted not in ("0", "1") or (wanted == "1") == (flat.status == "sold"):
        return redirect("my_listings")

    if flat.status == "sold" and otherOpen(seller, flat):
        messages.info(request, ONE_FLAT_MESSAGE)

        return redirect("my_listings")


    if flat.status == "sold":
        agreed = flat.offers.filter(status="accepted").first()

        if agreed:
            others = Offer.objects.filter(buyer_id=agreed.buyer_id, status="accepted")

            if others.exclude(listing=flat).exclude(listing__status="sold").exists():
                messages.error(
                    request,
                    "This buyer has already agreed a price on another flat. "
                    "Undo that agreement before relisting this flat.",
                )

                return redirect("my_listings")

    before = flat.status


    sellState(flat, "sold")

    if flat.status == before:
        return redirect("my_listings")

    flat.save(update_fields=["status", "statusBeforeSold"])
    closed = 0

    if flat.status == "sold":
        offers = list(
            Offer.objects.select_for_update()
            .filter(listing=flat, status__in=OPEN_OFFER_STATUSES)
            .select_related("listing")
        )

        closed = Offer.objects.filter(pk__in=[offer.pk for offer in offers]).update(
            status="declined", respondedAt=timezone.now()
        )

        offer_notes(offers, "offer_declined", "buyer")


        booked = list(
            ViewingBooking.objects.select_for_update()
            .filter(
                slot__listing=flat,
                slot__startsAt__gt=timezone.now(),
                status__in=("requested", "confirmed"),
            )
            .select_related("slot__listing")
        )

        closed += ViewingBooking.objects.filter(pk__in=[booking.pk for booking in booked]).update(
            status="cancelled"
        )

        for booking in booked:
            visit_note(booking.slot, "viewing_cancelled", [booking.buyer_id], "seller")


    if closed:
        messages.success(
            request, "Your flat is marked as sold. We’ve closed the open offers and viewings on it."
        )
    elif flat.status == "sold":
        messages.success(request, "Your flat is marked as sold.")
    elif flat.status == "live":
        messages.success(request, "Your flat is relisted. Buyers can see it again.")
    elif flat.status == "paused":
        messages.success(
            request,
            "Your flat is relisted but still paused. Resume it when you’re ready for buyers.",
        )
    else:
        messages.success(
            request, "Your flat is relisted. Our team will check it before buyers can see it."
        )


    return redirect("my_listings")


def addReply(offers):
    return offers.select_related("buyer", "listing").annotate(
        buyerAgreedElsewhere=deal_away(),
        othersOpen=Count(
            "listing__offers",
            filter=Q(listing__offers__status__in=OPEN_OFFER_STATUSES),
            distinct=True,
        ),
        flatTaken=Count(
            "listing__offers", filter=Q(listing__offers__status="accepted"), distinct=True
        ),
        counterLow=F("amount") + Value(Decimal("1")),
        counterHigh=Least("listing__askingPrice", Value(3000000)),
    )


def offerAsks(offer):
    name = buyer_name(offer.buyer)
    amount = f"${offer.amount:,.0f}"
    offer.acceptAsk = f"Accept {amount} from {name}?"
    others = offer.othersOpen - 1

    if others > 0:
        offer.acceptAsk += f" This will decline the other {plural(others, 'offer')} on this flat."

    if offer.status == "countered":
        offer.declineAsk = f"Decline {name}’s offer and close it?"
    else:
        offer.declineAsk = f"Decline {amount} from {name}?"

    offer.undoAsk = (
        f"Undo accepting {amount} from {name}? It will go back to waiting for your reply."
    )



    return offer


@login_required
@needEmail
def seller_viewings(request):
    from sellerpanel.listings import sellSlots

    if not hasRole(request.user, "seller"):
        return sell_only(request)

    autoNudge()

    toReply = addReply(Offer.objects.filter(listing__seller=request.user, status="pending"))

    toReply = [offerAsks(offer) for offer in toReply.order_by("createdAt", "pk")]
    oldOffers = Offer.objects.filter(listing__seller=request.user).exclude(status="pending")

    bidPage = Paginator(oldOffers.order_by("-createdAt", "-pk"), 20).get_page(
        request.GET.get("offers_page")
    )

    history = addReply(Offer.objects.filter(pk__in=[offer.pk for offer in bidPage]))
    history = history.order_by("-createdAt", "-pk")


    bidFlats = (
        Listing.objects.filter(pk__in=history.values("listing_id"))
        .annotate(acceptedCount=Count("offers", filter=Q(offers__status="accepted"), distinct=True))
        .prefetch_related(
            Prefetch(
                "offers",
                queryset=history.filter(status__in=("countered", "accepted")),
                to_attr="openHistory",
            ),
            Prefetch(
                "offers",
                queryset=history.filter(status__in=("declined", "withdrawn")),
                to_attr="closedHistory",
            ),
        )
        .order_by("-createdAt", "-pk")
    )


    for flat in bidFlats:
        for offer in flat.openHistory:
            offerAsks(offer)

    slots = ViewingSlot.objects.filter(
        listing__seller=request.user, removed=False, startsAt__gte=timezone.now()
    )

    slots = sellSlots(slots.select_related("listing")).order_by("replyOrder", "startsAt", "pk")
    slotPage = Paginator(slots, 20).get_page(request.GET.get("slots_page"))

    slotsNow = sellSlots(
        ViewingSlot.objects.filter(pk__in=[slot.pk for slot in slotPage]).select_related("listing")
    )

    waitSlots = list(slotsNow.filter(waitingCount__gt=0).order_by("startsAt", "pk"))
    planSlots = list(slotsNow.filter(waitingCount=0).order_by("startsAt", "pk"))

    oldSlots = ViewingSlot.objects.filter(
        listing__seller=request.user, removed=False, startsAt__lt=timezone.now()
    )

    oldSlots = list(oldSlots.select_related("listing").order_by("-startsAt", "-pk")[:20])
    historyFlats = {flat.pk: flat for flat in bidFlats}
    openParts = []
    soldParts = []


    for flat in request.user.listings.exclude(status="withdrawn").order_by("-createdAt", "-pk"):
        known = historyFlats.get(flat.pk)

        section = {
            "flat": known or flat,
            "reply": [offer for offer in toReply if offer.listing_id == flat.pk],
            "waiting": known.openHistory if known else [],
            "closed": known.closedHistory if known else [],
            "slots": [
                slot for slot in slotsNow.order_by("startsAt", "pk") if slot.listing_id == flat.pk
            ],
            "pastSlots": [slot for slot in oldSlots if slot.listing_id == flat.pk],
        }

        anyOffer = section["reply"] or section["waiting"] or section["closed"]

        if flat.status != "sold":
            openParts.append(section)
        elif anyOffer:
            soldParts.append(section)

    readyPics(
        [section["flat"] for section in openParts + soldParts]
        + [offer.listing for offer in toReply]
        + [offer.listing for flat in bidFlats for offer in flat.openHistory + flat.closedHistory]
        + [slot.listing for slot in waitSlots + planSlots]
    )



    now = timezone.localtime()

    context = {
        "replyOffers": toReply,
        "offerFlats": bidFlats,
        "offerPage": bidPage,
        "slotPage": slotPage,
        "waitingSlots": waitSlots,
        "plannedSlots": planSlots,
        "openSections": openParts,
        "soldSections": soldParts,
        "liveFlats": request.user.listings.filter(status="live").order_by("-createdAt", "-pk"),
        "upcomingCount": sureVisits(request.user),
        "slotMin": (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"),
        "slotMax": (now + timedelta(days=180)).strftime("%Y-%m-%dT%H:%M"),
    }


    return render(request, "sellerpanel/seller_viewings.html", context)


# Check this payment step


@login_required
@require_POST
@needEmail
def offer_floor(request, publicId):
    if not hasRole(request.user, "seller"):
        return redirect("home")

    flat = get_object_or_404(Listing, publicId=publicId, seller=request.user)
    turnOn = request.POST.get("on") == "1"



    if turnOn and not (flat.estimatedPrice and flat.bandLow):
        messages.error(request, "We can’t set a minimum until we have an estimate for this flat.")


        return redirect("seller_listing", publicId=flat.publicId)


    Listing.objects.filter(pk=flat.pk).update(autoDeclineLow=turnOn)

    if turnOn:
        messages.success(
            request,
            f"Offer minimum is on. We’ll decline offers below ${round1000(flat.bandLow):,} for you.",
        )
    else:
        messages.success(request, "Offer minimum is off. You’ll see every offer.")

    return redirect("seller_listing", publicId=flat.publicId)


@never_cache
@login_required
@require_POST
@needEmail
def photo_move_back(request):
    if not hasRole(request.user, "seller"):
        return sell_only(request)

    token = request.POST.get("draftToken")
    drafts = request.session.get(DRAFTS_KEY, {})

    if token not in drafts:
        messages.error(
            request, "Sorry, we couldn’t find that photo. Please reload the page and try again."
        )

        return redirect("sell")

    data = drafts[token]
    data["formData"] = {name: request.POST.get(name, "") for name in ListingForm.base_fields}
    room = request.POST.get("photoBack", "")
    moveBack(request, data, room)
    request.session.modified = True
    listing = listingFor(request, data)


    page = (
        reverse("edit_listing", kwargs={"publicId": listing.publicId})
        if listing
        else reverse("sell")
    )

    return redirect(f"{page}?draftToken={token}#sell-photos")


def stepsFor(request):
    user = request.user

    if not user.is_authenticated:
        return None

    if request.session.pop(SKIP_KEY, False):
        return None

    flat = Listing.objects.filter(seller=user, status="live", nextStepsSeenAt__isnull=True).first()

    if flat is None:
        return None


    from storefront.templatetags.offer_tags import share_links

    now = timezone.localtime()
    go_url = lambda step: reverse("next_step_go", args=[flat.publicId, step])
    times = ViewingSlot.objects.filter(listing=flat, startsAt__gte=now).count()
    boosted = bool(packsNow(Listing.objects.filter(pk=flat.pk)))

    steps = [
        {
            "key": "viewings",
            "title": "Add viewing times",
            "text": "Buyers can only visit at the times you add. Two or three this week is a good start.",
            "done": times > 0,
            "optional": False,
        },
        {
            "key": "share",
            "title": "Share your listing",
            "text": "Send the link to friends and family. Shared flats get more views.",
            "done": False,
            "optional": False,
        },
        {
            "key": "minimum",
            "title": "Set an offer minimum",
            "text": "We’ll decline offers below your minimum for you.",
            "done": flat.autoDeclineLow,
            "optional": True,
            "url": go_url("minimum"),
            "button": "Set a minimum",
        },
        {
            "key": "boost",
            "title": "Make it stand out",
            "text": "A boost shows your flat above free listings in search.",
            "done": boosted,
            "optional": True,
            "url": go_url("boost"),
            "button": "See boosts",
        },
    ]


    nextStep = next((step for step in steps if not step["done"]), None)

    for step in steps:
        step["next"] = step is nextStep


    place = f"Blk {flat.block} {flat.town.title()}" if flat.block else flat.town.title()

    return {
        "flat": flat,
        "place": place,
        "steps": steps,
        "times": times,
        "share": share_links({"request": request}, flat),
        "slotMin": (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"),
        "slotMax": (now + timedelta(days=180)).strftime("%Y-%m-%dT%H:%M"),
        "viewingsUrl": go_url("viewings"),
        "dismiss": reverse("next_steps_done", args=[flat.publicId]),
    }


@login_required
@require_POST
def next_steps_done(request, publicId):
    flat = get_object_or_404(Listing, publicId=publicId, seller=request.user)

    if flat.nextStepsSeenAt is None:
        Listing.objects.filter(pk=flat.pk).update(nextStepsSeenAt=timezone.now())

    if request.headers.get("X-Beacon") == "1":
        return HttpResponse(status=204)

    return redirect(request.POST.get("next") or reverse("seller_listing", args=[flat.publicId]))


@login_required
def next_step_go(request, publicId, step):
    flat = get_object_or_404(Listing, publicId=publicId, seller=request.user)
    request.session[SKIP_KEY] = True

    if step == "viewings":
        return redirect(reverse("seller_viewings") + "?first=1#slot-form")

    if step == "minimum":
        return redirect(reverse("seller_listing", args=[flat.publicId]) + "#offer-minimum")

    if step == "boost":
        return redirect(reverse("packages") + f"?listing={flat.publicId}")

    return redirect(reverse("seller_listing", args=[flat.publicId]))


PAUSE_WORDS = {
    "both": "The seller has paused {place} for now. Your offer and viewing stay as they are, and we’ll tell you when it’s back",
    "offer": "The seller has paused {place} for now. Your offer stays as it is, and we’ll tell you when it’s back",
    "viewing": "The seller has paused {place} for now. Your viewing stays booked, and we’ll tell you when it’s back",
    "saved": "{place}, a flat you saved, is paused for now. We’ll tell you when it’s back on sale",
}
RESUME_WORDS = {
    "both": "{place} is back on sale. Your offer and viewing are just as you left them",
    "offer": "{place} is back on sale, and your offer is still there",
    "viewing": "{place} is back on sale, and your viewing is still booked",
    "saved": "{place}, a flat you saved, is back on sale",
}
WITHDRAW_WORDS = {
    "both": "The seller has taken {place} off FlatFair, so we’ve closed your offer and cancelled your viewing",
    "offer": "The seller has taken {place} off FlatFair, so we’ve closed your offer",
    "viewing": "The seller has taken {place} off FlatFair, so we’ve cancelled your viewing",
    "saved": "{place}, a flat you saved, is no longer for sale. The seller has taken it off FlatFair",
}


def sellerFlat(request, publicId):
    if request.method == "POST":
        lock_user(request.user.pk)
        flats = Listing.objects.select_for_update()
    else:
        flats = Listing.objects.all()

    return get_object_or_404(flats, publicId=publicId, seller=request.user)


def followers(flat):
    offers = set(
        Offer.objects.filter(
            listing=flat, status__in=(("pending", "countered", "accepted"))
        ).values_list("buyer_id", flat=True)
    )



    visits = set(
        ViewingBooking.objects.filter(
            slot__listing=flat,
            slot__removed=False,
            slot__startsAt__gt=timezone.now(),
            status__in=ACTIVE_BOOKINGS,
        ).values_list("buyer_id", flat=True)
    )



    saved = set(
        SavedListing.objects.filter(listing=flat)
        .exclude(collection__user_id=flat.seller_id)
        .values_list("collection__user_id", flat=True)
    )


    return offers, visits, saved


def tellFans(flat, kind, words, people, savedUrl):
    offers, visits, saved = people


    place = _place(flat)
    notices = []

    for person in offers | visits | saved:
        if person in offers and person in visits:
            key = "both"
        elif person in offers:
            key = "offer"
        elif person in visits:
            key = "viewing"
        else:
            key = "saved"


        url = savedUrl if key == "saved" else reverse("my_offers")
        notices.append(
            ActivityNotice(
                user_id=person, kind=kind, text=words[key].format(place=place)[:200], url=url
            )
        )

    made = ActivityNotice.objects.bulk_create(notices)


    mailNotes(made)



    return len(made)


def record(request, action, flat, detail):
    AuditLog.objects.create(
        actor=request.user,
        action=action,
        target=f"Listing #{flat.pk} ({flat.town.title()})",
        detail=detail[:200],
    )


def pageFor(flat, part=""):
    return redirect(reverse("seller_listing", args=[flat.publicId]) + part)


@login_required
@needEmail
def pause_listing(request, publicId):
    if not hasRole(request.user, "seller"):
        return redirect("home")

    with transaction.atomic():
        flat = sellerFlat(request, publicId)

        if flat.status != "live":
            messages.info(request, "You can pause a flat only while buyers can see it.")

            return pageFor(flat)

        if request.method != "POST":
            offers, visits, saved = followers(flat)
            context = {
                "flat": flat,
                "reasons": PAUSE_REASONS,
                "toldCount": len(offers | visits | saved),
            }

            return render(request, "sellerpanel/pause_listing.html", context)

        reason = request.POST.get("reason", "")

        if reason not in dict(PAUSE_REASONS):
            reason = ""

        Listing.objects.filter(pk=flat.pk).update(
            status="paused", pausedAt=timezone.now(), pauseReason=reason
        )
        told = tellFans(flat, "flat_paused", PAUSE_WORDS, followers(flat), reverse("saved"))
        record(
            request,
            "listing paused by seller",
            flat,
            dict(PAUSE_REASONS).get(reason, "No reason given"),
        )


    if told:
        messages.success(
            request,
            f"Your listing is paused. We’ve let {told} interested buyer{'s' if told > 1 else ''} know.",
        )
    else:
        messages.success(
            request, "Your listing is paused. Buyers can’t find it until you resume it."
        )



    return pageFor(flat)


@login_required
@require_POST
@needEmail
def resume_listing(request, publicId):
    if not hasRole(request.user, "seller"):
        return redirect("home")

    with transaction.atomic():
        flat = sellerFlat(request, publicId)



        if flat.status != "paused":
            return pageFor(flat)

        Listing.objects.filter(pk=flat.pk).update(status="live", pausedAt=None, pauseReason="")
        flat.status = "live"
        url = reverse("listing_detail", args=[flat.publicId])
        told = tellFans(flat, "flat_resumed", RESUME_WORDS, followers(flat), url)
        record(request, "listing resumed by seller", flat, "Back on sale")

    if told:
        messages.success(
            request,
            f"Your listing is live again. We’ve let {told} interested buyer{'s' if told > 1 else ''} know.",
        )
    else:
        messages.success(request, "Your listing is live again. Buyers can find it now.")

    return pageFor(flat)


def withdrawReason(request):
    reason = request.POST.get("reason", "")
    note = " ".join(request.POST.get("note", "").split())[:200]

    if reason not in dict(WITHDRAW_REASONS):
        return None, note, "Please tell us why you’re deleting this listing."

    if reason == "other" and len(note) < 5:
        return None, note, "Please tell us in a few words why you’re deleting this listing."

    return reason, note, ""


def closeAll(flat, now):
    Offer.objects.filter(listing=flat, status__in=(("pending", "countered"))).update(
        status="declined", respondedAt=now
    )



    ViewingBooking.objects.filter(
        slot__listing=flat, slot__startsAt__gt=now, status__in=ACTIVE_BOOKINGS
    ).update(
        status="cancelled",
        cancelledBy="seller",
        cancelReason="withdrawn",
        cancelledAt=now,
        cancelCounts=False,
    )

    ViewingSlot.objects.filter(listing=flat, startsAt__gt=now).update(removed=True)
    ViewingAsk.objects.filter(listing=flat, answeredAt__isnull=True).update(answeredAt=now)


@login_required
@needEmail
def delete_listing(request, publicId):
    if not hasRole(request.user, "seller"):
        return redirect("home")


    with transaction.atomic():
        flat = sellerFlat(request, publicId)

        if flat.status == "sold":
            messages.info(request, "Sold flats stay in your records, so they can’t be deleted.")

            return redirect("my_listings")

        if flat.status not in (("live", "review", "paused", "rejected", "hidden")):
            return redirect("my_listings")

        if flat.offers.filter(status="accepted").exists():
            messages.error(
                request,
                "You’ve agreed a price with a buyer. Please undo that first, or mark the flat as sold.",
            )

            return pageFor(flat)

        offers, visits, saved = followers(flat)

        context = {
            "flat": flat,
            "reasons": WITHDRAW_REASONS,
            "offerCount": len(offers),
            "viewingCount": len(visits),
            "toldCount": len(offers | visits | saved),
            "chosen": request.POST.get("reason", ""),
            "note": request.POST.get("note", "")[:200],
        }


        if request.method != "POST":
            return render(request, "sellerpanel/delete_listing.html", context)

        reason, note, problem = withdrawReason(request)

        if problem:
            context["error"] = problem

            return render(request, "sellerpanel/delete_listing.html", context, status=400)

        now = timezone.now()
        people = (offers, visits, saved)


        closeAll(flat, now)
        Listing.objects.filter(pk=flat.pk).update(
            status="withdrawn", withdrawnAt=now, withdrawReason=reason, withdrawNote=note
        )
        told = tellFans(flat, "flat_withdrawn", WITHDRAW_WORDS, people, reverse("saved"))
        label = dict(WITHDRAW_REASONS)[reason]
        record(request, "listing deleted by seller", flat, f"{label}: {note}" if note else label)

    if told:
        messages.success(
            request,
            f"Your listing is deleted. We’ve let {told} interested buyer{'s' if told > 1 else ''} know, and you can list another flat whenever you like.",
        )
    else:
        messages.success(
            request, "Your listing is deleted. You can list another flat whenever you like."
        )

    return redirect("my_listings")
