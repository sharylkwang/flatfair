from django.contrib.auth.models import User
from django.db import models
from django.urls import reverse
from django.utils import timezone

TOPICS = [
    ("buying", "Buying"),



    ("selling", "Selling"),


    ("money", "Loans and grants"),



    ("estates", "Towns and estates"),



    ("renovation", "Renovation"),
]



REASONS = [
    ("scam", "Asks for money or contact outside FlatFair"),



    ("rude", "Rude or unkind"),



    ("spam", "Spam or advert"),


    ("wrong", "Wrong or misleading"),
]


class Post(models.Model):
    author = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="forumPosts"
    )


    question = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="replies"
    )
    topic = models.CharField(max_length=12, choices=TOPICS, blank=True)
    title = models.CharField(max_length=150, blank=True)
    body = models.TextField(max_length=3000)
    hidden = models.BooleanField(default=False)
    hiddenReason = models.CharField(max_length=200, blank=True)
    createdAt = models.DateTimeField(default=timezone.now)
    editedAt = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["question", "createdAt"])]

    def url(self):
        if self.question_id:
            return (
                reverse("community_question", args=[self.question_id])
                + f"?post={self.pk}#post-{self.pk}"
            )


        return reverse("community_question", args=[self.pk])


class Vote(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="forumVotes")
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="votes")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "post"], name="forum_one_vote")
        ]


class Report(models.Model):
    reporter = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="forumReports"
    )
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="reports")
    reason = models.CharField(max_length=10, choices=REASONS)
    resolved = models.BooleanField(default=False)



    createdAt = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["reporter", "post"], name="forum_one_report"
            )
        ]
