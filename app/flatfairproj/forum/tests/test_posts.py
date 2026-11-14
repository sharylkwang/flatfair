from django.test import TestCase
from django.urls import reverse

from adminpanel.models import AuditLog
from forum.models import Post, Report, Vote
from .helpers import makeQuestion, makeReply
from storefront.models import ActivityNotice
from storefront.tests.helpers import makeStaff, makeUser


class AskTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("seller@test.com", "seller", "Qwer")

    def test_asking_question_requires_sign(self):
        response = self.client.get(reverse("community_ask"))
        self.assertRedirects(
            response,
            reverse("signin") + "?next=" + reverse("community_ask"),
            fetch_redirect_response=False,
        )

    def test_seller_create_question(self):
        self.client.force_login(self.seller)
        data = {
            "topic": "selling",
            "title": "Best day for viewings?",
            "body": "Weekend or weekday evening?",
        }
        response = self.client.post(reverse("community_ask"), data)
        question = Post.objects.get()
        self.assertRedirects(
            response,
            reverse("community_question", args=[question.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(question.author, self.seller)
        self.assertIsNone(question.question)

    def test_invalid_question_shows_errors(self):
        self.client.force_login(self.seller)
        response = self.client.post(reverse("community_ask"), {"topic": "nope", "title": "", "body": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Write your question.")
        self.assertFalse(Post.objects.exists())

    def test_staff_blocks_create_community(self):
        self.client.force_login(makeStaff("admin2@test.com", "Admin"))
        response = self.client.post(
            reverse("community_ask"), {"topic": "selling", "title": "Hi", "body": "Hi"}
        )
        self.assertRedirects(response, reverse("community"), fetch_redirect_response=False)
        self.assertFalse(Post.objects.exists())


class ReplyTests(TestCase):

    def setUp(self):
        self.asker = makeUser("buyer@test.com", "buyer", "Asdf")
        self.seller = makeUser("seller@test.com", "seller", "Qwer")
        self.question = makeQuestion(self.asker)
        self.url = reverse("community_question", args=[self.question.pk])

    def test_replying_requires_sign_in(self):
        response = self.client.post(self.url, {"body": "Hello"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("signin"), response["Location"])
        self.assertEqual(self.question.replies.count(), 0)

    def test_notifies_question_author(self):
        self.client.force_login(self.seller)
        response = self.client.post(self.url, {"body": "Four months for us."})
        reply = self.question.replies.get()
        self.assertRedirects(
            response, self.url + f"?post={reply.pk}#post-{reply.pk}", fetch_redirect_response=False
        )
        notice = ActivityNotice.objects.get(user=self.asker)
        self.assertEqual(notice.kind, "community_reply")
        self.assertEqual(notice.url, self.url + f"?post={reply.pk}#post-{reply.pk}")
        self.assertIn("Qwer replied", notice.text)

    def test_own_question_not_create(self):
        self.client.force_login(self.asker)
        self.client.post(self.url, {"body": "Any more tips?"})
        self.assertEqual(self.question.replies.count(), 1)
        self.assertFalse(ActivityNotice.objects.exists())

    def test_blank_reply_is_rejected(self):
        self.client.force_login(self.seller)
        response = self.client.post(self.url, {"body": ""})
        self.assertContains(response, "Write your reply first.")
        self.assertEqual(self.question.replies.count(), 0)


class VoteTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.asker = makeUser("buyer@test.com", "buyer", "Asdf")
        cls.seller = makeUser("seller@test.com", "seller", "Qwer")
        cls.question = makeQuestion(cls.asker)
        cls.reply = makeReply(cls.question, cls.seller)

    def test_posting_again_toggles_off(self):
        self.client.force_login(self.seller)
        url = reverse("community_vote", args=[self.question.pk])
        self.client.post(url)
        self.assertEqual(Vote.objects.filter(post=self.question).count(), 1)
        self.client.post(url)
        self.assertEqual(Vote.objects.filter(post=self.question).count(), 0)

    def test_author_blocks_own_reply(self):
        self.client.force_login(self.seller)
        self.client.post(reverse("community_vote", args=[self.reply.pk]))
        self.assertFalse(Vote.objects.exists())

    def test_voting_reply_redirects_shows(self):
        self.client.force_login(self.asker)
        response = self.client.post(reverse("community_vote", args=[self.reply.pk]))
        self.assertRedirects(response, self.reply.url(), fetch_redirect_response=False)
        response = self.client.get(reverse("community_question", args=[self.question.pk]))
        self.assertContains(response, "Voted · 1")

    def test_voting_requires_sign_in(self):
        self.assertEqual(self.client.get(reverse("community_vote", args=[self.question.pk])).status_code, 302)
        self.client.force_login(self.asker)
        self.assertEqual(self.client.get(reverse("community_vote", args=[self.reply.pk])).status_code, 405)


class ReportTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.asker = makeUser("buyer@test.com", "buyer", "Asdf")
        cls.question = makeQuestion(cls.asker)
        cls.reply = makeReply(
            cls.question, makeUser("seller@test.com", "seller", "Qwer"), "PayNow me a deposit"
        )

    def test_duplicate_retains_original(self):
        self.client.force_login(self.asker)
        url = reverse("community_report", args=[self.reply.pk])
        self.client.post(url, {"reason": "scam"})
        self.client.post(url, {"reason": "spam"})
        report = Report.objects.get()
        self.assertEqual((report.reporter, report.post, report.reason), (self.asker, self.reply, "scam"))

    def test_invalid_reason_reporting_own(self):
        self.client.force_login(self.asker)
        self.client.post(reverse("community_report", args=[self.reply.pk]), {"reason": "bad"})
        self.client.post(reverse("community_report", args=[self.question.pk]), {"reason": "spam"})
        self.assertFalse(Report.objects.exists())


class EditTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.asker = makeUser("buyer@test.com", "buyer", "Asdf")
        cls.seller = makeUser("seller@test.com", "seller", "Qwer")
        cls.question = makeQuestion(cls.asker)
        cls.reply = makeReply(cls.question, cls.seller)

    def test_author_reply_show_edited(self):
        self.client.force_login(self.seller)
        response = self.client.post(
            reverse("community_edit", args=[self.reply.pk]), {"body": "Five months in the end."}
        )
        self.assertRedirects(response, self.reply.url(), fetch_redirect_response=False)
        self.reply.refresh_from_db()
        self.assertEqual(self.reply.body, "Five months in the end.")
        self.assertIsNotNone(self.reply.editedAt)
        page = self.client.get(reverse("community_question", args=[self.question.pk]))
        self.assertContains(page, "Edited</span>")

    def test_author_question_topic_title(self):
        self.client.force_login(self.asker)
        data = {"topic": "buying", "title": "How long did yours take?", "body": "We just listed."}
        self.client.post(reverse("community_edit", args=[self.question.pk]), data)
        self.question.refresh_from_db()
        self.assertEqual((self.question.topic, self.question.title), ("buying", "How long did yours take?"))
        self.assertIsNotNone(self.question.editedAt)

    def test_editing_another_members_reply(self):
        self.client.force_login(self.asker)
        response = self.client.post(reverse("community_edit", args=[self.reply.pk]), {"body": "Changed"})
        self.assertEqual(response.status_code, 404)
        self.reply.refresh_from_db()
        self.assertIsNone(self.reply.editedAt)

    def test_hidden_replies_questions_blocks(self):
        self.reply.hidden = True
        self.reply.save()
        self.client.force_login(self.seller)
        response = self.client.post(reverse("community_edit", args=[self.reply.pk]), {"body": "Changed"})
        self.assertEqual(response.status_code, 404)
        self.question.hidden = True
        self.question.save()
        self.client.force_login(self.asker)
        self.assertEqual(self.client.get(reverse("community_edit", args=[self.question.pk])).status_code, 404)

    def test_blank_unchanged_reply_not(self):
        self.client.force_login(self.seller)
        response = self.client.post(reverse("community_edit", args=[self.reply.pk]), {"body": ""})
        self.assertContains(response, "Write your reply first.")
        self.client.post(reverse("community_edit", args=[self.reply.pk]), {"body": self.reply.body})
        self.reply.refresh_from_db()
        self.assertIsNone(self.reply.editedAt)

    def test_link_visible_signed_in(self):
        url = reverse("community_edit", args=[self.reply.pk])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.seller)
        self.assertContains(self.client.get(reverse("community_question", args=[self.question.pk])), url)
        self.client.force_login(self.asker)
        self.assertNotContains(self.client.get(reverse("community_question", args=[self.question.pk])), url)


class ModerationTests(TestCase):

    def setUp(self):
        self.buyer = makeUser("buyer@test.com", "buyer", "Asdf")
        self.question = makeQuestion(self.buyer)
        self.reply = makeReply(
            self.question, makeUser("seller@test.com", "seller", "Qwer"), "PayNow me a deposit"
        )
        Report.objects.create(reporter=self.buyer, post=self.reply, reason="scam")
        self.otherAdmin = makeStaff("admin2@test.com", "Admin")

    def test_buyer_blocks_open_hide(self):
        self.client.force_login(self.buyer)
        self.assertEqual(self.client.get(reverse("admin_community")).status_code, 403)
        response = self.client.post(
            reverse("admin_community_action", args=[self.reply.pk]),
            {"action": "hide", "reason": "x"},
        )
        self.assertEqual(response.status_code, 403)
        self.reply.refresh_from_db()
        self.assertFalse(self.reply.hidden)

    def test_admins_view_reported_posts(self):
        for staff in [self.otherAdmin, makeStaff("admin@test.com", "Admin")]:
            self.client.force_login(staff)
            response = self.client.get(reverse("admin_community"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(list(response.context["rows"]), [self.reply])
            row = (
                response.content.decode().split(f'id="post-{self.reply.pk}"', 1)[1].split("</article>", 1)[0]
            )
            self.assertEqual(row.count("<a "), 1)
            self.assertIn(
                f'href="{reverse('admin_community_post', args=[self.reply.pk])}">View post <span',
                row,
            )
            self.assertNotIn("<details", row)
            self.assertNotIn("<form", row)
            post = self.client.get(reverse("admin_community_post", args=[self.reply.pk]))
            for check_text_1 in (
                "Asks for money or contact outside FlatFair",
                'name="action" value="hide" data-ask="Hide this post from the community?" data-yes="Hide post" data-no="Keep it up"',
                'name="action" value="keep" formnovalidate',
            ):
                self.assertContains(post, check_text_1)
            self.assertRedirects(
                self.client.get(reverse("admin_community_post", args=[self.reply.pk + 99])),
                reverse("admin_community"),
            )

    def test_hiding_post_resolves_reports(self):
        self.client.force_login(self.otherAdmin)
        url = reverse("admin_community_action", args=[self.reply.pk])
        self.client.post(url, {"action": "hide", "reason": "Asks for a deposit"})
        self.reply.refresh_from_db()
        self.assertTrue(self.reply.hidden)
        self.assertEqual(self.reply.hiddenReason, "Asks for a deposit")
        self.assertFalse(Report.objects.filter(resolved=False).exists())
        entry = AuditLog.objects.get()
        self.assertEqual(
            (entry.actor, entry.action, entry.target),
            (self.otherAdmin, "community post hidden", f"Community post #{self.reply.pk}"),
        )
        response = self.client.get(reverse("community_question", args=[self.question.pk]))
        self.assertNotContains(response, "PayNow me a deposit")

    def test_hiding_post_requires_reason(self):
        self.client.force_login(self.otherAdmin)
        url = reverse("admin_community_action", args=[self.reply.pk])
        self.client.post(url, {"action": "hide", "reason": ""})
        self.reply.refresh_from_db()
        self.assertFalse(self.reply.hidden)
        self.assertFalse(AuditLog.objects.exists())

    def test_hidden_posts_restored_audit(self):
        self.reply.hidden = True
        self.reply.save()
        self.client.force_login(self.otherAdmin)
        response = self.client.get(reverse("admin_community") + "?show=hidden")
        self.assertEqual(list(response.context["rows"]), [self.reply])
        self.client.post(
            reverse("admin_community_action", args=[self.reply.pk]),
            {"action": "show", "reason": "Checked with the seller"},
        )
        self.reply.refresh_from_db()
        self.assertFalse(self.reply.hidden)
        self.assertEqual(AuditLog.objects.get().action, "community post shown")

    def test_keeping_post_resolves_report(self):
        self.client.force_login(self.otherAdmin)
        self.client.post(reverse("admin_community_action", args=[self.reply.pk]), {"action": "keep"})
        self.reply.refresh_from_db()
        self.assertFalse(self.reply.hidden)
        self.assertTrue(Report.objects.get().resolved)
        self.assertEqual(list(self.client.get(reverse("admin_community")).context["rows"]), [])
