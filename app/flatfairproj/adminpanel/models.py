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


@deconstructible
class DocumentStorage(FileSystemStorage):
    @property
    def base_location(self):
        return str(
            getattr(
                settings,
                "PRIVATE_DOCUMENT_ROOT",
                f"{settings.MEDIA_ROOT}-private-documents",
            )
        )

    @property
    def location(self):
        return str(Path(self.base_location).resolve())

    def url(self, name):
        raise ValueError("Documents are available only through the checked download view.")


def documentPath(document, filename):
    return f"{document.request_id}/{uuid4().hex}{Path(filename).suffix.lower()}"



class DocumentRequest(models.Model):
    STATUSES = [
        ("requested", "Documents requested"),
        ("submitted", "Documents submitted"),
        ("accepted", "Documents accepted"),
        ("rejected", "Upload again"),
    ]

    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="documentRequests")
    requestedBy = models.ForeignKey(
        User, on_delete=models.RESTRICT, related_name="documentRequests"
    )
    message = models.CharField(max_length=200)
    status = models.CharField(max_length=10, choices=STATUSES, default="requested")
    staffReason = models.CharField(max_length=200, blank=True)
    createdAt = models.DateTimeField(default=timezone.now)
    updatedAt = models.DateTimeField(default=timezone.now)
    submittedAt = models.DateTimeField(null=True, blank=True)
    reviewedAt = models.DateTimeField(null=True, blank=True)


class DocumentFile(models.Model):
    request = models.ForeignKey(DocumentRequest, on_delete=models.CASCADE, related_name="files")
    file = models.FileField(storage=DocumentStorage(), upload_to=documentPath)
    originalName = models.CharField(max_length=255)
    size = models.PositiveIntegerField()
    contentType = models.CharField(max_length=50)
    uploadedAt = models.DateTimeField(default=timezone.now)


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
