from .models import AuditLog
from django.http import HttpResponse
from storefront.models import Listing, object_id
from django.core.paginator import Paginator
from django.contrib.auth.models import User
import csv
from django.contrib import messages
import re
from django.urls import reverse
from .access import staffPage
from django.utils import timezone
from adminpanel.views import audit, customerRoles, dateRange, findUsers, page, plural


@staffPage("admin_audit")
def audit_log(request):
    entries = AuditLog.objects.select_related("actor").order_by("-createdAt")
    action = request.GET.get("action", "")

    if action:
        entries = entries.filter(action=action)

    actor = request.GET.get("actor", "")

    if actor == "system":
        entries = entries.filter(actor=None)
    elif actor:
        try:
            entries = entries.filter(actor_id=object_id(actor))
        except Exception as error:
            messages.error(request, "Choose a valid staff member.")

    start, end, problem = dateRange(request)

    if start:
        entries = entries.filter(createdAt__date__gte=start)

    if end:
        entries = entries.filter(createdAt__date__lte=end)

    shown = Paginator(entries, 25).get_page(request.GET.get("page"))
    auditRows(shown)

    actions = AuditLog.objects.values_list("action", flat=True).distinct().order_by("action")

    context = {
        "entries": shown,
        "actions": [(value, labelFor(value)) for value in actions],
        "actors": User.objects.filter(auditEntries__isnull=False).distinct().order_by("first_name"),
        "action": action,
        "actor": actor,
        "start": request.GET.get("from", ""),
        "end": request.GET.get("to", ""),
        "dateProblem": problem,
        "filtered": bool(action or actor or request.GET.get("from") or request.GET.get("to")),
    }

    return page(request, "adminpanel/audit.html", context)


def csvFile(name, header, rows):
    reply = HttpResponse(content_type="text/csv")

    reply["Content-Disposition"] = (
        f'attachment; filename="flatfair-{name}-{timezone.localdate()}.csv"'
    )

    writer = csv.writer(reply)
    writer.writerow(header)

    for row in rows:
        writer.writerow([safeCell(value) for value in row])

    return reply


@staffPage("admin_audit")
def audit_csv(request):
    entries = AuditLog.objects.select_related("actor").order_by("-createdAt")
    parts = []
    action = request.GET.get("action", "")

    if action:
        entries = entries.filter(action=action)
        parts.append(f"action {labelFor(action)}")

    actor = request.GET.get("actor", "")

    if actor == "system":
        entries = entries.filter(actor=None)
        parts.append("staff System")
    elif actor:
        try:
            entries = entries.filter(actor_id=object_id(actor))
            parts.append(f"staff #{actor}")
        except Exception as error:
            entries = entries.none()

    start, end, problem = dateRange(request)

    if start:
        entries = entries.filter(createdAt__date__gte=start)
        parts.append(f"from {start}")

    if end:
        entries = entries.filter(createdAt__date__lte=end)
        parts.append(f"to {end}")

    rows = []

    for entry in entries.iterator(chunk_size=500):
        person = entry.actor

        rows.append(
            [
                timezone.localtime(entry.createdAt).strftime("%Y-%m-%d %H:%M:%S"),
                (person.get_full_name() or person.email) if person else "System",
                person.email if person else "",
                labelFor(entry.action),
                entry.target,
                entry.detail,
            ]
        )

    detail = f"{len(rows)} row{'' if len(rows) == 1 else 's'}, " + (
        ("filtered by " + ", ".join(parts)) if parts else "no filters"
    )

    audit(request, "export", "Audit log", detail[:200])

    return csvFile(
        "audit-log",
        ["When", "Staff", "Staff email", "Action", "Record", "Details"],
        rows,
    )


@staffPage("admin_orders")
def orders_csv(request):
    from adminpanel.orders import findOrders

    rows = []
    found, problem = findOrders(request)

    for order in found:
        names = []

        for item in order.items.all():
            names.append(item.name)

        rows.append(
            [
                order.pk,
                timezone.localtime(order.createdAt).strftime("%Y-%m-%d %H:%M"),
                order.seller.first_name,
                order.seller.email,
                "; ".join(names),
                order.subtotal,
                order.gst,
                order.total,
                order.get_status_display(),
            ]
        )

    logExport(request, "orders", len(rows))

    return csvFile(
        "orders",
        [
            "Order",
            "Date",
            "Seller",
            "Email",
            "Items",
            "Subtotal",
            "GST",
            "Total",
            "Status",
        ],
        rows,
    )


@staffPage("admin_customers")
def users_csv(request):
    rows = [
        [
            customer.pk,
            customer.first_name,
            customer.email,
            customerRoles(customer),
            timezone.localtime(customer.date_joined).strftime("%Y-%m-%d"),
            "Active" if customer.is_active else "Suspended",
        ]
        for customer in findUsers(request)
    ]

    logExport(request, "customers", len(rows))

    return csvFile("customers", ["ID", "Name", "Email", "Role", "Joined", "Status"], rows)


@staffPage("admin_queue")
def flats_csv(request):
    from adminpanel.moderation import findFlats

    homes = findFlats(request)

    rows = [
        [
            flat.pk,
            timezone.localtime(flat.createdAt).strftime("%Y-%m-%d"),
            flat.town,
            flat.block,
            flat.flatType,
            flat.flatModel,
            flat.floorAreaSqm,
            flat.askingPrice,
            flat.bandLow,
            flat.estimatedPrice,
            flat.bandHigh,
            flat.modelVersion,
            flat.get_status_display(),
            flat.seller.email if flat.seller else "",
        ]
        for flat in homes
    ]

    logExport(request, "listings", len(rows))

    header = [
        "ID",
        "Listed",
        "Town",
        "Block",
        "Flat type",
        "Flat model",
        "Area (sqm)",
        "Asking",
        "Band low",
        "Estimate",
    ]

    header += ["Band high", "Model", "Status", "Seller"]

    return csvFile("listings", header, rows)
