from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from .crypto import EncryptedTextField


class ResetIssuanceLock(models.Model):
    requestIp = models.GenericIPAddressField(unique=True)


class ResetCode(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="resetCodes")
    codeHash = models.CharField(max_length=64)
    requestIp = models.GenericIPAddressField(null=True, blank=True)
    createdAt = models.DateTimeField(auto_now_add=True)
    expiresAt = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    verifiedAt = models.DateTimeField(null=True, blank=True)
    usedAt = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "createdAt"]),
            models.Index(fields=["requestIp", "createdAt"]),
        ]


class PasswordHistory(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="passwordHistory")
    passwordHash = models.CharField(max_length=128)
    createdAt = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["user", "createdAt"])]


class EmailCheck(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="emailCheck")
    codeHash = models.CharField(max_length=64, blank=True)
    expiresAt = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveSmallIntegerField(default=0)
    sentCount = models.PositiveIntegerField(default=0)
    sentAt = models.DateTimeField(null=True, blank=True)
    verifiedAt = models.DateTimeField(null=True, blank=True)
    pendingEmail = models.EmailField(max_length=150, blank=True)


