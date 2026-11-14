from django.test import TestCase
from django.urls import reverse

from adminpanel.models import AuditLog, Ticket, TicketMessage
from storefront.models import ActivityNotice, Product, Review
from storefront.presentation import newReviews, addRatings
from storefront.tests.helpers import makeOrder, makeProduct, makeStaff, makeUser


class TicketTests(TestCase):

    def setUp(self):
        self.staff = makeStaff("admin2@test.com", "Admin")
        self.seller = makeUser("asdf@test.com", "seller")
        self.ticket = Ticket.objects.create(
            customer=self.seller, email="asdf@test.com", topic="refund", message="asdf"
        )
        self.client.force_login(self.staff)

    def test_board_groups_statuses_links(self):
        Ticket.objects.create(email="qwer@test.com", topic="other", message="qwer", status="resolved")
        columns = self.client.get(reverse("admin_tickets")).context["columns"]
        self.assertEqual(
            [(column["key"], len(column["tickets"])) for column in columns],
            [("open", 1), ("resolved", 1)],
        )
        self.assertTrue(columns[0]["tickets"][0].replyLink.startswith("mailto:asdf@test.com"))

    def test_staff_reply_keeps_open(self):
        self.client.post(reverse("admin_ticket", args=[self.ticket.pk]), {"body": "We refunded it."})
        self.ticket.refresh_from_db()
        self.assertEqual((self.ticket.status, self.ticket.assignee), ("open", self.staff))
        reply = TicketMessage.objects.get()
        self.assertTrue(reply.fromStaff)
        self.assertTrue(
            AuditLog.objects.filter(action="ticket replied", target=f"Ticket #{self.ticket.pk}").exists()
        )
        notice = ActivityNotice.objects.get(user=self.seller)
        self.assertEqual(
            (notice.kind, notice.url),
            ("support_reply", reverse("my_ticket", args=[self.ticket.pk])),
        )

    def test_resolved_blocks_further_replies(self):
        url = reverse("admin_ticket_action", args=[self.ticket.pk])
        self.client.post(url, {"action": "resolve"})
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, "resolved")
        self.assertIsNotNone(self.ticket.resolvedAt)
        self.client.post(reverse("admin_ticket", args=[self.ticket.pk]), {"body": "late"})
        self.assertFalse(TicketMessage.objects.exists())

    def test_resolving_tells_customer(self):
        self.client.post(reverse("admin_ticket_action", args=[self.ticket.pk]), {"action": "resolve"})
        notice = ActivityNotice.objects.get(user=self.seller)
        self.assertEqual(
            (notice.kind, notice.url),
            ("support_reply", reverse("my_ticket", args=[self.ticket.pk])),
        )
        self.assertEqual(notice.text, f"Our team marked your message #{self.ticket.pk} as resolved.")
        guest = Ticket.objects.create(email="guest@test.com", topic="other", message="Hello")
        self.client.post(reverse("admin_ticket_action", args=[guest.pk]), {"action": "resolve"})
        self.assertEqual(ActivityNotice.objects.count(), 1)

    def test_guest_resolves_missing_creating(self):
        guest = Ticket.objects.create(email="qwer@test.com", topic="other", message="qwer")
        self.client.post(reverse("admin_ticket", args=[guest.pk]), {"body": "hi"})
        self.assertFalse(TicketMessage.objects.exists())
        response = self.client.post(
            reverse("admin_ticket_action", args=[guest.pk]), {"action": "resolve", "next": "detail"}
        )
        self.assertRedirects(response, reverse("admin_ticket", args=[guest.pk]))

    def test_open_resolved_blocks_assigned(self):
        url = reverse("admin_ticket_action", args=[self.ticket.pk])
        for action in ("assign", "asdf"):
            self.client.post(url, {"action": action})
        self.ticket.refresh_from_db()
        self.assertEqual((self.ticket.status, self.ticket.assignee), ("open", None))
        self.assertFalse(AuditLog.objects.filter(action="ticket assigned").exists())
        self.assertEqual([key for key, label in Ticket.STATUSES], ["open", "resolved"])
        page = self.client.get(reverse("admin_ticket", args=[self.ticket.pk]))
        self.assertNotContains(page, "Assign to me")
        self.assertNotContains(self.client.get(reverse("admin_tickets")), "Assigned")

    def test_admin_open_refundable_order(self):
        order = makeOrder(self.seller, makeProduct("Floor plan", "addon", 40, None))
        self.ticket.order = order
        self.ticket.save()
        response = self.client.get(reverse("admin_ticket", args=[self.ticket.pk]))
        self.assertContains(response, "Floor plan")
        self.assertContains(response, "Can be refunded")
        self.assertContains(response, reverse("admin_order", args=[order.pk]))
        self.client.force_login(makeStaff("admin@test.com"))
        response = self.client.get(reverse("admin_ticket", args=[self.ticket.pk]))
        self.assertContains(response, reverse("admin_order", args=[order.pk]))
        self.assertNotContains(response, "An Admin can refund it")

    def test_completed_order_shows(self):
        self.ticket.order = makeOrder(self.seller, makeProduct(), "completed")
        self.ticket.save()
        response = self.client.get(reverse("admin_ticket", args=[self.ticket.pk]))
        self.assertContains(response, "Completed")
        self.assertContains(response, "Can't be refunded")
        self.assertNotContains(response, "An Admin can refund it")


