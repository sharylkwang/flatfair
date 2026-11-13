from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from adminpanel.access import canOpen
from storefront.models import needEmail, Deal, DealStep, Listing, ViewingBooking
from .notices import _place, notify, visit_note

STEPS = [
    {
        "key": "otp",
        "who": "seller",
        "days": 7,
        "title": "Grant the Option to Purchase",
        "about": "The seller gives the buyer HDB’s Option to Purchase (OTP) on the HDB Resale Portal. The buyer pays an option fee of $1 to $1,000.",
    },
    {
        "key": "valuation",
        "who": "buyer",
        "days": 1,
        "title": "Ask HDB for a valuation",
        "about": "The buyer sends HDB a Request for Value on the Resale Portal by the next working day after getting the OTP.",
    },
    {
        "key": "exercise",
        "who": "buyer",
        "days": 21,
        "title": "Exercise the Option to Purchase",
        "about": "The buyer signs the OTP within 21 days and pays the exercise fee. The option and exercise fees together are at most $5,000.",
    },
    {
        "key": "application",
        "who": "both",
        "days": 7,
        "title": "Send the resale application",
        "about": "Seller and buyer each send their part of the resale application on the HDB Resale Portal, within 7 days of each other.",
    },
    {
        "key": "approval",
        "who": "both",
        "days": 35,
        "title": "Get HDB’s approval",
        "about": "HDB checks both sides and approves the resale. Tick this when HDB’s approval letter arrives.",
    },
    {
        "key": "completion",
        "who": "both",
        "days": 56,
        "title": "Complete at the HDB appointment",
        "about": "The sale completes at HDB and the keys change hands. When this is ticked, FlatFair marks the flat as sold.",
    },
]

BY_KEY = {step["key"]: step for step in STEPS}
WHO = {"seller": "Seller", "buyer": "Buyer", "both": "Seller or buyer"}
STUCK_DAYS = 3


def createDeal(offer):
    if Deal.objects.filter(offer=offer).exists():
        deal = Deal.objects.get(offer=offer)

        if deal.status == "cancelled":
            deal.status, deal.closedAt, deal.price = "open", None, int(offer.amount)
            deal.save(update_fields=["status", "closedAt", "price"])

            deal.steps.update(doneAt=None, doneBy="", note="", reminderSentOn=None, dueOn=None)

            plan_from(deal, 0, timezone.localdate())

        return deal

    deal = Deal.objects.create(
        offer=offer,
        listing=offer.listing,
        buyer_id=offer.buyer_id,
        price=int(offer.amount),
    )

    DealStep.objects.bulk_create(
        [DealStep(deal=deal, key=step["key"], position=index) for index, step in enumerate(STEPS)]
    )

    plan_from(deal, 0, timezone.localdate())

    return deal


def cancelDeal(offer):
    Deal.objects.filter(offer=offer, status="open").update(
        status="cancelled", closedAt=timezone.now()
    )


def plan_from(deal, position, start):
    step = deal.steps.filter(position=position).first()

    if step:
        DealStep.objects.filter(pk=step.pk).update(
            dueOn=start + timedelta(days=BY_KEY[step.key]["days"])
        )


def role_in(deal, user):
    if user.is_authenticated and deal.buyer_id == user.pk:
        return "buyer"

    if user.is_authenticated and deal.listing.seller_id == user.pk:
        return "seller"

    if canOpen(user, "admin_orders"):
        return "admin"

    return None


def stepRows(deal, role=None, today=None):
    today = today or timezone.localdate()
    rows = []
    current = None

    for step in deal.steps.all():
        info = BY_KEY[step.key]
        done = step.doneAt is not None

        if not done and current is None and deal.status == "open":
            current = step

        rows.append(
            {
                "step": step,
                "title": info["title"],
                "about": info["about"],
                "who": WHO[info["who"]],
                "done": done,
                "current": step is current,
                "late": step is current and step.dueOn and step.dueOn < today,
                "canMark": step is current
                and role in ("buyer", "seller")
                and info["who"] in (role, "both"),
            }
        )

    return rows


