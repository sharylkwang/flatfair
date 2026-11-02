import os
import signal
import threading
import time
from django.core.management.base import BaseCommand
from django.db import close_old_connections


class Command(BaseCommand):
    help = "Run the single sequential vision queue in a separate process."

    def add_arguments(self, parser):
        parser.add_argument("--parent", type=int)

    def handle(self, *args, **options):
        from storefront.models import ListingPhoto
        from vision import queue
        from vision.models import PhotoAnalysis

        stopped = threading.Event()

        def stop(*_):
            stopped.set()
            queue.wake()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        if options["parent"]:

            def watch():
                while os.getppid() == options["parent"] and not stopped.is_set():
                    time.sleep(2)

                stop()

            threading.Thread(target=watch, daemon=True, name="parent-watch").start()

        waiting = ListingPhoto.objects.filter(listing__status="live", vision_analysis__isnull=True)
        PhotoAnalysis.objects.bulk_create(
            [PhotoAnalysis(photo=photo) for photo in waiting], ignore_conflicts=True
        )
        queue._supervise(stopped)
        close_old_connections()
