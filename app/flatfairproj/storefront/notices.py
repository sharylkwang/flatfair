import logging
import threading
from hashlib import sha256
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, F, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.template.loader import render_to_string
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from .models import ActivityNotice, Payment, hasRole, notAllowed
from adminpanel.models import Ticket, TicketMessage

LEADS = {
    "both": "Buying and selling: offers, viewings, listings, orders, support and community replies.",
    "seller": "Offers, viewings, listings, orders, support and community replies.",
    "buyer": "News on your offers, viewings, support messages and community replies.",
    "staff": "Staff tasks are listed on the staff overview, not here.",
}


def role_of(user):
    if user.is_staff:
        return "staff"

    if hasRole(user, "buyer") and hasRole(user, "seller"):
        return "both"

    if hasRole(user, "seller"):
        return "seller"

    return "buyer"


def count_new(user):
    return ActivityNotice.objects.filter(user=user, read=False).count()


def new_notes(user, count=3):
    return ActivityNotice.objects.filter(user=user, read=False).order_by("-createdAt", "-pk")[
        :count
    ]


def notify(user, kind, text, url):
    from .notices import mailNotes

    notice = ActivityNotice.objects.create(
        user_id=getattr(user, "pk", user), kind=kind, text=text[:200], url=url
    )

    mailNotes([notice])

    return notice


def searchNote(savedSearch, flat, kind):
    if kind == "new_match":
        text = f'New flat matches "{savedSearch.name}"'
    else:
        text = "Price dropped on a flat in your saved search"

    url = reverse("listing_detail", args=[flat.publicId])

    notice = notify(
        savedSearch.buyer,
        "search_match" if kind == "new_match" else "search_price_drop",
        text,
        url,
    )

    from .notices import buildEmail, site_Url
    from .notices import real_mail

    email = savedSearch.buyer.email

    if real_mail(email):
        if kind == "new_match":
            subject = "FlatFair: new flat matches your search"
        else:
            subject = "FlatFair: price dropped on a flat"

        message = buildEmail(
            subject,
            email,
            text,
            name=savedSearch.buyer.first_name,
            rows=[
                ("Flat", _place(flat), False),
                ("Price", _dollars(flat.askingPrice), True),
            ],
            button={"label": "View the flat", "url": site_Url(url)},
            notes=["You can change or pause your saved searches on the Saved page."],
            reason="You’re getting this email because you saved a search on FlatFair.",
        )

        transaction.on_commit(lambda: message.send(fail_silently=True))

    return notice


def _dollars(amount):
    return f"${amount or 0:,.0f}"


def _place(flat):
    if flat.block:
        return f"Blk {flat.block} {flat.town.title()}"

    return flat.town.title()


def _time(moment):
    local = timezone.localtime(moment)
    text = f"{local:%a} {local.day} {local:%b}, {local.hour % 12 or 12}:{local:%M}{local:%p}"

    return text.replace("AM", "am").replace("PM", "pm")


def _offer_note(offer, kind, to):
    offer_text = {
        ("offer_new", "seller"): "New offer of {amount} for {place}",
        ("offer_countered", "buyer"): "The seller is asking {counter} for {place}",
        (
            "offer_accepted",
            "buyer",
        ): "The seller accepted your offer of {amount} for {place}",
        (
            "offer_accepted",
            "seller",
        ): "The buyer agreed to your price of {counter} for {place}",
        (
            "offer_declined",
            "buyer",
        ): "The seller declined your offer of {amount} for {place}",
        (
            "offer_declined",
            "seller",
        ): "The buyer declined your price of {counter} for {place}",
        (
            "offer_declined_auto",
            "buyer",
        ): "Your offer of {amount} for {place} was below the seller’s minimum, so it was declined",
        (
            "offer_declined_auto",
            "seller",
        ): "An offer of {amount} for {place} was below your minimum, so we declined it for you",
        (
            "offer_withdrawn",
            "seller",
        ): "The buyer withdrew their offer of {amount} for {place}",
        ("offer_reply", "buyer"): "The seller replied to your offer for {place}",
        (
            "offer_buyer_reply",
            "seller",
        ): "The buyer replied about their offer of {amount} for {place}",
        (
            "offer_reopened",
            "buyer",
        ): "The seller undid their acceptance of your offer of {amount} for {place}. Your offer is open again",
        (
            "offer_suggested",
            "seller",
        ): "The buyer suggested {amount} for {place}. Please accept, decline or counter",
    }

    flat = offer.listing

    if to == "seller":
        user_id = flat.seller_id
        url = reverse("seller_viewings") + f"#offer-{offer.pk}"
    else:
        user_id = offer.buyer_id
        url = reverse("my_offers") + f"#offer-{offer.pk}"

    words = offer_text[kind, to].format(
        amount=_dollars(offer.amount),
        counter=_dollars(offer.counterAmount),
        place=_place(flat),
    )

    return ActivityNotice(user_id=user_id, kind=kind, text=words[:200], url=url)


