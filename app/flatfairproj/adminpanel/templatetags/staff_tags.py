from django import template
from django.utils import timezone
from sellerpanel.plans import packState
from adminpanel.access import MENU_GROUPS, staffRole, canOpen, canSeeFlat, menu

register = template.Library()

STATE_LOOKS = {
    "listing": {
        "live": ("good", "check"),
        "review": ("warn", "clock"),
        "sold": ("final", "flag"),
        "rejected": ("closed", "cross"),
        "hidden": ("closed", "hidden"),
        "paused": ("info", "clock"),
        "withdrawn": ("closed", "undo"),
    },
    "order": {
        "paid": ("good", "receipt"),
        "processing": ("info", "clock"),
        "completed": ("good", "check"),
        "refunded": ("closed", "undo"),
    },
    "payment": {
        "pending": ("warn", "clock"),
        "paid": ("good", "receipt"),
        "failed": ("bad", "cross"),
        "refunded": ("closed", "undo"),
        "partly_refunded": ("closed", "undo"),
    },
    "refund": {
        "pending": ("bad", "clock"),
        "succeeded": ("good", "check"),
        "failed": ("bad", "cross"),
    },



    "ticket": {"open": ("warn", "mail"), "resolved": ("good", "check")},
    "documents": {
        "requested": ("warn", "clock"),
        "submitted": ("info", "mail"),
        "accepted": ("good", "check"),
        "rejected": ("closed", "cross"),
    },
    "decision": {
        "approved": ("good", "check"),
        "rejected": ("bad", "cross"),
        "documents": ("warn", "mail"),
        "takendown": ("closed", "hidden"),
    },
    "offer": {
        "pending": ("warn", "clock"),
        "countered": ("warn", "clock"),
        "accepted": ("good", "check"),
        "declined": ("closed", "cross"),
        "withdrawn": ("closed", "undo"),
    },
    "viewing": {
        "requested": ("warn", "clock"),
        "confirmed": ("good", "check"),
        "cancelled": ("closed", "cross"),
        "done": ("closed", "check"),
    },
    "hold": {
        "price": ("price", "offer", "Price hold"),
        "photos": ("photo", "camera", "Photo hold"),
        "other": ("closed", "clock", "Other hold"),
    },
    "account": {
        True: ("good", "check", "Active"),
        False: ("closed", "hidden", "Suspended"),
    },
    "access": {
        True: ("good", "check", "Active"),
        False: ("closed", "hidden", "Disabled"),
    },
    "post": {
        "reported": ("bad", "mail", "Reported"),
        "hidden": ("closed", "hidden", "Hidden"),
    },
    "shop": {True: ("good", "check", "Shown"), False: ("closed", "hidden", "Hidden")},
}


@register.filter
def staff_role(user):
    if not getattr(user, "is_staff", False):
        return ""

    return staffRole(user) or "Staff"


@register.filter
def can_open(user, page):
    return bool(getattr(user, "is_staff", False)) and canOpen(user, page)


@register.filter
def can_view_listing(user, flat):
    return canSeeFlat(user, flat)


@register.filter
def staff_menu(user):
    if not getattr(user, "is_staff", False):
        return []

    return menu(user)


@register.filter
def staff_menu_groups(user):
    items = {item["page"]: item for item in menu(user)}

    return [
        {"label": label, "items": [items[name] for name in names]}
        for label, names in MENU_GROUPS
        if all(name in items for name in names)
    ]


@register.inclusion_tag("adminpanel/status.html")
def staff_state(kind, value, label=""):
    look = STATE_LOOKS[kind].get(value, ("closed", "clock"))

    words = label or (look[2] if len(look) > 2 else str(value).replace("_", " ").capitalize())

    return {"tone": look[0], "icon": look[1], "label": words}


@register.filter
def package_status(item):
    return packState(item, timezone.now())


@register.filter
def order_label(order):
    return order.get_status_display()


@register.filter
def order_tone(order):
    status_tones = {
        "paid": "st-pending",
        "processing": "st-processing",
        "completed": "st-completed",
        "refunded": "st-refunded",
    }

    return status_tones.get(order.status, "st-hidden")


@register.filter
def todo_urgency(action):
    todo_urgency = {
        "Refunds waiting": "urgent",
        "Payments to check": "urgent",
        "Stuck deals": "urgent",
        "Reported posts": "urgent",
        "Open tickets": "soon",
        "Orders to act on": "soon",
        "Review queue": "soon",
    }

    return todo_urgency.get(action.get("title"), "info")


@register.filter
def menu_tone(item):
    menu_tones = {
        "admin_orders": "urgent",
        "admin_deals": "urgent",
        "admin_community": "urgent",
        "admin_tickets": "soon",
        "admin_queue": "soon",
    }

    if not item.get("count"):
        return "none"

    return menu_tones.get(item.get("page"), "info")


@register.filter
def risk_level(score):
    from adminpanel.access import riskLevel

    return riskLevel(score)


@register.filter
def all_zero(points, key):
    return not any(point[key] for point in points)
