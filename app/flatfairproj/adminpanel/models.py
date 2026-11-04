from pathlib import Path
from uuid import uuid4
from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible
from django.db import models
from django.db.models import F, Q
from django.contrib.auth.models import User
from django.utils import timezone
from storefront.models import Listing, Order


class Decision(models.Model):
    CHOICES = [
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("documents", "Documents requested"),
        ("takendown", "Taken down"),
    ]

    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="decisions")
    staff = models.ForeignKey(
        User, on_delete=models.RESTRICT, related_name="decisions", null=True, blank=True
    )
    decision = models.CharField(max_length=10, choices=CHOICES)
    reason = models.CharField(max_length=200, blank=True)
    askingPrice = models.IntegerField()
    estimatedPrice = models.IntegerField(null=True)
    bandLow = models.IntegerField(null=True)
    bandHigh = models.IntegerField(null=True)
    modelVersion = models.CharField(max_length=10, blank=True)
    createdAt = models.DateTimeField(default=timezone.now)


class AuditLog(models.Model):
    actor = models.ForeignKey(
        User,
        on_delete=models.RESTRICT,
        related_name="auditEntries",
        null=True,
        blank=True,
    )
    action = models.CharField(max_length=30)
    target = models.CharField(max_length=80)
    detail = models.CharField(max_length=200, blank=True)
    createdAt = models.DateTimeField(default=timezone.now)


class Ticket(models.Model):
    TOPICS = [
        ("listing", "My listing"),
        ("orders", "Payments and orders"),
        ("refund", "Refund request"),
        ("viewings", "Offers and viewings"),
        ("account", "My account"),
        ("other", "Something else"),
    ]
    STATUSES = [("open", "Open"), ("resolved", "Resolved")]
    TONES = {"open": "st-pending", "resolved": "st-resolved"}

    customer = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )
    email = models.EmailField()
    topic = models.CharField(max_length=10, choices=TOPICS)
    message = models.TextField(max_length=2000)
    status = models.CharField(max_length=10, choices=STATUSES, default="open")
    assignee = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assignedTickets",
    )
    createdAt = models.DateTimeField(default=timezone.now)
    resolvedAt = models.DateTimeField(null=True, blank=True)
    order = models.ForeignKey(
        Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )
    customerSeenAt = models.DateTimeField(null=True, blank=True)

    def tone(self):
        return self.TONES[self.status]


class TicketMessageQuerySet(models.QuerySet):
    def unseen(self):
        return self.filter(fromStaff=True).filter(
            Q(ticket__customerSeenAt=None) | Q(createdAt__gt=F("ticket__customerSeenAt"))
        )


class TicketMessage(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="replies")
    author = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ticketMessages",
    )
    body = models.TextField(max_length=2000)
    createdAt = models.DateTimeField(default=timezone.now)
    fromStaff = models.BooleanField(default=False)
    objects = TicketMessageQuerySet.as_manager()
