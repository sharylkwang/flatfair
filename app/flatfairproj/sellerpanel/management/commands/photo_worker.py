import os


import signal


from django.core.management.base import BaseCommand



from sellerpanel.photoworker import IDLE_SECONDS, capMemory, runWorker


class Command(BaseCommand):



    def add_arguments(self, parser):


        parser.add_argument("--once", action="store_true")



        parser.add_argument("--idle-seconds", type=float, default=IDLE_SECONDS)

    def handle(self, *args, **options):



        os.environ["CUDA_VISIBLE_DEVICES"] = ""


        signal.signal(signal.SIGTERM, signal.SIG_DFL)


        capMemory()


        count = runWorker(once=options["once"], idleSeconds=max(0, options["idle_seconds"]))



        self.stdout.write(f"Checked {count} photos.")
