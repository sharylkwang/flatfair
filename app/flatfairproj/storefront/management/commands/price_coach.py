from django.core.management.base import BaseCommand


from storefront.offeradvice import coachSlow


class Command(BaseCommand):


    help = "Suggest a lower price to sellers whose flats have had little interest for 14 days."

    def handle(self, *args, **options):


        self.stdout.write(f"Price suggestions sent: {coachSlow()}")
