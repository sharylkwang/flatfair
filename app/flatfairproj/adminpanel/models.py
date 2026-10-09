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