def progress(deal):
    steps = list(deal.steps.all())
    done = sum(step.doneAt is not None for step in steps)
    nextStep = next((step for step in steps if step.doneAt is None), None)

    return {
        "deal": deal,
        "done": done,
        "total": len(steps),
        "next": (BY_KEY[nextStep.key]["title"] if nextStep and deal.status == "open" else ""),
        "nextWho": (WHO[BY_KEY[nextStep.key]["who"]] if nextStep and deal.status == "open" else ""),
        "dueOn": nextStep.dueOn if nextStep and deal.status == "open" else None,
    }


def stuckDeals(today=None):
    today = today or timezone.localdate()

    late = DealStep.objects.filter(
        deal__status="open",
        doneAt__isnull=True,
        dueOn__lt=today - timedelta(days=STUCK_DAYS),
    )

    return Deal.objects.filter(pk__in=late.values("deal_id"))


def finishSale(deal):
    flat = Listing.objects.select_for_update().get(pk=deal.listing_id)

    if flat.status in ("live", "review", "paused"):
        flat.statusBeforeSold = flat.status
        flat.status = "sold"
        flat.save(update_fields=["status", "statusBeforeSold"])

    booked = list(
        ViewingBooking.objects.select_for_update()
        .filter(
            slot__listing=flat,
            slot__startsAt__gt=timezone.now(),
            status__in=("requested", "confirmed"),
        )
        .select_related("slot__listing")
    )

    ViewingBooking.objects.filter(pk__in=[booking.pk for booking in booked]).update(
        status="cancelled"
    )

    for booking in booked:
        visit_note(booking.slot, "viewing_cancelled", [booking.buyer_id], "seller")

    Deal.objects.filter(pk=deal.pk).update(status="complete", closedAt=timezone.now())


def dealFor(request, pk):
    deal = get_object_or_404(
        Deal.objects.select_related("listing__seller", "buyer", "offer"), pk=pk
    )

    role = role_in(deal, request.user)

    if role is None:
        raise Http404

    return deal, role


@login_required
def deal_page(request, pk):
    deal, role = dealFor(request, pk)

    context = {
        "deal": deal,
        "flat": deal.listing,
        "role": role,
        "rows": stepRows(deal, role),
        "progress": progress(deal),
        "back": (
            reverse("admin_deals")
            if role == "admin"
            else (
                reverse("seller_viewings") + "#offers-tab"
                if role == "seller"
                else reverse("my_offers")
            )
        ),
    }

    return render(request, "storefront/deal.html", context)


@login_required
@require_POST
@needEmail
def deal_step(request, pk):
    with transaction.atomic():
        deal = get_object_or_404(Deal.objects.select_for_update().select_related("listing"), pk=pk)

        role = role_in(deal, request.user)

        if role is None:
            raise Http404

        rows = stepRows(deal, role)
        row = next((row for row in rows if row["current"]), None)

        if deal.status != "open" or row is None or row["step"].key != request.POST.get("step"):
            messages.error(request, "This step has changed. Please check the deal again.")

            return redirect("deal_page", pk=deal.pk)

        if not row["canMark"]:
            messages.error(request, "Only the " + row["who"].lower() + " can tick this step.")

            return redirect("deal_page", pk=deal.pk)

        step = row["step"]
        note = " ".join(request.POST.get("note", "").split())[:200]
        now = timezone.now()
        DealStep.objects.filter(pk=step.pk).update(doneAt=now, doneBy=role, note=note)
        plan_from(deal, step.position + 1, timezone.localdate())

        if role == "buyer":
            other = deal.listing.seller_id
        else:
            other = deal.buyer_id

        if role == "buyer":
            who = "The buyer"
        else:
            who = "The seller"

        notify(
            other,
            "deal_step",
            f"{who} ticked “{row['title']}” for {_place(deal.listing)}",
            reverse("deal_page", args=[deal.pk]),
        )

        if step.key == "completion":
            finishSale(deal)

            messages.success(
                request,
                "Congratulations! The sale is complete and the flat is marked as sold.",
            )
        else:
            messages.success(
                request,
                f"“{row['title']}” is ticked. We’ve told the {'seller' if role == 'buyer' else 'buyer'}.",
            )

    return redirect("deal_page", pk=deal.pk)
