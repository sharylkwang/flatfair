from django.apps import AppConfig


class VisionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "vision"

    def ready(self):
        from django.db import transaction
        from django.db.models.signals import post_save
        from storefront.models import Listing, ListingPhoto
        from .queue import queue_flat, start

        def published(sender, instance, **kwargs):
            if sender is Listing:
                listing_id = instance.pk
            else:
                listing_id = instance.listing_id

            transaction.on_commit(lambda: queue_flat(listing_id))

        post_save.connect(published, sender=Listing, weak=False, dispatch_uid="vision-listing")
        post_save.connect(published, sender=ListingPhoto, weak=False, dispatch_uid="vision-photo")
        start()
