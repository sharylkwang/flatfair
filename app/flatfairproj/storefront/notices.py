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
