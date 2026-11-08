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


def labelFor(action):
    if action == "export":
        return "Downloaded a list"

    return action.capitalize()


def auditRows(entries):
    export_names = {
        "orders": "Order list (CSV)",
        "customers": "Customer list (CSV)",
        "listings": "Listing list (CSV)",
    }

    teamMail = set(User.objects.filter(is_staff=True).values_list("email", flat=True))

    userIds = []
    flatIds = []

    for entry in entries:
        match = re.match(r"(Order|Customer|Listing|Ticket) #(\d+)", entry.target)
        entry.recordType = match.group(1) if match else ""
        entry.recordId = int(match.group(2)) if match else None

        if entry.recordType == "Customer":
            userIds.append(entry.recordId)
        elif entry.recordType == "Listing":
            flatIds.append(entry.recordId)

    names = dict(User.objects.filter(pk__in=userIds).values_list("pk", "first_name"))

    linkIds = dict(Listing.objects.filter(pk__in=flatIds).values_list("pk", "publicId"))

    for entry in entries:
        entry.actionLabel = labelFor(entry.action)
        entry.recordKind = ""
        entry.recordLabel = entry.target
        entry.recordUrl = ""
        kind = entry.recordType
        row_id = entry.recordId
        named = entry.target.split("(", 1)[-1].rstrip(")")

        if kind == "Order":
            entry.recordKind = "Order"
            entry.recordLabel = f"#{row_id}"
            entry.recordUrl = reverse("admin_order", args=[row_id])
        elif kind == "Customer":
            entry.recordKind = "Customer"
            entry.recordLabel = names.get(row_id) or named
            entry.recordUrl = reverse("admin_customer", args=[row_id])
        elif kind == "Listing":
            entry.recordKind = "Listing"
            entry.recordLabel = named

            if row_id in linkIds:
                entry.recordUrl = reverse("listing_detail", args=[linkIds[row_id]])
        elif kind == "Ticket":
            entry.recordKind = "Support message"
            entry.recordLabel = f"#{row_id}"
            entry.recordUrl = reverse("admin_ticket", args=[row_id])
        elif entry.action == "export":
            entry.recordLabel = export_names.get(entry.target, entry.target)
        elif entry.action.startswith("product"):
            entry.recordKind = "Product"
            entry.recordUrl = reverse("admin_catalogue")
        elif (
            entry.target in teamMail
            or entry.action.startswith("staff")
            or entry.action == "password changed"
        ):
            entry.recordKind = "Staff"
            entry.recordUrl = reverse("admin_staff")

    return entries


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


def safeCell(value):
    if not isinstance(value, str):
        return value

    start = 0

    while start < len(value) and (value[start].isspace() or ord(value[start]) < 32):
        start += 1

    if (
        value[start : start + 1] in ("=", "+", "-", "@")
        or "\t" in value[:start]
        or "\r" in value[:start]
    ):
        return "'" + value

    return value


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


def logExport(request, exportType, rowCount):
    parts = []

    for key in (
        "status",
        "role",
        "state",
        "type",
        "from",
        "to",
        "town",
        "above",
        "seller",
    ):
        value = request.GET.get(key, "")

        if value:
            parts.append(f"{key} {value[:20]}")

    if request.GET.get("q"):
        parts.append("a search")

    detail = plural(rowCount, "row") + ", no filters"

    if parts:
        detail = plural(rowCount, "row") + ", filtered by " + ", ".join(parts)

    audit(request, "export", exportType, detail)


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
