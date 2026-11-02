import time
from django.core.management.base import BaseCommand, CommandError
from storefront.models import ListingPhoto
from vision.pipeline import weights_ok
from vision.services import process, is_stale
from vision.worker import VISION_HARD_MB, memory_cap, trim


class Command(BaseCommand):
    help = "Analyse listing photos sequentially using local CPU models."

    def add_arguments(self, parser):
        parser.add_argument("--listing", type=int)
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--limit", type=int)

    def handle(self, *args, **options):
        if not weights_ok():
            raise CommandError("Vision weights are missing; run download_vision_models first.")

        memory_cap(VISION_HARD_MB)
        photos = ListingPhoto.objects.order_by("pk")

        if options["listing"]:
            photos = photos.filter(listing_id=options["listing"])

        if not options["force"]:
            undone = []

            for maybe_pic in photos.select_related("vision_analysis"):
                if (
                    not hasattr(maybe_pic, "vision_analysis")
                    or maybe_pic.vision_analysis.status != "done"
                    or is_stale(maybe_pic.vision_analysis)
                ):
                    undone.append(maybe_pic)

            photos = undone

        if options["limit"] is not None:
            photos = photos[: max(0, options["limit"])]

        for photo in photos:
            started = time.monotonic()
            result = process(photo, options["force"])
            trim()
            seconds = time.monotonic() - started
            self.stdout.write(
                f"Photo {photo.pk} listing {photo.listing_id}: {result.status} {seconds:.2f}s {result.error}"
            )
