from django.core.management.base import BaseCommand
from sellerpanel.photocheck import MODEL_VERSION, checkPics, slotWarn
from storefront.models import Listing, ListingPhoto

BATCH = 16


class Command(BaseCommand):
    help = "Refresh photo checks, tags and photo search data for public photos with missing or older model data."

    def handle(self, *args, **options):
        photos = ListingPhoto.objects.filter(listing__in=Listing.objects.public()).exclude(
            modelVersion=MODEL_VERSION, embedding__isnull=False
        )



        photos = list(photos.order_by("pk"))
        saved = 0



        for start in range(0, len(photos), BATCH):
            batch = []



            images = []



            for photo in photos[start : start + BATCH]:
                try:
                    with photo.image.open("rb") as file:
                        images.append(file.read())



                    batch.append(photo)
                except (OSError, ValueError):
                    self.stdout.write(f"Skipped photo {photo.pk}: file missing.")

            for photo, result in zip(batch, checkPics(images)):
                if result and "embedding" in result:
                    photo.tags = result["tags"]



                    photo.embedding = result["embedding"]
                    photo.modelVersion = result.get("modelVersion")


                    warning = slotWarn(result, photo.room)



                    photo.photoReview = [warning] if warning else []
                    photo.aiLabel = result.get("label", "unchecked")


                    photo.aiIrrelevant = result.get("irrelevant")


                    photo.aiGenerated = result.get("aiGenerated")


                    photo.save(
                        update_fields=[
                            "tags",
                            "embedding",
                            "modelVersion",
                            "aiLabel",
                            "aiIrrelevant",
                            "aiGenerated",
                            "photoReview",
                        ]
                    )
                    saved += 1



            self.stdout.write(f"Tagged {saved} of {len(photos)} photos.")
