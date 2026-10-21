from functools import wraps
from django.contrib.auth.decorators import login_required
from django.db.models import (
    Case, Count, F, FloatField,
    IntegerField, OuterRef, Subquery, Value,
    When,
)
from django.db.models.functions import Cast, Coalesce, Greatest, Least
from django.shortcuts import render

PAGES = {
    "admin_overview": ("Overview", "dashboard"),
    "admin_queue": ("Review queue", "check"),
    "admin_orders": ("Orders", "receipt"),
    "admin_tickets": ("Support tickets", "support"),
    "admin_customers": ("Customers", "people"),
    "admin_deals": ("Deals", "offer"),
    "admin_reviews": ("Reviews", "star"),
    "admin_community": ("Community", "chat"),
    "admin_listings": ("Listings", "home"),
    "admin_catalogue": ("Catalogue", "offer"),
    "admin_staff": ("Admins", "people"),
    "admin_price_model": ("Price check accuracy", "chart"),
    "admin_audit": ("Audit log", "clock"),
}


def staffRole(user):
    if user.is_authenticated and user.is_active and user.is_staff:
        return "Admin"

    return None


def canOpen(user, page):
    return staffRole(user) == "Admin"


def menu(user):
    if not staffRole(user):
        return []

    return [
        {
            "page": page,
            "label": label,
            "icon": icon,
            "more": page in (("admin_price_model", "admin_audit")),
        }
        for page, (label, icon) in PAGES.items()
    ]


def staffPage(page):
    def wrap(view):
        @login_required
        @wraps(view)
        def inner(request, *args, **kwargs):
            if not canOpen(request.user, page):
                return render(request, "storefront/forbidden.html", {"role": "staff"}, status=403)

            from storefront.listing_rules import autoClose

            autoClose()

            return view(request, *args, **kwargs)

        return inner

    return wrap


def price_risk():
    gap = (Cast(F("askingPrice") - F("bandHigh"), FloatField()) * 100.0) / Cast(
        F("bandHigh"), FloatField()
    )

    return Case(
        When(estimatedPrice__isnull=True, then=Value(25.0)),
        When(bandLow__isnull=True, then=Value(25.0)),
        When(bandHigh__isnull=True, then=Value(25.0)),
        When(askingPrice__gt=F("bandHigh"), then=Least(Value(40.0), gap * 40.0 / 15.0)),
        When(askingPrice__lt=F("bandLow"), then=Value(30.0)),
        default=Value(0.0),
        output_field=FloatField(),
    )


def withRisk(flats):
    from adminpanel.models import Decision

    denied = (
        Decision.objects.filter(decision="rejected", listing__seller=OuterRef("seller"))
        .exclude(listing=OuterRef("pk"))
        .values("listing__seller")
        .annotate(total=Count("pk"))
        .values("total")[:1]
    )

    flats = flats.annotate(
        pastRejections=Coalesce(Subquery(denied, output_field=IntegerField()), Value(0))
    )

    score = (
        price_risk()
        + Least(Cast(F("aiPhotos"), FloatField()) * 20.0, Value(40.0))
        + Least(Cast(F("unsurePhotos"), FloatField()) * 15.0, Value(30.0))
        + Least(
            Cast(F("uncheckedPhotos") + F("stalePhotos"), FloatField()) * 5.0,
            Value(15.0),
        )
        + Case(
            When(sellerReactivated=True, then=Value(20.0)),
            default=Value(0.0),
            output_field=FloatField(),
        )
        + Least(Cast(F("pastRejections"), FloatField()) * 10.0, Value(20.0))
    )

    return flats.annotate(
        risk=Cast(Least(Greatest(score, Value(0.0)), Value(100.0)), IntegerField())
    )


def riskLevel(score):
    if score is None:
        return ""

    if score >= (50):
        return "high"

    if score >= (20):
        return "medium"

    return "low"


def riskParts(flat):
    parts = []

    if not flat.estimatedPrice or not flat.bandLow or not flat.bandHigh:
        parts.append(("No price estimate", 25))
    elif flat.askingPrice > flat.bandHigh:
        gap = (flat.askingPrice - flat.bandHigh) * 100 / flat.bandHigh

        parts.append((f"Price {gap:.0f}% above the fair range", round(min(40, gap * 40 / 15))))
    elif flat.askingPrice < flat.bandLow:
        parts.append(("Price below the fair range (possible typo)", 30))

    if getattr(flat, "aiPhotos", 0):
        parts.append(
            (
                f"{flat.aiPhotos} photo{'s' if flat.aiPhotos > 1 else ''} may be computer-made",
                min(40, flat.aiPhotos * 20),
            )
        )

    if getattr(flat, "unsurePhotos", 0):
        parts.append(
            (
                f"{flat.unsurePhotos} photo{'s' if flat.unsurePhotos > 1 else ''} may not show the flat",
                min(30, flat.unsurePhotos * 15),
            )
        )

    unseen = getattr(flat, "uncheckedPhotos", 0) + getattr(flat, "stalePhotos", 0)

    if unseen:
        parts.append(
            (
                f"{unseen} photo{'s' if unseen > 1 else ''} not checked yet",
                min(15, unseen * 5),
            )
        )

    if getattr(flat, "sellerReactivated", False):
        parts.append(("Seller was suspended before", 20))

    if getattr(flat, "pastRejections", 0):
        parts.append(
            (
                f"{flat.pastRejections} earlier listing{'s' if flat.pastRejections > 1 else ''} rejected",
                min(20, flat.pastRejections * 10),
            )
        )

    return parts


def passSafe(flats):
    from adminpanel.models import AuditLog, Decision
    from storefront.listing_rules import heldByCost, liveIssue

    passed = 0

    for flat in withRisk(flats).filter(risk__lt=(10)).order_by("reviewSince", "pk")[:(200)]:
        if not heldByCost(flat) or liveIssue(flat, flat.revision, "review"):
            continue

        flat.status = "live"
        flat.save(update_fields=["status"])
        reason = f"Risk score {flat.risk} of 100 (low): price close to the fair range, photo and seller checks clear."

        Decision.objects.create(
            listing=flat,
            staff=None,
            decision="approved",
            reason=reason,
            askingPrice=flat.askingPrice,
            estimatedPrice=flat.estimatedPrice,
            bandLow=flat.bandLow,
            bandHigh=flat.bandHigh,
            modelVersion=flat.modelVersion,
        )

        AuditLog.objects.create(
            actor=None,
            action="listing auto-approved",
            target=f"Listing #{flat.pk} ({flat.town.title()})",
            detail=reason,
        )

        from storefront.notices import choice_note

        choice_note(flat, "approved")
        passed += 1

    return passed