def offer_note(offer, kind, to):
    from .notices import mailNotes

    made = ActivityNotice.objects.bulk_create([_offer_note(offer, kind, to)])
    mailNotes(made)

    return made


def offer_notes(offers, kind, to):
    from .notices import mailNotes

    notices = []

    for offer in offers:
        notices.append(_offer_note(offer, kind, to))

    made = ActivityNotice.objects.bulk_create(notices)
    mailNotes(made)

    return made


def visit_note(slot, kind, users, by, reason=""):
    when = _time(slot.startsAt)
    place = _place(slot.listing)

    if by == "buyer":
        url = reverse("seller_viewings") + f"#slot-{slot.pk}"

        words = {
            "viewing_booked": f"A buyer asked to view {place} on {when}",
            "viewing_cancelled": f"A buyer cancelled their viewing of {place} on {when}",
        }[kind]
    else:
        url = reverse("my_offers") + f"#slot-{slot.pk}"

        words = {
            "viewing_confirmed": f"The seller confirmed your viewing of {place} on {when}",
            "viewing_cancelled": f"The seller cancelled your viewing of {place} on {when}",
        }[kind]

    if reason:
        words += ". Reason: " + reason

    from .notices import mailNotes

    notices = []

    for user in users:
        notices.append(
            ActivityNotice(user_id=getattr(user, "pk", user), kind=kind, text=words[:200], url=url)
        )

    made = ActivityNotice.objects.bulk_create(notices)
    mailNotes(made)

    return made


def choice_note(flat, choice, reason=""):
    text = {
        "approved": "{place} is live. Add some viewing times so buyers can visit.",
        "rejected": "Our team couldn’t approve {place}: {reason}",
        "documents": "Our team needs more from you for {place}: {reason}",
    }

    words = text[choice].format(place=_place(flat), reason=reason)

    if choice == "approved":
        url = reverse("seller_viewings") + "?first=1#slot-form"
    else:
        url = reverse("my_listings") + f"#listing-{flat.publicId}"

    return notify(flat.seller_id, f"listing_{choice}", words, url)


def page_items(user, community=False):
    from .reminders import autoNudge

    autoNudge()
    notices = ActivityNotice.objects.filter(user=user)

    if community:
        notices = notices.filter(kind__startswith="community_")

    items = []

    for notice in notices.order_by("-createdAt", "-pk")[:(30)]:
        items.append(
            {
                "title": notice.get_kind_display(),
                "text": notice.text,
                "url": notice.url,
                "when": notice.createdAt,
                "new": not notice.read,
            }
        )

    return items


log = logging.getLogger("storefront.mailer")


def real_mail(email):
    if not email or "@" not in email:
        return False

    domain = email.rsplit("@", 1)[1].lower()

    return not any(
        domain == skip or domain.endswith("." + skip) for skip in settings.NOTICE_MAIL_SKIP
    )


def site_Url(path):
    return (settings.PUBLIC_URL or "http://127.0.0.1:8765").rstrip("/") + path


def buildEmail(
    subject,
    to,
    heading,
    lines=(),
    name="",
    code="",
    rows=(),
    button=None,
    notes=(),
    preview="",
    reason=("You’re getting this email because of activity on your FlatFair account."),
):
    context = {
        "subject": subject,
        "heading": heading,
        "lines": list(lines),
        "name": name,
        "code": code,
        "rows": list(rows),
        "button": button,
        "notes": list(notes),
        "preview": preview or heading,
        "reason": reason,
        "supportUrl": site_Url(reverse("support")),
    }

    message = EmailMultiAlternatives(
        subject, render_to_string("emails/message.txt", context), settings.DEFAULT_FROM_EMAIL, [to]
    )
    message.attach_alternative(render_to_string("emails/message.html", context), "text/html")

    return message


def buttonText(kind):
    if kind.startswith("offer_"):
        return "View the offer"

    if kind.startswith("viewing_"):
        return "View the viewing"

    if kind.startswith("deal_"):
        return "Open the deal"

    if kind.startswith(("saved_", "flat_")):
        return "View the flat"

    return "Open my listing"


def deliver(message):
    try:
        message.send(fail_silently=False)
    except Exception:
        log.warning("Could not send a FlatFair email")


def sendLater(message):
    def start():
        if settings.NOTICE_MAIL_ASYNC:
            threading.Thread(target=deliver, args=(message,), daemon=True).start()
        else:
            deliver(message)

    transaction.on_commit(start)


