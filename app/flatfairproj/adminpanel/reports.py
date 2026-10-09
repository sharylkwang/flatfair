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


