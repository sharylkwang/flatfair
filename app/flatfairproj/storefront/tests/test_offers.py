from django.utils import timezone
from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from storefront.activity import buy_data, sell_data
from storefront.models import lock_user, ActivityNotice, Offer, Listing, OfferMessage
from .helpers import RoleMarkup
from .helpers import makeListing, makeStaff, makeUser
from storefront.offeradvice import SUGGEST_LIMIT
from storefront.tests.helpers import makeListing, makeUser


class OfferTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("asdf@test.com", "seller")
        cls.buyer = makeUser("qwer@test.com")
        cls.other = makeUser("zxcv@test.com")
        cls.flat = makeListing(cls.seller, askingPrice=600000)

    def act(self, user, kind, id, **data):
        self.client.force_login(user)
        return self.client.post(reverse("activity_action"), {"action": kind, "id": id, **data})

    def offer(self, buyer=None, amount=550000, status="pending"):
        return Offer.objects.create(
            listing=self.flat,
            buyer=buyer or self.buyer,
            amount=amount,
            status=status,
            respondedAt=timezone.now() if status != "pending" else None,
            counterAmount=580000 if status == "countered" else None,
        )

    def test_buyer_page_no_kpi(self):
        self.offer()
        self.client.force_login(self.buyer)
        markup = RoleMarkup(self.client.get(reverse("my_offers")).content.decode())
        self.assertEqual(markup.defaultKpis, 0)
        self.assertEqual(len(markup.moreSections), 0)
        self.offer(amount=540000, status="declined")
        content = self.client.get(reverse("my_offers")).content.decode()
        markup = RoleMarkup(content)
        self.assertEqual(len(markup.moreSections), 1)
        self.assertNotIn("open", markup.moreSections[0])
        before, more = content.split('id="buyer-more"', 1)
        self.assertNotIn("$540,000", before)
        self.assertIn("Past offers and viewings", more)
        self.assertIn("$540,000", more)
        self.assertIn('<span class="sbadge sbadge--grey">', more)

    def test_buyer_one_line_details(self):
        offer = self.offer()
        offer.reply = "Please get in touch."
        offer.save()
        self.client.force_login(self.buyer)
        content = self.client.get(reverse("my_offers")).content.decode()
        group = content.split('<article class="buyer-flat"', 1)[1].split("</article>", 1)[0]
        line = group.split(f'id="offer-{offer.pk}"', 1)[1].split("</li>", 1)[0]
        self.assertLess(group.index("<h3"), group.index("<img"))
        self.assertIn("$550,000", line)
        self.assertEqual(line.count('class="sbadge sbadge--warn"'), 1)
        self.assertIn("Sent", line)
        self.assertEqual(group.count("<a "), 1)
        self.assertNotIn("<details", group)
        self.assertNotIn("position-band", content)
        self.assertNotIn('value="withdraw"', content)
        flat = self.client.get(reverse("listing_detail", args=[self.flat.publicId])).content.decode()
        mine = flat.split('id="your-offer"', 1)[1].split('id="request-viewing"', 1)[0]
        for fact in (
            "$550,000",
            offer.reply,
            "Your offer compared with similar sales",
            'name="action" value="withdraw">Withdraw offer',
        ):
            self.assertIn(fact, mine)

    def test_flat_in_both_lists(self):
        self.offer()
        self.offer(amount=540000, status="declined")
        self.client.force_login(self.buyer)
        content = self.client.get(reverse("my_offers")).content.decode()
        self.assertEqual(content.count(f'id="buyer-flat-open-{self.flat.pk}"'), 1)
        self.assertEqual(content.count(f'id="buyer-flat-past-{self.flat.pk}"'), 1)
        ids = [part.split('"', 1)[0] for part in content.split(' id="')[1:]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_buyer_counter_line_names(self):
        offer = self.offer(status="countered")
        self.client.force_login(self.buyer)
        content = self.client.get(reverse("my_offers")).content.decode()
        line = content.split(f'id="offer-{offer.pk}"', 1)[1].split("</li>", 1)[0]
        self.assertIn("Seller countered", line)
        self.assertIn('value="accept_counter">Agree on $580,000</button>', line)
        self.assertIn('value="decline_counter">Close my offer', line)
        for word in (">Yes", ">No,", "Say no"):
            self.assertNotIn(word, content)

    def test_seller_row_shows_actions(self):
        offer = self.offer()
        offer.message = "Can we discuss the price?"
        offer.reply = "Happy to discuss."
        offer.save()
        self.client.force_login(self.seller)
        html = self.client.get(reverse("seller_viewings")).content.decode()
        row = html.split(f'<details class="s-item" id="offer-{offer.pk}" open>', 1)[1].split("</details>", 1)[
            0
        ]
        summary, panel = row.split("</summary>", 1)
        for fact in ("$550,000", "92% of asking", "Needs your reply", "Reply <span"):
            self.assertIn(fact, summary)
        self.assertNotIn("<form", summary)
        for fact in ("Asking $600,000", "Fair range $500,000–$600,000", offer.message, offer.reply):
            self.assertIn(fact, panel)
        for action in ["accept", "counter", "decline", "reply"]:
            self.assertEqual(panel.count(f'name="action" value="{action}"'), 1)
        self.assertNotIn("Review offer", html)
        self.assertNotIn("<summary>Details", html)
        offer.status = "declined"
        offer.message = ""
        offer.reply = ""
        offer.respondedAt = timezone.now()
        offer.save()
        html = self.client.get(reverse("seller_viewings")).content.decode()
        self.assertIn(f'<div class="s-item" id="offer-{offer.pk}">', html)
        self.assertNotIn(f'<details class="s-item" id="offer-{offer.pk}"', html)
        self.assertIn("Declined", html)
        self.assertNotIn('name="action" value="accept"', html)

    def test_countered_accepted_held_keep(self):
        offer = self.offer(status="countered")
        offer.counterAmount = 580000
        offer.save()
        self.client.force_login(self.seller)
        response = self.client.get(reverse("seller_viewings"))
        for action in ["counter", "decline"]:
            self.assertContains(response, f'name="action" value="{action}"')
        self.assertNotContains(response, 'name="action" value="accept"')
        offer.status = "accepted"
        offer.save()
        self.assertContains(self.client.get(reverse("seller_viewings")), 'name="action" value="reopen"')
        offer.status = "pending"
        offer.counterAmount = None
        offer.save()
        self.flat.status = "hidden"
        self.flat.save()
        response = self.client.get(reverse("seller_viewings"))
        self.assertContains(response, 'name="action" value="decline"')
        self.assertNotContains(response, 'name="action" value="accept"')
        self.assertNotContains(response, 'name="action" value="counter"')

    def test_closed_seller_history_starts(self):
        self.offer(status="declined")
        self.client.force_login(self.seller)
        response = self.client.get(reverse("seller_viewings"))
        self.assertContains(response, '<details class="offer-closed">')
        self.assertNotContains(response, '<details class="offer-closed" open>')

    def test_flat_page_lets_buyer(self):
        offer = self.offer(status="countered")
        page = reverse("listing_detail", args=[self.flat.publicId])
        self.client.force_login(self.buyer)
        response = self.client.get(page)
        for check_text_1 in (
            "Seller asks $580,000",
            "Agree on $580,000</button>",
            "Close my offer",
            "You offered $550,000.",
            'name="counterAmount" value="580000.00"',
            'name="action" value="accept_counter"',
            'name="action" value="decline_counter"',
        ):
            self.assertContains(response, check_text_1)
        self.act(self.buyer, "accept_counter", offer.pk, counterAmount="580000.00")
        offer.refresh_from_db()
        self.assertEqual(offer.status, "accepted")
        response = self.client.get(page)
        self.assertContains(response, '<span class="sbadge sbadge--good">')
        self.assertContains(response, "$580,000")
        self.assertContains(response, f'href="mailto:{self.seller.email}"')

    def test_flat_page_hides_counter(self):
        self.offer(status="countered")
        Offer.objects.create(
            listing=makeListing(makeUser("elsewhere@test.com", "seller")),
            buyer=self.buyer,
            amount=500000,
            status="accepted",
            respondedAt=timezone.now(),
        )
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        self.assertTrue(response.context["myOffer"].agreedElsewhere)
        self.assertNotContains(response, 'value="accept_counter"')
        self.assertContains(response, "You’ve already agreed a price on another flat.")
        self.assertContains(response, 'name="action" value="decline_counter"')

    def test_buyer_lists_show_ten(self):
        for number in range(11):
            flat = makeListing(makeUser(f"page-{number}@test.com", "seller"))
            Offer.objects.create(listing=flat, buyer=self.buyer, amount=500000)
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("my_offers"))
        self.assertEqual(len(response.context["offers"]), 10)
        self.assertEqual(len(response.context["buyerFlats"]), 10)
        self.assertContains(response, "Offers: Page 1 of 2")
        self.assertContains(response, "?offers_page=2#your-offers")

    def test_buyer_fields_validation_values(self):
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        self.assertContains(response, 'class="stack buyer-offer-form"')
        for name in ["amount", "message", "hasHfe"]:
            self.assertContains(response, f'name="{name}"')
        self.assertContains(response, 'name="action" value="offer"')
        self.assertContains(response, 'type="submit">Send offer</button>')
        for amount in ["582000", "570000", "600000"]:
            self.assertContains(response, f'data-offer-amount="{amount}"')
        response = self.act(
            self.buyer, "offer", self.flat.publicId, amount="100", message="Call me", hasHfe="yes"
        )
        for check_text_2 in (
            'value="100" required aria-invalid="true" aria-describedby="offer-error"',
            ">Call me</textarea>",
            'name="hasHfe" value="yes" checked',
            'id="offer-error" role="alert"',
        ):
            self.assertContains(response, check_text_2)
        response = self.act(
            self.buyer,
            "offer",
            self.flat.publicId,
            amount="560000",
            message="x" * 301,
            hasHfe="yes",
        )
        self.assertContains(response, 'aria-invalid="true" aria-describedby="offer-error">' + "x" * 301)

    def test_buyer_redirects_flat_notifies(self):
        response = self.act(self.buyer, "offer", self.flat.publicId, amount="560000", message="Keen to view")
        self.assertEqual(
            response.url,
            reverse("listing_detail", args=[self.flat.publicId]) + "#listing-action-card",
        )
        offer = Offer.objects.get()
        self.assertEqual((offer.amount, offer.status, offer.message), (560000, "pending", "Keen to view"))
        notice = ActivityNotice.objects.get(user=self.seller)
        self.assertEqual(notice.kind, "offer_new")
        self.assertIn("$560,000", notice.text)

    def test_validation_marks_amount_message(self):
        response = self.act(self.buyer, "offer", self.flat.publicId, amount="100")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["offerError"]["field"], "amount")
        response = self.act(self.buyer, "offer", self.flat.publicId, amount="560000", message="x" * 301)
        self.assertEqual(response.context["offerError"]["field"], "message")
        self.assertFalse(Offer.objects.exists())

    def test_buyer_blocks_create_duplicate(self):
        self.act(self.buyer, "offer", self.flat.publicId, amount="560000")
        self.act(self.buyer, "offer", self.flat.publicId, amount="570000")
        self.assertEqual(Offer.objects.count(), 1)

    def test_sold_flat_rejects_new_offer(self):
        self.flat.status = "sold"
        self.flat.save()
        self.act(self.buyer, "offer", self.flat.publicId, amount="560000")
        self.assertFalse(Offer.objects.exists())

    def test_seller_blocks_on_own(self):
        self.client.force_login(self.seller)
        for mode in ("buyer", "seller"):
            self.client.post(reverse("switch_mode"), {"mode": mode})
            response = self.act(self.seller, "offer", self.flat.publicId, amount="560000")
            self.assertEqual(response.status_code, 200)
            self.assertIn("own listing", response.context["offerError"]["text"])
            self.assertFalse(Offer.objects.exists())

    def test_staff_blocks_make_buyer(self):
        response = self.act(makeStaff("admin@test.com"), "offer", self.flat.publicId, amount="560000")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Offer.objects.exists())

    def test_accepting_declines_rivals(self):
        offer = self.offer()
        rival = self.offer(self.other, 540000)
        response = self.act(self.seller, "accept", offer.pk)
        self.assertEqual(response.url, reverse("seller_viewings") + f"#offer-{offer.pk}")
        offer.refresh_from_db()
        rival.refresh_from_db()
        self.assertEqual((offer.status, rival.status), ("accepted", "declined"))
        self.assertTrue(ActivityNotice.objects.filter(user=self.buyer, kind="offer_accepted").exists())
        self.assertTrue(ActivityNotice.objects.filter(user=self.other, kind="offer_declined").exists())

    def test_actions_require_correct_buyer(self):
        offer = self.offer()
        self.act(self.buyer, "accept", offer.pk)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "pending")
        self.act(self.seller, "withdraw", offer.pk)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "pending")

    def test_valid_counter_accepted_by(self):
        offer = self.offer()
        self.act(self.seller, "counter", offer.pk, amount="590000")
        offer.refresh_from_db()
        self.assertEqual((offer.status, offer.counterAmount), ("countered", 590000))
        self.act(self.seller, "counter", offer.pk, amount="700000")
        offer.refresh_from_db()
        self.assertEqual(offer.counterAmount, 590000)
        self.act(self.buyer, "accept_counter", offer.pk, counterAmount="590000")
        offer.refresh_from_db()
        self.assertEqual((offer.status, offer.amount, offer.counterAmount), ("accepted", 590000, None))
        self.assertTrue(ActivityNotice.objects.filter(user=self.seller, kind="offer_accepted").exists())

    def test_stale_counter_acceptance(self):
        offer = self.offer()
        self.act(self.seller, "counter", offer.pk, amount="590000")
        self.act(self.buyer, "accept_counter", offer.pk, counterAmount="580000")
        offer.refresh_from_db()
        self.assertEqual(offer.status, "countered")
        self.act(self.buyer, "decline_counter", offer.pk)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "withdrawn")

    def test_withdrawal_idempotent_notifies(self):
        offer = self.offer()
        self.act(self.buyer, "withdraw", offer.pk)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "withdrawn")
        self.act(self.buyer, "withdraw", offer.pk)
        self.assertEqual(ActivityNotice.objects.filter(kind="offer_withdrawn").count(), 1)

    def test_declined_blocks_accepted(self):
        offer = self.offer()
        self.act(self.seller, "decline", offer.pk)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "declined")
        self.act(self.seller, "accept", offer.pk)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "declined")

    def test_reopening_acceptance_not_allow(self):
        offer = self.offer()
        rival = self.offer(self.other, 540000)
        self.act(self.seller, "accept", offer.pk)
        self.act(self.seller, "reopen", offer.pk)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "pending")
        rival.status = "pending"
        rival.save()
        self.act(self.seller, "accept", rival.pk)
        self.act(self.seller, "accept", offer.pk)
        self.assertEqual(Offer.objects.filter(status="accepted").get(), rival)

    def test_seller_reply_cleans_whitespace(self):
        offer = self.offer()
        self.act(self.seller, "reply", offer.pk, reply="  Saturday   works ")
        offer.refresh_from_db()
        self.assertEqual(offer.reply, "Saturday works")
        self.assertTrue(ActivityNotice.objects.filter(user=self.buyer, kind="offer_reply").exists())
        self.act(self.seller, "reply", offer.pk, reply="   ")
        offer.refresh_from_db()
        self.assertEqual(offer.reply, "Saturday works")

    def test_unrelated_accounts_blocks(self):
        offer = self.offer()
        self.act(self.other, "withdraw", offer.pk)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "pending")
        stranger = makeUser("uiop@test.com", "seller")
        self.act(stranger, "accept", offer.pk)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "pending")

    def test_acceptance_locks_seller_invalid(self):
        offer = self.offer()
        with patch("storefront.activity.lock_user") as lock:
            self.act(self.seller, "accept", offer.pk)
        self.assertIn(self.seller.pk, [call.args[0] for call in lock.call_args_list])
        self.act(self.buyer, "withdraw", "asdf")
        offer.refresh_from_db()
        self.assertEqual(offer.status, "accepted")

    def test_buyer_creation_rate_limited(self):
        for i in range(5):
            flat = makeListing(makeUser(f"seller{i}@test.com", "seller"), askingPrice=600000)
            self.act(self.buyer, "offer", flat.publicId, amount="560000")
        self.act(self.buyer, "offer", self.flat.publicId, amount="560000")
        self.assertEqual(Offer.objects.count(), 5)

    def test_buyer_blocks_accept_counter(self):
        other = makeListing(makeUser("uiop@test.com", "seller"), block="9")
        Offer.objects.create(
            respondedAt=timezone.now(),
            listing=other,
            buyer=self.buyer,
            amount=500000,
            status="accepted",
        )
        offer = Offer.objects.create(
            respondedAt=timezone.now(),
            listing=self.flat,
            buyer=self.buyer,
            amount=550000,
            counterAmount=580000,
            status="countered",
        )
        response = self.act(self.buyer, "accept_counter", offer.pk, counterAmount="580000")
        offer.refresh_from_db()
        self.assertEqual(offer.status, "countered")
        self.assertContains(self.client.get(response.url), "You’ve already agreed a price on another flat.")

    def test_seller_acceptance_requires(self):
        other = makeListing(makeUser("uiop@test.com", "seller"), block="9")
        Offer.objects.create(
            respondedAt=timezone.now(),
            listing=other,
            buyer=self.buyer,
            amount=500000,
            status="accepted",
        )
        offer = self.offer()
        self.act(self.seller, "accept", offer.pk)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "pending")
        other.status = "sold"
        other.save()
        self.act(self.seller, "accept", offer.pk)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "accepted")

    def test_records_hfe_readiness_displays(self):
        self.act(self.buyer, "offer", self.flat.publicId, amount="560000", hasHfe="yes")
        self.act(self.other, "offer", self.flat.publicId, amount="555000")
        self.assertEqual(list(Offer.objects.order_by("pk").values_list("hasHfe", flat=True)), [True, False])
        self.client.force_login(self.seller)
        response = self.client.get(reverse("seller_viewings"))
        self.assertContains(response, "HFE letter: ready")
        self.assertContains(response, "HFE letter: not yet")

    def test_acceptance_locks_buyer_seller(self):
        offer = self.offer()
        with patch("storefront.activity.lock_user", wraps=lock_user) as lock:
            self.act(self.seller, "accept", offer.pk)
        self.assertEqual(lock.call_args_list[0].args, (self.buyer.pk,))
        self.assertEqual(lock.call_args_list[1].args, (self.seller.pk,))
        offer.refresh_from_db()
        self.assertEqual(offer.status, "accepted")

    def test_inactive_buyer_blocks_accepted(self):
        offer = self.offer()
        rival = self.offer(self.other)
        self.buyer.is_active = False
        self.buyer.save(update_fields=["is_active"])
        self.act(self.seller, "accept", offer.pk)
        offer.refresh_from_db()
        rival.refresh_from_db()
        self.assertEqual((offer.status, rival.status), ("pending", "pending"))
        self.assertFalse(ActivityNotice.objects.exists())

    def test_acceptance_rechecks_buyer(self):
        offer = self.offer()
        otherFlat = makeListing(makeUser("uiop@test.com", "seller"))

        def lockAccount(pk):
            if pk == self.buyer.pk:
                Offer.objects.create(
                    respondedAt=timezone.now(),
                    listing=otherFlat,
                    buyer=self.buyer,
                    amount=500000,
                    status="accepted",
                )
            return lock_user(pk)

        with patch("storefront.activity.lock_user", side_effect=lockAccount):
            self.act(self.seller, "accept", offer.pk)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "pending")
        self.assertFalse(ActivityNotice.objects.exists())


class ContextTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.seller = makeUser("asdf@test.com", "seller")
        cls.buyer = makeUser("qwer@test.com")
        cls.flat = makeListing(cls.seller)

    def test_buyer_separates_pending_past(self):
        Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=500000)
        Offer.objects.create(
            respondedAt=timezone.now(),
            listing=makeListing(self.seller, status="sold"),
            buyer=self.buyer,
            amount=500000,
            status="declined",
        )
        context = buy_data(self.buyer)
        self.assertEqual(context["pendingOffers"], 1)
        self.assertEqual(len(context["offers"]), 1)
        self.assertEqual(len(context["pastOffers"]), 1)

    def test_seller_counts_statuses(self):
        Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=500000)
        makeListing(self.seller, status="hidden")
        context = sell_data(self.seller)
        self.assertEqual(
            context["listingCounts"],
            {"live": 1, "review": 0, "paused": 0, "sold": 0, "hidden": 1, "rejected": 0},
        )
        self.assertEqual(context["openOffers"], 1)
        flat = [f for f in context["activityFlats"] if f.pk == self.flat.pk][0]
        self.assertEqual((flat.offerCount, flat.bestOffer), (1, 500000))
        self.assertEqual(len(sell_data(self.seller, {"status": "hidden"})["activityFlats"]), 1)

    def test_buyer_offers_page_paginates(self):
        self.client.force_login(self.buyer)
        Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=500000)
        response = self.client.get(reverse("my_offers"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["offerPage"].paginator.count, 1)

    def test_buyer_orders_countered_pending(self):
        waiting = Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=500000)
        accepted = Offer.objects.create(
            respondedAt=timezone.now(),
            listing=makeListing(makeUser("accepted@test.com", "seller")),
            buyer=self.buyer,
            amount=500000,
            status="accepted",
        )
        countered = Offer.objects.create(
            respondedAt=timezone.now(),
            listing=makeListing(makeUser("countered@test.com", "seller")),
            buyer=self.buyer,
            amount=500000,
            status="countered",
            counterAmount=540000,
        )
        offers = list(buy_data(self.buyer)["offers"])
        self.assertEqual(offers, [countered, waiting, accepted])

    def test_flat_page_shows_buyers(self):
        self.client.force_login(self.buyer)
        offer = Offer.objects.create(
            respondedAt=timezone.now(),
            listing=self.flat,
            buyer=self.buyer,
            amount=500000,
            status="countered",
            counterAmount=540000,
        )
        response = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        self.assertEqual(response.context["myOffer"], offer)
        self.flat.status = "review"
        self.flat.save()
        self.client.force_login(makeUser("uiop@test.com"))
        self.assertRedirects(
            self.client.get(reverse("listing_detail", args=[self.flat.publicId])), reverse("home")
        )

    def test_contact_email_revealed_offer(self):
        Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=500000)
        self.client.force_login(self.buyer)
        self.assertNotContains(self.client.get(reverse("my_offers")), "mailto:asdf@test.com")
        self.client.force_login(self.seller)
        self.assertNotContains(self.client.get(reverse("seller_viewings")), "mailto:qwer@test.com")
        Offer.objects.update(status="accepted", respondedAt=timezone.now())
        self.client.force_login(self.buyer)
        response = self.client.get(reverse("my_offers"))
        self.assertContains(response, "mailto:asdf@test.com")
        flatPage = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        self.assertContains(flatPage, "mailto:asdf@test.com")
        self.assertContains(flatPage, "Have your HFE letter ready.")
        self.client.force_login(self.seller)
        self.assertContains(self.client.get(reverse("seller_viewings")), "mailto:qwer@test.com")

    def test_sold_listing_acceptance_blocks(self):
        offer = Offer.objects.create(
            respondedAt=timezone.now(),
            listing=self.flat,
            buyer=self.buyer,
            amount=500000,
            status="accepted",
        )
        self.flat.status = "sold"
        self.flat.save()
        self.client.force_login(self.seller)
        self.client.post(reverse("activity_action"), {"action": "reopen", "id": offer.pk})
        offer.refresh_from_db()
        self.assertEqual(offer.status, "accepted")


class OfferRuleTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.buyer = makeUser("qwer@test.com")
        self.flat = makeListing(self.seller, askingPrice=600000)
        self.offer = Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=550000)

    def act(self, user, kind, id, **data):
        self.client.force_login(user)
        return self.client.post(reverse("activity_action"), {"action": kind, "id": id, **data}, follow=True)

    def text(self, response):
        return " ".join((str(m) for m in response.context["messages"]))

    def test_buyer_withdraw_seller(self):
        self.seller.is_active = False
        self.seller.save()
        response = self.act(self.buyer, "withdraw", self.offer.pk)
        self.assertIn("Your offer has been withdrawn.", self.text(response))
        flat = makeListing(makeUser("zxcv@test.com", "seller"))
        flat.seller.is_active = False
        flat.seller.save()
        self.act(self.buyer, "offer", flat.publicId, amount="500000")
        self.assertEqual(Offer.objects.filter(listing=flat).count(), 0)

    def test_seller_blocks_counter_another(self):
        Offer.objects.create(
            respondedAt=timezone.now(),
            listing=self.flat,
            buyer=makeUser("zxcv@test.com"),
            amount=560000,
            status="accepted",
        )
        response = self.act(self.seller, "counter", self.offer.pk, amount="590000")
        self.assertIn("You’ve already accepted another offer", self.text(response))
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.status, "pending")

    def test_closed_blocks_replies_counters(self):
        self.offer.status = "withdrawn"
        self.offer.respondedAt = timezone.now()
        self.offer.save()
        response = self.act(self.seller, "reply", self.offer.pk, reply="hello")
        self.assertIn("This offer is closed", self.text(response))
        response = self.act(self.seller, "counter", self.offer.pk, amount="590000")
        self.assertIn("no longer open", self.text(response))

    def test_counter_requires_higher_valid(self):
        response = self.act(self.seller, "counter", self.offer.pk, amount="550000")
        self.assertIn("more than the buyer offered", self.text(response))
        response = self.act(self.seller, "counter", self.offer.pk, amount="asdf")
        self.assertIn("whole-dollar amount", self.text(response))

    def test_unknown_action_non_post(self):
        response = self.act(self.buyer, "asdf", self.offer.pk)
        self.assertEqual(response.status_code, 403)
        self.client.force_login(self.buyer)
        self.assertEqual(self.client.get(reverse("activity_action")).status_code, 405)


class CounterBackTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.buyer = makeUser("qwer@test.com")
        self.flat = makeListing(self.seller, askingPrice=600000)
        self.offer = Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=500000)

    def act(self, user, kind, **data):
        self.client.force_login(user)
        return self.client.post(
            reverse("activity_action"), {"action": kind, "id": self.offer.pk, **data}, follow=True
        )

    def counter(self, amount):
        return self.act(self.seller, "counter", amount=amount)

    def suggest(self, amount):
        return self.act(self.buyer, "suggest", amount=amount)

    def test_buyer_answer_price_in(self):
        self.counter(520000)
        response = self.suggest(510000)
        self.assertContains(response, "We’ve sent your price of $510,000 to the seller.")
        self.offer.refresh_from_db()
        self.assertEqual(
            (self.offer.status, self.offer.amount, self.offer.counterAmount),
            ("pending", 510000, None),
        )
        self.assertEqual(
            self.offer.priceSteps,
            [
                {"by": "buyer", "amount": 500000},
                {"by": "seller", "amount": 520000},
                {"by": "buyer", "amount": 510000},
            ],
        )
        notice = ActivityNotice.objects.get(user=self.seller, kind="offer_suggested")
        self.assertIn("The buyer suggested $510,000 for Blk 123 Yishun", notice.text)
        self.assertEqual(notice.url, reverse("seller_viewings") + f"#offer-{self.offer.pk}")

    def test_price_must_sit_between(self):
        self.counter(520000)
        for amount in (500000, 520000, 530000):
            response = self.suggest(amount)
            self.assertContains(
                response,
                "Please suggest a price above your $500,000 offer and below the seller’s $520,000.",
            )
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.status, "countered")

    def test_buyer_blocks_suggest_seller(self):
        response = self.suggest(510000)
        self.assertContains(
            response, "You can only suggest a price after the seller has replied with theirs."
        )
        self.assertFalse(ActivityNotice.objects.filter(kind="offer_suggested").exists())

    def test_both_sides_see_history(self):
        self.counter(520000)
        self.suggest(510000)
        self.client.force_login(self.seller)
        page = self.client.get(reverse("seller_viewings"))
        self.assertContains(page, "Buyer offered $500,000 · You asked $520,000 · Buyer suggested $510,000")
        self.client.force_login(self.buyer)
        page = self.client.get(reverse("my_offers"))
        self.assertContains(page, "You offered $500,000 · Seller asked $520,000 · You suggested $510,000")

    def test_seller_accept_suggested_price(self):
        self.counter(520000)
        self.suggest(510000)
        self.act(self.seller, "accept")
        self.offer.refresh_from_db()
        self.assertEqual((self.offer.status, self.offer.amount), ("accepted", 510000))
        notice = ActivityNotice.objects.get(user=self.buyer, kind="offer_accepted")
        self.assertIn("$510,000", notice.text)

    def test_seller_again_but_not(self):
        self.counter(520000)
        self.suggest(510000)
        response = self.counter(525000)
        self.assertContains(response, "Your new price can’t be more than the $520,000 you asked before.")
        self.counter(515000)
        self.offer.refresh_from_db()
        self.assertEqual((self.offer.status, self.offer.counterAmount), ("countered", 515000))
        self.assertEqual(self.offer.priceSteps[-1], {"by": "seller", "amount": 515000})
        self.assertEqual(ActivityNotice.objects.filter(user=self.buyer, kind="offer_countered").count(), 2)

    def test_buyer_suggest_few_times(self):
        self.counter(560000)
        for number in range(SUGGEST_LIMIT):
            self.suggest(510000 + number * 10000)
            self.counter(559000 - number * 1000)
        response = self.suggest(545000)
        self.assertContains(response, f"You’ve already suggested {SUGGEST_LIMIT} prices on this offer.")
        page = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        self.assertContains(
            page, f"You’ve suggested {SUGGEST_LIMIT} prices, which is the most for one offer."
        )
        self.assertNotContains(page, "Suggest another price")

    def test_flat_page_offers_suggest(self):
        self.counter(520000)
        self.client.force_login(self.buyer)
        page = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        self.assertContains(page, "Suggest another price")
        self.assertContains(page, 'min="500001" max="519999"')
        self.assertContains(page, f"You can suggest up to {SUGGEST_LIMIT} prices on this offer.")

    def test_paused_flat_takes_no(self):
        self.counter(520000)
        Listing.objects.filter(pk=self.flat.pk).update(status="paused", pausedAt=timezone.now())
        response = self.suggest(510000)
        self.assertContains(response, "The seller has paused this flat for now.")
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.status, "countered")