def mailNotes(notices):
    subjects = {
        "offer_new": "New offer on your flat",
        "offer_reply": "The seller replied to your offer",
        "offer_buyer_reply": "The buyer replied about their offer",
        "offer_countered": "The seller sent a new price",
        "offer_accepted": "Your price is agreed",
        "offer_declined_auto": "Your offer was below the seller’s minimum",
        "offer_reopened": "The seller undid their acceptance",
        "viewing_booked": "A buyer asked to view your flat",
        "viewing_confirmed": "Your viewing is confirmed",
        "viewing_cancelled": "A viewing was cancelled",
        "viewing_times_added": "New viewing times for a flat you asked about",
        "deal_step": "Your deal moved forward",
        "deal_due": "A step in your deal is due",
        "price_coach": "A price suggestion for your flat",
        "photo_refused": "Please replace one photo",
        "listing_approved": "Your flat is live",
        "listing_rejected": "Your listing needs changes",
        "saved_price_drop": "A flat you saved is now cheaper",
        "saved_under_offer": "A flat you saved is under offer",
        "saved_sold": "A flat you saved has been sold",
        "offer_suggested": "The buyer suggested another price",
        "viewing_moved": "A buyer moved their viewing",
        "flat_paused": "A flat you’re following is paused",
        "flat_resumed": "A flat you’re following is back on sale",
        "flat_withdrawn": "A flat you’re following is no longer for sale",
    }

    from django.contrib.auth.models import User

    wanted = [notice for notice in notices if notice.kind in subjects]

    if not wanted:
        return

    from accounts.models import EmailCheck

    ids = {notice.user_id for notice in wanted}
    people = User.objects.in_bulk(ids)
    firm = set(
        EmailCheck.objects.filter(user_id__in=ids, verifiedAt__isnull=False).values_list(
            "user_id", flat=True
        )
    )

    for notice in wanted:
        person = people.get(notice.user_id)

        if (
            person is None
            or not person.is_active
            or person.pk not in firm
            or not real_mail(person.email)
        ):
            continue

        subject = subjects[notice.kind]

        sendLater(
            buildEmail(
                "FlatFair: " + subject,
                person.email,
                subject,
                name=person.first_name,
                lines=[notice.text.rstrip(".") + "."],
                button={"label": buttonText(notice.kind), "url": site_Url(notice.url)},
                reason=(
                    "You’re getting this email because you use FlatFair. You can see every update under Notifications."
                ),
            )
        )


def fanAlerts(listingId, kinds):
    from .models import ActivityNotice, Listing, SavedListing
    from .notices import _dollars, _place

    flat = Listing.objects.filter(pk=listingId).first()

    if flat is None:
        return 0

    people = set(
        SavedListing.objects.filter(listing=flat)
        .exclude(collection__user_id=flat.seller_id)
        .values_list("collection__user_id", flat=True)
    )

    words = {
        "saved_price_drop": f"{_place(flat)}, a flat you saved, is now {_dollars(flat.askingPrice)}",
        "saved_under_offer": f"{_place(flat)}, a flat you saved, is now under offer. You can still make an offer",
        "saved_sold": f"{_place(flat)}, a flat you saved, has been sold",
    }

    url = reverse("listing_detail", args=[flat.publicId])

    notices = ActivityNotice.objects.bulk_create(
        [
            ActivityNotice(user_id=person, kind=kind, text=words[kind][:200], url=url)
            for kind in kinds
            for person in people
        ]
    )

    mailNotes(notices)

    return len(notices)


@receiver(post_save, sender="storefront.Order")
def mailSlip(sender, instance, created, **kwargs):
    if not created or instance.status not in ("paid", "processing"):
        return

    person = instance.seller

    if person is None or not real_mail(person.email):
        return

    def send():
        from .models import Order

        order = Order.objects.filter(pk=instance.pk).prefetch_related("items").first()

        if order is None:
            return

        rows = [(item.name, f"${item.price:,.2f}", False) for item in order.items.all()]
        rows += [
            ("Subtotal", f"${order.subtotal:,.2f}", False),
            ("GST (9%)", f"${order.gst:,.2f}", False),
            ("Total", f"${order.total:,.2f}", True),
        ]

        deliver(
            buildEmail(
                f"FlatFair: Receipt for order #{order.pk}",
                person.email,
                "Thanks for your order",
                name=person.first_name,
                lines=[
                    f"Here’s your receipt for order #{order.pk}. You can follow it under Orders in the Seller Centre."
                ],
                rows=rows,
                button={
                    "label": "View receipt",
                    "url": site_Url(reverse("receipt", args=[order.pk])),
                },
                preview=f"Receipt for order #{order.pk}: ${order.total:,.2f}",
            )
        )

    transaction.on_commit(
        lambda: (
            threading.Thread(target=send, daemon=True).start()
            if settings.NOTICE_MAIL_ASYNC
            else send()
        )
    )


