import time
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from vision import brands
from vision.models import PhotoAnalysis, DetectedItem
from vision.services import ai_lock, is_stale


class Command(BaseCommand):
    help = "Read brands and logos on items of finished photo analyses (local CPU, no re-detection)."

    def add_arguments(self, parser):
        parser.add_argument("--listing", type=int, help="Only this listing.")
        parser.add_argument(
            "--force", action="store_true", help="Check again even if this brand check already ran."
        )
        parser.add_argument("--limit", type=int, help="At most this many photos.")

    def handle(self, *args, **options):
        if not brands.ocr_available():
            raise CommandError("Florence-2 weights are missing; run download_vision_models first.")

        scans = (
            PhotoAnalysis.objects.filter(status="done", photo__isnull=False)
            .select_related("photo")
            .order_by("pk")
        )

        if options["listing"]:
            scans = scans.filter(photo__listing_id=options["listing"])


        if not options["force"]:
            scans = scans.filter(
                Q(timings__brands__version__isnull=True)
                | ~Q(timings__brands__version=brands.VERSION)
            )

        if options["limit"] is not None:
            scans = scans[: max(0, options["limit"])]


        named = checked = 0

        for scan in scans:
            if is_stale(scan):
                self.stdout.write(
                    f"Photo {scan.photo_id}: skipped, the photo changed since it was analysed "
                    "(run analyse_listing_photos)"
                )

                continue

            started = time.monotonic()


            result = self.read_brands(scan)


            if result is None:
                self.stdout.write(
                    f"Photo {scan.photo_id}: skipped, the analysis changed while reading"
                )

                continue

            checked += 1
            named += len(result)
            words = ", ".join(f"{label}: {brand}" for label, brand in result) or "no brand"
            seconds = time.monotonic() - started
            self.stdout.write(
                f"Photo {scan.photo_id} listing {scan.photo.listing_id}: {words} {seconds:.1f}s"
            )

        self.stdout.write(f"Checked {checked} photo(s); named {named} item(s).")

    def read_brands(self, analysis):
        with ai_lock():
            analysis.refresh_from_db()


            batch = analysis.timings.get("publication_generation")

            if analysis.status != "done":
                return None

            items = list(analysis.items.order_by("pk"))

            found, report = brands.detect(
                analysis.photo.image.path,
                [{"label": item.label, "polygon": item.polygon, "box": item.box} for item in items],
                analysis.timings.get("size", [1000, 1000]),
            )

            with transaction.atomic():
                current = PhotoAnalysis.objects.select_for_update().get(pk=analysis.pk)

                if (
                    current.status != "done"
                    or current.timings.get("publication_generation") != batch
                ):
                    return None


                changed = []

                for item, brand in zip(items, found):
                    details = brands.with_brand(item.attributes, brand)


                    if details != item.attributes or list(details) != list(item.attributes):
                        item.attributes = details
                        changed.append(item)

                DetectedItem.objects.filter(analysis=current).bulk_update(changed, ["attributes"])


                current.timings = dict(current.timings, brands=report)



                current.save(update_fields=["timings"])


        named = []



        for item, brand in zip(items, found):
            if brand:
                named.append((item.label, brand))

        return named