class OfferThreadTests(TestCase):

    def setUp(self):
        self.seller = makeUser("thread-seller@test.com", "seller")
        self.buyer = makeUser("thread-buyer@test.com", "buyer")
        self.flat = makeListing(self.seller)
        self.offer = Offer.objects.create(listing=self.flat, buyer=self.buyer, amount=540000)

    def act(self, user, kind, **data):
        self.client.force_login(user)
        return self.client.post(
            reverse("activity_action"), {"action": kind, "id": self.offer.pk, **data}, follow=True
        )

    def test_buyer_answers_seller_on(self):
        self.act(self.seller, "reply", reply="Could you move in after March?")
        self.client.force_login(self.buyer)
        page = self.client.get(reverse("my_offers")).content.decode()
        self.assertIn("Could you move in after March?", page)
        self.assertIn("Reply to the seller", page)
        response = self.act(self.buyer, "buyer_reply", reply="Yes, April works for us.")
        self.assertContains(response, "Reply sent to the seller.")
        self.assertEqual(list(OfferMessage.objects.values_list("by", flat=True)), ["seller", "buyer"])
        notice = ActivityNotice.objects.get(user=self.seller, kind="offer_buyer_reply")
        self.assertIn("The buyer replied about their offer of $540,000", notice.text)
        self.client.force_login(self.seller)
        self.assertContains(self.client.get(reverse("seller_viewings")), "Yes, April works for us.")

    def test_closed_empty_replies_refused(self):
        response = self.act(self.buyer, "buyer_reply", reply="   ")
        self.assertContains(response, "Write a reply before sending.")
        Offer.objects.filter(pk=self.offer.pk).update(status="withdrawn", respondedAt=timezone.now())
        response = self.act(self.buyer, "buyer_reply", reply="Hello")
        self.assertContains(response, "This offer is closed")
        self.assertFalse(OfferMessage.objects.exists())

    def test_another_buyer_cannot_reply(self):
        other = makeUser("thread-other@test.com", "buyer")
        self.client.force_login(other)
        self.client.post(
            reverse("activity_action"),
            {"action": "buyer_reply", "id": self.offer.pk, "reply": "Hi"},
        )
        self.assertFalse(OfferMessage.objects.exists())