@login_required
def notifications(request):
    social = request.GET.get("show") == "community"
    shownAt = timezone.now()
    items = Paginator(page_items(request.user, social), 10).get_page(request.GET.get("page"))
    unread = request.user.activity_notices.filter(read=False, createdAt__lte=shownAt)

    if social:
        unread = unread.filter(kind__startswith="community_")

    unread.update(read=True)
    context = {"items": items, "lead": LEADS[role_of(request.user)], "community": social}

    return render(request, "storefront/notifications.html", context)


def noteFlood(request):
    key = "support:" + sha256(request.META.get("REMOTE_ADDR", "").encode()).hexdigest()
    sent = cache.get(key, 0) + 1
    cache.set(key, sent, 3600)

    return sent > (5)


def helpStart(request):
    initial = {}

    if request.GET.get("topic") in dict(Ticket.TOPICS):
        initial["topic"] = request.GET["topic"]

    if request.GET.get("order", "").isdigit():
        initial["order"] = request.GET["order"]

    if request.user.is_authenticated and request.GET.get("payment", "").isdigit():
        payment = get_object_or_404(Payment, pk=request.GET["payment"], seller=request.user)
        initial["message"] = f"Please help me with payment {payment.reference}."

    return initial


def support(request):
    form = None

    if not request.user.is_staff:
        form = TicketForm(
            request.POST if request.method == "POST" else None,
            user=request.user,
            initial=helpStart(request),
        )

        if request.method == "POST" and form.is_valid():
            if not request.user.is_authenticated and noteFlood(request):
                messages.error(
                    request, "You’ve sent a few messages already. Please try again in an hour."
                )

                return render(request, "storefront/support.html", {"ticketForm": form})

            ticket = form.save()

            if request.user.is_authenticated:
                messages.success(request, "Message sent. We’ll reply on this page.")

                return redirect("my_ticket", ticket.pk)

            messages.success(request, "Message sent. Our support team will reply by email.")

            return redirect(reverse("support") + "#contact-support")

    return render(request, "storefront/support.html", {"ticketForm": form})


UNSEEN = Q(replies__fromStaff=True) & (
    Q(customerSeenAt=None) | Q(replies__createdAt__gt=F("customerSeenAt"))
)


def userOnly(request, pk=None):
    if request.user.is_staff:
        if pk:
            return redirect("admin_ticket", pk)

        return redirect("admin_tickets")

    if not hasattr(request.user, "profile"):
        return notAllowed(request, "buyer")

    return None


def ticketRows(user):
    latest = TicketMessage.objects.filter(ticket=OuterRef("pk")).order_by("-createdAt", "-pk")

    return (
        Ticket.objects.filter(customer=user)
        .select_related("order")
        .annotate(
            lastBody=Subquery(latest.values("body")[:1]),
            lastFromStaff=Subquery(latest.values("fromStaff")[:1]),
            lastAt=Coalesce(Subquery(latest.values("createdAt")[:1]), "createdAt"),
            unseen=Count("replies", filter=UNSEEN),
        )
        .order_by("-lastAt", "-pk")
    )


@login_required
def my_messages(request):
    blocked = userOnly(request)

    if blocked:
        return blocked

    rows = Paginator(ticketRows(request.user), 20).get_page(request.GET.get("page"))

    return render(request, "storefront/my_messages.html", {"rows": rows})


@transaction.atomic
def userReply(ticket, user, body):
    TicketMessage.objects.create(ticket=ticket, author=user, body=body)

    Ticket.objects.filter(pk=ticket.pk).update(
        status="open", resolvedAt=None, customerSeenAt=timezone.now()
    )


@login_required
def my_ticket(request, pk):
    blocked = userOnly(request, pk)

    if blocked:
        return blocked

    ticket = get_object_or_404(Ticket.objects.select_related("order"), pk=pk, customer=request.user)

    form = ReplyForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        userReply(ticket, request.user, form.cleaned_data["body"])
        messages.success(request, "Reply sent. Our team will answer here.")

        return redirect("my_ticket", ticket.pk)

    thread = list(ticket.replies.select_related("author").order_by("createdAt", "pk"))

    if thread:
        Ticket.objects.filter(pk=ticket.pk).update(customerSeenAt=thread[-1].createdAt)

    context = {
        "ticket": ticket,
        "thread": thread,
        "form": form,
        "lastFromStaff": bool(thread) and thread[-1].fromStaff,
    }

    return render(request, "storefront/my_ticket.html", context)
