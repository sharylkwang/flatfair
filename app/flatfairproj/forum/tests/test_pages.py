from datetime import timedelta, datetime, timezone as datetimeTimezone
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from forum.models import Post, Vote, Report
from forum.views import userFame
from .helpers import makeQuestion, makeReply
from storefront.tests.helpers import makeStaff, makeUser


class ListTests(TestCase):

    def setUp(self):
        self.buyer = makeUser("buyer@test.com", "buyer", "Asdf")
        self.seller = makeUser("seller@test.com", "seller", "Qwer")
        old = timezone.now() - timedelta(days=3)
        self.popular = makeQuestion(self.seller, "Is the valuation fair?", "buying", createdAt=old)
        self.fresh = makeQuestion(self.buyer, "Repaint before selling?", "renovation")
        Vote.objects.create(user=self.buyer, post=self.popular)

    def test_community_newest_questions(self):
        response = self.client.get(reverse("community"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["rows"]), [self.fresh, self.popular])

    def test_vote_sort_most_popular(self):
        response = self.client.get(reverse("community") + "?sort=votes")
        self.assertEqual(list(response.context["rows"]), [self.popular, self.fresh])
        self.assertEqual(response.context["rows"][0].voteCount, 1)

    def test_topic_case_insensitive_search(self):
        response = self.client.get(reverse("community") + "?topic=renovation")
        self.assertEqual(list(response.context["rows"]), [self.fresh])
        response = self.client.get(reverse("community") + "?q=VALUATION")
        self.assertEqual(list(response.context["rows"]), [self.popular])

    def test_hidden_questions_omitted(self):
        self.fresh.hidden = True
        self.fresh.save()
        response = self.client.get(reverse("community"))
        self.assertEqual(list(response.context["rows"]), [self.popular])
        self.assertEqual(
            self.client.get(reverse("community_question", args=[self.fresh.pk])).status_code, 404
        )


class QuestionPageTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.buyer = makeUser("buyer@test.com", "buyer", "Asdf")
        cls.question = makeQuestion(cls.buyer)
        cls.reply = makeReply(cls.question, makeUser("seller@test.com", "seller", "Qwer"))
        cls.gone = makeReply(cls.question, cls.buyer, "Removed text", hidden=True)

    def test_shows_visible_replies_sign(self):
        response = self.client.get(reverse("community_question", args=[self.question.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "About four months for us.")
        self.assertNotContains(response, "Removed text")
        self.assertContains(response, "Sign in to reply")

    def test_reply_id_blocks_used(self):
        self.assertEqual(
            self.client.get(reverse("community_question", args=[self.reply.pk])).status_code, 404
        )

    def test_home_links_to_community(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, reverse("community"))

    def test_replies_paginate_permalink(self):
        start = timezone.now() - timedelta(days=1)
        seller = self.reply.author
        posts = []
        for i in range(25):
            when = start + timedelta(minutes=i)
            posts.append(Post(author=seller, question=self.question, body=f"Tip {i}", createdAt=when))
        Post.objects.bulk_create(posts)
        url = reverse("community_question", args=[self.question.pk])
        response = self.client.get(url)
        self.assertEqual(len(response.context["replies"]), 20)
        self.assertContains(response, "26 replies")
        self.assertContains(response, "Page 1 of 2")
        self.assertEqual(response.context["replies"][0].body, "Tip 0")
        self.assertEqual(
            self.client.get(url + "?page=2").context["replies"][5].body, "About four months for us."
        )
        response = self.client.get(self.reply.url())
        self.assertEqual(response.context["replies"].number, 2)

    def test_invalid_post_query_falls(self):
        url = reverse("community_question", args=[self.question.pk])
        for postId in ("²", "9" * 5000):
            with self.subTest(postId=postId[:20]):
                response = self.client.get(url, {"post": postId})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["replies"].number, 1)


class MemberTests(TestCase):

    def setUp(self):
        self.buyer = makeUser("buyer@test.com", "buyer", "Asdf")
        self.seller = makeUser("seller@test.com", "seller", "Qwer")
        self.question = makeQuestion(self.buyer)
        self.reply = makeReply(self.question, self.seller)
        self.hidden = makeReply(self.question, self.seller, "Removed text", hidden=True)
        Vote.objects.create(user=self.buyer, post=self.reply)

    def test_page_shows_visible_replies(self):
        response = self.client.get(reverse("community_member", args=[self.seller.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["replies"]), [self.reply])
        self.assertEqual(response.context["voteCount"], 1)
        self.assertContains(response, "How long does resale take?")
        self.assertNotContains(response, "Removed text")
        self.assertNotContains(response, "seller@test.com")

    def test_missing_public_posts_visible(self):
        self.question.hidden = True
        self.question.save()
        self.assertEqual(self.client.get(reverse("community_member", args=[self.seller.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("community_member", args=[self.buyer.pk])).status_code, 404)
        self.client.force_login(self.buyer)
        self.assertEqual(self.client.get(reverse("community_member", args=[self.buyer.pk])).status_code, 200)

    def test_staff_no_public_page(self):
        staff = makeStaff("admin2@test.com", "Admin")
        self.assertEqual(self.client.get(reverse("community_member", args=[staff.pk])).status_code, 404)
        response = self.client.get(reverse("community_question", args=[self.question.pk]))
        self.assertContains(response, reverse("community_member", args=[self.seller.pk]))


class ReputationTests(TestCase):

    def setUp(self):
        self.now = datetime(2026, 11, 15, 12, tzinfo=datetimeTimezone.utc)
        self.asker = makeUser("asker@test.com", name="Kelvin")
        self.helper = makeUser("helper@test.com", name="Maya")
        self.voters = [makeUser(f"voter{i}@test.com", name=f"Member {i}") for i in range(3)]
        self.question = makeQuestion(self.asker, createdAt=self.now - timedelta(days=20))

    def reputation(self, request=None):
        with patch("forum.views.timezone.now", return_value=self.now):
            return userFame(request or RequestFactory().get("/community"))

    def test_points_count_votes_replies(self):
        ownQuestion = makeQuestion(self.helper, createdAt=self.now - timedelta(days=20))
        first = makeReply(self.question, self.helper, createdAt=self.now)
        second = makeReply(self.question, self.helper, createdAt=self.now)
        for voter in self.voters:
            Vote.objects.create(user=voter, post=first)
        Vote.objects.create(user=self.voters[0], post=second)
        for voter in self.voters[:2]:
            Vote.objects.create(user=voter, post=ownQuestion)
        hidden = makeReply(self.question, self.helper, hidden=True, createdAt=self.now)
        hiddenQuestion = makeQuestion(self.asker, hidden=True, createdAt=self.now)
        underHidden = makeReply(hiddenQuestion, self.helper, createdAt=self.now)
        for post in (hidden, underHidden):
            Vote.objects.create(user=self.asker, post=post)
        stats = self.reputation()["members"][self.helper.pk]
        self.assertEqual((stats["questionCount"], stats["replyCount"], stats["voteCount"]), (1, 2, 6))
        self.assertEqual(stats["points"], 10)
        self.assertEqual(stats["monthPoints"], 8)
        self.assertEqual(stats["replyVotes"], 4)

    def test_aggregate_cached_on_request(self):
        request = RequestFactory().get("/community")
        with self.assertNumQueries(1):
            first = self.reputation(request)
            self.assertIs(self.reputation(request), first)
        makeReply(self.question, self.helper, createdAt=self.now)
        with self.assertNumQueries(1):
            self.assertEqual(self.reputation()["members"][self.helper.pk]["points"], 2)

    def test_helpful_badge_requires_three(self):
        reply = makeReply(self.question, self.helper, createdAt=self.now - timedelta(days=14))
        for voter in self.voters[:2]:
            Vote.objects.create(user=voter, post=reply)
        stats = self.reputation()["members"][self.helper.pk]
        self.assertNotIn("Helpful", [badge["label"] for badge in stats["badges"]])
        Vote.objects.create(user=self.voters[2], post=reply)
        stats = self.reputation()["members"][self.helper.pk]
        self.assertEqual(
            [badge["label"] for badge in stats["badges"]],
            ["Top contributor", "Helpful", "New member"],
        )
        Post.objects.filter(pk=reply.pk).update(createdAt=self.now - timedelta(days=14, seconds=1))
        stats = self.reputation()["members"][self.helper.pk]
        self.assertNotIn("New member", [badge["label"] for badge in stats["badges"]])
        self.assertTrue(all((badge["title"] for badge in stats["badges"])))

    def test_votes_on_questions_not(self):
        for voter in self.voters:
            Vote.objects.create(user=voter, post=self.question)
        stats = self.reputation()["members"][self.asker.pk]
        self.assertNotIn("Helpful", [badge["label"] for badge in stats["badges"]])
        self.assertNotIn("New member", [badge["label"] for badge in stats["badges"]])

    def test_top_three_monthly_points(self):
        for i, author in enumerate([self.helper, *self.voters]):
            for j in range(i + 1):
                makeReply(self.question, author, createdAt=self.now)
        for i in range(10):
            makeReply(self.question, self.asker, createdAt=self.now - timedelta(days=45))
        reputation = self.reputation()
        leaders = reputation["topContributors"]
        self.assertEqual(reputation["contributorPeriod"], "Posts made this month")
        self.assertEqual(
            [row["id"] for row in leaders],
            [self.voters[2].pk, self.voters[1].pk, self.voters[0].pk, self.helper.pk],
        )
        self.assertTrue(
            all(
                (any((badge["label"] == "Top contributor" for badge in row["badges"])) for row in leaders[:3])
            )
        )
        self.assertFalse(any((badge["label"] == "Top contributor" for badge in leaders[3]["badges"])))
        self.assertEqual(reputation["members"][self.asker.pk]["points"], 20)
        self.assertNotIn(
            "Top contributor",
            [badge["label"] for badge in reputation["members"][self.asker.pk]["badges"]],
        )

    def test_sparse_month_falls_back(self):
        for i in range(3):
            makeReply(self.question, self.helper, createdAt=self.now - timedelta(days=45))
        makeReply(self.question, self.voters[0], createdAt=self.now)
        reputation = self.reputation()
        self.assertEqual(reputation["contributorPeriod"], "Overall")
        self.assertEqual(
            [row["id"] for row in reputation["topContributors"]],
            [self.helper.pk, self.voters[0].pk],
        )
        self.assertIn(
            "Top contributor",
            [badge["label"] for badge in reputation["members"][self.helper.pk]["badges"]],
        )

    def test_sidebars_limit_five_members(self):
        for i in range(7):
            author = makeUser(f"active{i}@test.com", name=f"Active {i}")
            for j in range(i + 1):
                makeReply(self.question, author, createdAt=self.now - timedelta(days=1))
        with patch("forum.views.timezone.now", return_value=self.now):
            response = self.client.get(reverse("community"))
        self.assertEqual(len(response.context["topContributors"]), 5)
        active = response.context["activeMembers"]
        self.assertEqual([row["activeCount"] for row in active], [7, 6, 5, 4, 3])
        self.assertEqual(response.context["activePeriod"], "Last 7 days")
        for check_text_1 in ("Top contributors", "Most active this week", "Popular topics"):
            self.assertNotContains(response, check_text_1)

    def test_activity_falls_back_thirty(self):
        makeReply(self.question, self.helper, createdAt=self.now - timedelta(days=10))
        reputation = self.reputation()
        self.assertEqual(reputation["activePeriod"], "Last 30 days")
        self.assertEqual([row["id"] for row in reputation["activeMembers"]], [self.helper.pk, self.asker.pk])
        Post.objects.update(createdAt=self.now - timedelta(days=31))
        self.assertEqual(self.reputation()["activeMembers"], [])

    def test_popular_topics_count_visible(self):
        makeQuestion(self.helper, topic="buying", createdAt=self.now)
        makeQuestion(self.helper, topic="buying", createdAt=self.now)
        makeQuestion(self.helper, topic="buying", hidden=True, createdAt=self.now)
        makeReply(self.question, self.helper, createdAt=self.now)
        response = self.client.get(reverse("community"), {"topic": "renovation", "q": "missing"})
        topics = response.context["popularTopics"]
        self.assertEqual((topics[0]["label"], topics[0]["count"]), ("Buying", 2))
        self.assertEqual(sum((row["count"] for row in topics)), 3)

    def test_list_includes_excerpt_relative(self):
        Post.objects.filter(pk=self.question.pk).update(createdAt=self.now - timedelta(days=3))
        with patch("forum.views.timezone.now", return_value=self.now):
            response = self.client.get(reverse("community"))
        for check_text_2 in ("We just listed our flat.", "3 days ago", "community-avatar", "New member"):
            self.assertContains(response, check_text_2)
        self.assertContains(response, reverse("community_member", args=[self.asker.pk]))

    def test_question_tags_visible_replies(self):
        replies = [makeReply(self.question, self.helper, createdAt=self.now) for i in range(3)]
        response = self.client.get(reverse("community"))
        self.assertContains(response, ">Active</span>")
        Vote.objects.create(user=self.asker, post=replies[0])
        response = self.client.get(reverse("community"))
        self.assertContains(response, ">Answered</span>")
        Post.objects.filter(pk=replies[0].pk).update(hidden=True)
        response = self.client.get(reverse("community"))
        self.assertNotContains(response, ">Answered</span>")
        self.assertNotContains(response, ">Active</span>")

    def test_member_header_points_counts(self):
        reply = makeReply(self.question, self.helper, body="Here is a recent reply.", createdAt=self.now)
        Vote.objects.create(user=self.asker, post=reply)
        with self.assertNumQueries(3):
            response = self.client.get(reverse("community_member", args=[self.helper.pk]))
        self.assertEqual(response.context["memberStats"]["points"], 3)
        self.assertEqual(
            (
                response.context["questionCount"],
                response.context["replyCount"],
                response.context["voteCount"],
            ),
            (0, 1, 1),
        )
        self.assertEqual(list(response.context["recentPosts"]), [reply])
        for text in (
            "Recent posts",
            "Points",
            "Votes received",
            "New member",
            "Here is a recent reply.",
        ):
            self.assertContains(response, text)

    def test_list_query_count_stays(self):
        with self.assertNumQueries(4):
            self.assertEqual(self.client.get(reverse("community")).status_code, 200)
        for i in range(10):
            author = makeUser(f"author{i}@test.com", name=f"Author {i}")
            item = makeQuestion(author)
            reply = makeReply(item, self.helper)
            Vote.objects.create(user=self.asker, post=reply)
        with self.assertNumQueries(4):
            response = self.client.get(reverse("community"))
        self.assertEqual(len(response.context["rows"]), 10)
        self.client.force_login(self.asker)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("community"))
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(queries), 12)


class TopAnswerTests(TestCase):

    def setUp(self):
        self.asker = makeUser("asker@test.com", name="Kelvin")
        self.helper = makeUser("helper@test.com", name="Maya")
        self.voter = makeUser("voter@test.com", name="Anita")
        self.question = makeQuestion(self.asker)
        self.early = makeReply(self.question, self.asker, body="An update from the asker.")
        self.top = makeReply(self.question, self.helper, body="The most useful reply.")
        self.url = reverse("community_question", args=[self.question.pk])

    def test_highest_voted_visible_reply(self):
        Vote.objects.create(user=self.asker, post=self.top)
        Vote.objects.create(user=self.voter, post=self.top)
        Vote.objects.create(user=self.helper, post=self.early)
        hidden = makeReply(self.question, self.helper, hidden=True)
        for voter in (self.asker, self.helper, self.voter):
            Vote.objects.create(user=voter, post=hidden)
        response = self.client.get(self.url)
        self.assertEqual(list(response.context["replies"]), [self.top, self.early])
        self.assertEqual(response.context["topAnswerId"], self.top.pk)
        for check_text_3 in ("Top answer", ">Asker</span>", "2 votes", "Top contributor"):
            self.assertContains(response, check_text_3)

    def test_tied_votes_choose_earliest(self):
        self.assertIsNone(self.client.get(self.url).context["topAnswerId"])
        for post in (self.early, self.top):
            Vote.objects.create(user=self.voter, post=post)
        response = self.client.get(self.url)
        self.assertEqual(response.context["topAnswerId"], self.early.pk)
        self.assertEqual(list(response.context["replies"]), [self.early, self.top])

    def test_vote_ranked_reply_permalinks(self):
        for i in range(22):
            makeReply(self.question, self.helper)
        last = makeReply(self.question, self.helper, body="Last but most voted.")
        Vote.objects.create(user=self.asker, post=last)
        Vote.objects.create(user=self.voter, post=self.top)
        response = self.client.get(last.url())
        self.assertEqual(response.context["replies"].number, 1)
        self.assertIn(last, response.context["replies"])
        response = self.client.get(self.early.url())
        self.assertEqual(response.context["replies"].number, 1)
        self.assertIn(self.early, response.context["replies"])
        target = self.question.replies.order_by("pk")[21]
        response = self.client.get(target.url())
        self.assertEqual(response.context["replies"].number, 2)
        self.assertIn(target, response.context["replies"])


PEOPLE = [
    ("buyer1", "buyer"),
    ("buyer2", "buyer"),
    ("buyer3", "buyer"),
    ("seller1", "seller"),
    ("seller6", "seller"),
    ("seller8", "seller"),
    ("seller14", "seller"),
]


class SeedTests(TestCase):

    def test_seeding_twice_keeps_expected(self):
        for name, role in PEOPLE:
            makeUser(f"{name}@demo.flatfair.sg", role)
        call_command("seed_forum", stdout=StringIO())
        call_command("seed_forum", stdout=StringIO())
        self.assertEqual(Post.objects.filter(question=None).count(), 6)
        self.assertEqual(Report.objects.count(), 1)
        self.assertEqual(Report.objects.get().post.author.username, "seller14@demo.flatfair.sg")
        mine = Post.objects.filter(author__username="buyer1@demo.flatfair.sg")
        self.assertEqual(mine.count(), 4)
        self.assertEqual(mine.exclude(editedAt=None).count(), 1)
