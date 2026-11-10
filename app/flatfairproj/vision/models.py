from django.db import models, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone


class PhotoAnalysis(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),



        ("running", "Running"),


        ("done", "Done"),


        ("failed", "Failed"),
    ]



    photo = models.OneToOneField(
        "storefront.ListingPhoto", on_delete=models.CASCADE, related_name="vision_analysis"
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    version = models.CharField(max_length=20, default="1")
    pipelineIdentity = models.JSONField(default=dict, blank=True)



    error = models.TextField(blank=True)
    timings = models.JSONField(default=dict)
    flooring = models.JSONField(default=dict)
    layout = models.TextField(blank=True)


class DetectedItem(models.Model):
    analysis = models.ForeignKey(PhotoAnalysis, on_delete=models.CASCADE, related_name="items")
    label = models.CharField(max_length=60)
    score = models.FloatField()
    box = models.JSONField(default=list)


    polygon = models.JSONField(default=list)
    crop = models.ImageField(upload_to="vision/")
    attributes = models.JSONField(default=dict)
    placement = models.CharField(max_length=40, blank=True)
    embedding = models.JSONField(default=list)


class ItemPick(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),



        ("running", "Running"),



        ("done", "Done"),



        ("empty", "Nothing found"),


        ("failed", "Failed"),
    ]
    analysis = models.ForeignKey(PhotoAnalysis, on_delete=models.CASCADE, related_name="picks")
    sessionKey = models.CharField(max_length=40, db_index=True)
    x = models.FloatField()
    y = models.FloatField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    label = models.CharField(max_length=60, blank=True)
    confident = models.BooleanField(default=False)
    box = models.JSONField(default=list)



    polygon = models.JSONField(default=list)
    crop = models.ImageField(upload_to="vision/picks/", blank=True)
    embedding = models.JSONField(default=list)
    createdAt = models.DateTimeField(default=timezone.now)


@receiver(post_delete, sender=ItemPick)
@receiver(post_delete, sender=DetectedItem)
def deleteCrop(sender, instance, using, **kwargs):
    if instance.crop:
        storage = instance.crop.storage



        name = instance.crop.name


        transaction.on_commit(lambda: storage.delete(name), using=using, robust=True)