class ReviewTests(TestCase):

    def setUp(self):
        self.admin = makeStaff("admin@test.com")
        self.seller = makeUser("asdf@test.com", "seller", name="Tan")
        self.product = makeProduct("Floor plan", "addon", 40, None)
        item = makeOrder(self.seller, self.product, status="completed").items.get()
        self.review = Review.objects.create(seller=self.seller, orderItem=item, rating=2, comment="Slow")
        self.client.force_login(self.admin)

    def test_low_rating_filter_counts(self):
        response = self.client.get(reverse("admin_reviews"), {"rating": "low"})
        self.assertEqual(list(response.context["rows"]), [self.review])
        self.assertEqual(response.context["counts"], {"all": 1, "low": 1})
        self.assertContains(
            response,
            f'href="{reverse('admin_review', args=[self.review.pk])}?rating=low">View review <span',
        )
        self.assertNotContains(response, "Reason for hiding")
        page = self.client.get(reverse("admin_review", args=[self.review.pk]), {"rating": "low"})
        self.assertContains(page, "Reason for hiding")
        self.assertContains(page, '<input type="hidden" name="rating" value="low">')
        self.assertRedirects(
            self.client.get(reverse("admin_review", args=[self.review.pk + 99])),
            reverse("admin_reviews"),
        )

    def test_empty_low_rating_filter(self):
        self.review.rating = 5
        self.review.save(update_fields=["rating"])
        for query, message in [
            ({"rating": "low"}, "No low ratings."),
            ({"rating": "low", "days": "30"}, "No low ratings in the last 30 days."),
        ]:
            with self.subTest(query=query):
                response = self.client.get(reverse("admin_reviews"), query)
                self.assertEqual(response.context["counts"], {"all": 1, "low": 0})
                self.assertContains(response, message)
                self.assertNotContains(response, "No reviews yet.")

    def test_hidden_removed_product_ratings(self):
        url = reverse("admin_review_visibility", args=[self.review.pk])
        self.client.post(url, {"hide": "1", "reason": "Contains private contact details"})
        self.review.refresh_from_db()
        self.assertTrue(self.review.hidden)
        product = addRatings(Product.objects.all()).get()
        self.assertEqual(product.reviewCount, 0)
        self.client.post(url, {"hide": "0"})
        self.review.refresh_from_db()
        self.assertFalse(self.review.hidden)
        self.assertEqual(AuditLog.objects.filter(action__startswith="review").count(), 2)

    def test_public_packages_visible_ratings(self):
        product = addRatings(Product.objects.all())[0]
        self.assertEqual((product.reviewCount, product.averageRating), (1, 2))
        self.assertEqual(newReviews(), [self.review])
        self.assertEqual(self.client.get(reverse("packages")).status_code, 200)

    def test_admin_hide_restore(self):
        self.client.force_login(makeStaff("admin2@test.com", "Admin"))
        response = self.client.post(
            reverse("admin_review_visibility", args=[self.review.pk]),
            {"hide": "1", "reason": "Contains private contact details"},
        )
        self.assertEqual(response.status_code, 302)
        self.review.refresh_from_db()
        self.assertTrue(self.review.hidden)
        self.client.post(reverse("admin_review_visibility", args=[self.review.pk]), {"hide": "0"})
        self.review.refresh_from_db()
        self.assertFalse(self.review.hidden)
        self.assertEqual(
            AuditLog.objects.filter(actor__groups__name="Admin", action__startswith="review").count(),
            2,
        )

    def test_hide_requires_short_reason(self):
        url = reverse("admin_review_visibility", args=[self.review.pk])
        for reason in ("", "  ", "x" * 201):
            response = self.client.post(url, {"hide": "1", "reason": reason}, follow=True)
            self.assertContains(response, "Add a reason")
            self.review.refresh_from_db()
            self.assertFalse(self.review.hidden)
            self.assertFalse(AuditLog.objects.filter(action="review hidden").exists())
        response = self.client.get(reverse("admin_review", args=[self.review.pk]))
        self.assertContains(response, 'name="reason" maxlength="200" required')
        self.client.post(url, {"hide": "1", "reason": "  Private details  "})
        hidden = AuditLog.objects.get(action="review hidden")
        self.assertEqual(hidden.detail, "Private details")
        self.assertEqual(hidden.actor, self.admin)
        self.assertIsNotNone(hidden.createdAt)
        self.client.post(url, {"hide": "0"})
        shown = AuditLog.objects.get(action="review shown")
        self.assertEqual(shown.actor, self.admin)
        self.assertGreaterEqual(shown.createdAt, hidden.createdAt)
