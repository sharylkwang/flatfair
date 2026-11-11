from datetime import timedelta
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from forum.models import Post, Report, Vote

SCAM = "I have a cheaper flat nearby. PayNow a $500 deposit to 9123 4567 and I will hold it for you."
EDITED = "As a buyer, clean walls made the flat look bigger in the photos."

QUESTIONS = [
    (
        "selling",
        "seller1",
        12,
        5,
        "How long did your HDB resale take from listing to keys?",
        "We listed last month and have had six viewings. Friends say the whole process takes about four months. Is that normal?",
        [
            (
                "buyer3",
                "Ours took about 14 weeks. The HDB approval took around 8 weeks after we agreed on the price.",
            ),
            (
                "seller4",
                "Around four months for us too. Waiting for the valuation was the slowest part.",
            ),
        ],
    ),
    (
        "buying",
        "buyer1",
        10,
        8,
        "Did you pay above the valuation for your flat?",
        "We like a 4-room in Tampines near the MRT, but the asking price is above recent sales. How did you decide how much was too much?",
        [
            (
                "buyer4",
                "We checked recent sales in the same block with the price check and set a limit before the viewing.",
            ),
            (
                "seller2",
                "As a seller, I was happy to go a little lower for a buyer who could follow my timeline.",
            ),
            ("seller14", SCAM),
        ],
    ),
    (
        "money",
        "buyer2",
        8,
        6,
        "When should we apply for the HFE letter?",
        "We are just starting to look. Do we need the HDB Flat Eligibility letter before we make an offer?",
        [
            (
                "buyer5",
                "Get it early. Sellers asked us for it at the first viewing, and it took a few weeks to come.",
            ),
            (
                "seller3",
                "I only shortlisted offers from buyers who already had their HFE letter.",
            ),
            (
                "buyer1",
                "We applied two months before we started viewing. It made the offer step much faster.",
            ),
        ],
    ),
    (
        "estates",
        "buyer3",
        6,
        3,
        "Punggol or Sengkang for a young family?",
        "Both towns have flats near schools and parks. Which one worked better for you, and why?",
        [
            (
                "seller5",
                "We lived in Sengkang for eight years. The LRT is handy and there are many childcare centres near the blocks.",
            ),
            (
                "buyer6",
                "Punggol Waterfront is lovely on weekends, but check the bus links to your workplace first.",
            ),
        ],
    ),
    (
        "renovation",
        "seller6",
        4,
        4,
        "Should I repaint before selling?",
        "Our walls have some marks from the kids. Is it worth repainting the whole flat before viewings?",
        [
            (
                "seller7",
                "I only repainted the living room and the corridor. Most buyers plan their own renovation anyway.",
            ),
            ("buyer1", EDITED),
        ],
    ),
    (
        "selling",
        "seller8",
        2,
        2,
        "How do you handle viewings with young kids at home?",
        "We still live in the flat with two toddlers. How did you keep viewings short and calm?",
        [
            (
                "seller9",
                "We set two viewing windows each weekend and took the kids to the playground during them.",
            ),
            (
                "buyer1",
                "As a buyer, I did not mind toys around. A tidy living room was enough.",
            ),
        ],
    ),
]


class Command(BaseCommand):
    help = "Add demo community questions with replies, votes, one edited reply and one reported reply."

    @transaction.atomic
    def handle(self, *args, **options):
        now = timezone.now()



        people = {}


        for user in User.objects.filter(
            username__endswith="@demo.flatfair.sg"
        ).order_by("username"):
            people[user.username.split("@")[0]] = user


        for topic, asker, days, votes, title, body, replies in QUESTIONS:
            author = people.get(asker)


            if author is None:
                continue


            question = Post.objects.filter(question=None, title=title).first()



            if question is None:
                asked = now - timedelta(days=days)


                question = Post.objects.create(
                    author=author, topic=topic, title=title, body=body, createdAt=asked
                )


                voters = [user for user in people.values() if user != author][:votes]

                Vote.objects.bulk_create(
                    [Vote(user=user, post=question) for user in voters]
                )



            for i, (name, text) in enumerate(replies):
                if name in people and not question.replies.filter(body=text).exists():
                    replied = question.createdAt + timedelta(hours=3 + i * 5)



                    Post.objects.create(
                        author=people[name],
                        question=question,
                        body=text,
                        createdAt=replied,
                    )

        Post.objects.filter(body=EDITED, editedAt=None).update(
            editedAt=F("createdAt") + timedelta(hours=2)
        )

        scam = Post.objects.filter(body=SCAM).first()



        if scam and "buyer1" in people:
            Report.objects.get_or_create(
                reporter=people["buyer1"], post=scam, defaults={"reason": "scam"}
            )



        self.stdout.write(self.style.SUCCESS("Community demo ready."))
