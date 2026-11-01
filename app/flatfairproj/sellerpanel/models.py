from django.conf import settings


from django.db import models


class ListingDraft(models.Model):
    seller = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="listingDraft"
    )
    formData = models.JSONField(default=dict)
    draftToken = models.CharField(max_length=32)
    photos = models.JSONField(default=dict)
    checks = models.JSONField(default=dict)
    updatedAt = models.DateTimeField(auto_now=True)


class PackageRecommendation(models.Model):
    listing = models.ForeignKey(
        "storefront.Listing", on_delete=models.CASCADE, related_name="packageRecommendations"
    )
    product = models.ForeignKey(
        "storefront.Product", on_delete=models.CASCADE, related_name="packageRecommendations"
    )
    reason = models.CharField(max_length=300)
    modelVersion = models.CharField(max_length=30)
    createdAt = models.DateTimeField(auto_now_add=True, db_index=True)
    converted = models.BooleanField(default=False)
    orderItem = models.OneToOneField(
        "storefront.OrderItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="packageRecommendation",
    )
