import math


import time



from django.core.management.base import BaseCommand, CommandError



from sellerpanel.reconciliation import syncBatch


class Command(BaseCommand):
    help = "Check stale pending Stripe payments and refunds and place verified purchases."

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop",
            type=float,
            metavar="SECONDS",
            help="Repeat checks every SECONDS until stopped.",


        )

    def handle(self, *args, **options):


        gap = options["loop"]


        if gap is not None and (not math.isfinite(gap) or gap <= 0):
            raise CommandError("--loop must be a positive number of seconds.")


        try:
            while True:


                self.reconcile()


                if gap is None:



                    break



                time.sleep(gap)
        except KeyboardInterrupt:
            self.stdout.write("Payment reconciliation stopped.")


    def reconcile(self):
        checked, refunds, failed = syncBatch(self.stdout.write)


        self.stdout.write(self.style.SUCCESS(f"Checked {checked} stale pending Stripe payments."))


        self.stdout.write(
            f"Checked {refunds} pending Stripe refunds. Failed {failed} reconciliation checks."
        )
