from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.core import mail
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .helpers import makeListing, makeUser, makeStaff
from storefront.models import Collection, SavedListing, ActivityNotice, SavedSearch, SearchAlert
from storefront.search import tidySearch


class SavedTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.buyer = makeUser("qwer@test.com")
        self.flat = makeListing(self.seller)
        self.client.force_login(self.buyer)
        self.save = reverse("toggle_save", args=[self.flat.publicId])

    def test_two_flats_get_their(self):
        collection = Collection.objects.create(user=self.buyer, name="Near MRT")
        second = makeListing(makeUser("other@test.com", "seller"))
        for flat in (self.flat, second):
            SavedListing.objects.create(collection=collection, listing=flat)
        response = self.client.get(reverse("saved"))
        content = response.content.decode()
        self.assertNotContains(response, 'href="#saved-comparison"')
        rows, compare = content.split('<section id="saved-comparison"', 1)
        compare, searches = compare.split('id="saved-searches"', 1)
        self.assertEqual(rows.count('class="buyer-flat saved-row"'), 2)
        self.assertIn("<h2>Compare these flats</h2>", compare)
        self.assertIn("Near MRT · 2 flats side by side.", compare)
        self.assertIn('<h2 id="saved-searches-title">Saved searches</h2>', searches)
        self.assertNotIn('<details class="fold', compare)

    def test_comparison_names_fair_price(self):
        collection = Collection.objects.create(user=self.buyer, name="Near MRT")
        flats = [
            self.flat,
            makeListing(makeUser("high@test.com", "seller"), askingPrice=700000),
            makeListing(makeUser("low@test.com", "seller"), askingPrice=400000),
            makeListing(
                makeUser("none@test.com", "seller"),
                estimatedPrice=None,
                bandLow=None,
                bandHigh=None,
            ),
            makeListing(makeUser("sold@test.com", "seller"), status="sold", statusBeforeSold="live"),
        ]
        for flat in flats:
            SavedListing.objects.create(collection=collection, listing=flat)
        cards = self.client.get(reverse("saved")).content.decode().split('class="cmp-cards"', 1)[1]
        for label in ["Within range", "Above range", "Below range", "No estimate", "Sold"]:
            self.assertIn(f"<dt>Fair price</dt><dd>{label}</dd>", cards)
        self.assertNotIn("<dt>Fair price</dt><dd>No</dd>", cards)

    def test_single_flat_not_show(self):
        collection = Collection.objects.create(user=self.buyer, name="Near MRT")
        SavedListing.objects.create(collection=collection, listing=self.flat)
        self.assertNotContains(self.client.get(reverse("saved")), 'id="saved-comparison"')

    def test_sold_flats_start_collapsed(self):
        collection = Collection.objects.create(user=self.buyer, name="Near MRT")
        SavedListing.objects.create(collection=collection, listing=self.flat)
        self.flat.status = "sold"
        self.flat.statusBeforeSold = "live"
        self.flat.save()
        response = self.client.get(reverse("saved"))
        self.assertEqual(response.context["activeSaved"], [])
        self.assertEqual(response.context["soldSaved"], [self.flat])
        content = response.content.decode()
        section = content.split('<details class="fold" id="saved-sold">', 1)[1].split("</details>", 1)[0]
        self.assertIn(reverse("listing_detail", args=[self.flat.publicId]), section)
        self.assertIn('Sold flats <span class="fold-hint">1</span>', section)
        self.assertIn('<span class="sbadge sbadge--final">', section)
        self.assertNotContains(response, 'id="saved-comparison"')
        self.assertContains(response, "$550,000")
        other = makeListing(makeUser("live@test.com", "seller"))
        SavedListing.objects.create(collection=collection, listing=other)
        compare = (
            self.client.get(reverse("saved")).content.decode().split('<section id="saved-comparison"', 1)[1]
        )
        self.assertIn(reverse("listing_detail", args=[self.flat.publicId]), compare)
        self.assertIn("· sold", compare)

    def test_row_facts_one_badge(self):
        collection = Collection.objects.create(user=self.buyer, name="Near MRT")
        SavedListing.objects.create(collection=collection, listing=self.flat)
        content = self.client.get(reverse("saved")).content.decode()
        row = content.split('class="buyer-flat saved-row"', 1)[1].split("</article>", 1)[0]
        for fact in (
            "$550,000",
            "8th floor",
            "92 sqm",
            'class="sbadge sbadge--good"',
            ">View flat</a>",
        ):
            self.assertIn(fact, row)
        self.assertEqual(row.count('class="sbadge sbadge--'), 1)
        self.assertEqual(row.count("<a "), 1)
        self.assertNotIn("<details", row)
        self.assertLess(row.index("<h3>"), row.index("<img"))
        self.assertIn(reverse("toggle_save", args=[self.flat.publicId]), row)
        self.assertIn(f'name="collection" value="{collection.pk}"', row)
        self.assertIn('aria-pressed="true"', row)
        self.assertIn("Edit list</summary>", content)
        self.assertEqual(content.count("Delete this list"), 1)

    def test_heart_asks_which_list(self):
        page = self.client.get(reverse("listings")).content.decode()
        self.assertIn('aria-label="Save this flat"', page)
        menu = page.split('class="save-pop"', 1)[1].split("</details>", 1)[0]
        self.assertIn("Save to which list?", menu)
        self.assertIn('name="newName" value="My shortlist"', menu)
        self.assertIn('placeholder="New list name"', menu)
        self.assertNotIn("Remove from saved", menu)
        self.assertNotIn('class="save-quick"', page)
        self.assertFalse(SavedListing.objects.exists())
        response = self.client.post(self.save, {"newName": "My shortlist"}, follow=True)
        self.assertEqual(SavedListing.objects.get().collection.name, "My shortlist")
        self.assertContains(response, "Saved to My shortlist.")
        page = self.client.get(reverse("listings")).content.decode()
        self.assertIn('data-saved="true" aria-label="Saved. Change lists"', page)
        self.assertIn("Saved in", page)
        self.assertIn("Remove from saved", page)

    def test_remove_clears_list_never(self):
        first = Collection.objects.create(user=self.buyer, name="Near MRT")
        second = Collection.objects.create(user=self.buyer, name="Room to grow")
        for collection in (first, second):
            SavedListing.objects.create(collection=collection, listing=self.flat)
        response = self.client.post(self.save, {"remove": "1"}, follow=True)
        self.assertContains(response, "Removed from your saved flats.")
        self.assertFalse(SavedListing.objects.exists())
        self.client.post(self.save, {"remove": "1"})
        self.assertFalse(SavedListing.objects.exists())
        self.assertEqual(Collection.objects.count(), 2)

    def test_first_tap_general_list(self):
        Collection.objects.create(user=self.buyer, name="Near MRT")
        second = Collection.objects.create(user=self.buyer, name="Room to grow")
        self.client.post(self.save)
        self.assertEqual(SavedListing.objects.get().collection.name, "My shortlist")
        self.client.post(self.save, {"collection": second.pk})
        self.assertEqual(SavedListing.objects.count(), 2)
        self.client.post(self.save)
        self.assertFalse(SavedListing.objects.exists())
        self.assertEqual(Collection.objects.count(), 3)
        self.client.post(self.save)
        self.assertEqual(Collection.objects.filter(name="My shortlist").count(), 1)

    def test_lists_answer_scripts_json(self):
        reply = self.client.post(
            self.save,
            {"newName": "Near MRT", "next": "/listings#save-x", "big": "1", "scope": "home-saved"},
            HTTP_ACCEPT="application/json",
        ).json()
        self.assertEqual(
            (reply["saved"], reply["count"], reply["message"], reply["failed"]),
            (True, 1, "Saved to Near MRT.", False),
        )
        menu = reply["menu"]
        self.assertIn('<details class="save-menu big" open>', menu)
        self.assertIn(f'id="save-{self.flat.publicId}-home-saved"', menu)
        self.assertIn('data-big="1" data-scope="home-saved"', menu)
        self.assertIn('value="/listings#save-', menu)
        self.assertIn('<span class="nm">Near MRT</span>', menu)
        self.assertIn('class="save-opt in" aria-pressed="true"', menu)
        self.assertNotContains(self.client.get(reverse("saved")), "Saved to Near MRT.")
        reply = self.client.post(self.save, {"remove": "1"}, HTTP_ACCEPT="application/json").json()
        self.assertEqual(
            (reply["saved"], reply["count"], reply["message"]),
            (False, 0, "Removed from your saved flats."),
        )
        self.assertIn("Save to which list?", reply["menu"])
        fresh = self.client.get(
            self.save, {"next": "//evil.example/"}, HTTP_ACCEPT="application/json"
        ).json()["menu"]
        self.assertIn(f'value="{reverse('listing_detail', args=[self.flat.publicId])}#save-', fresh)
        self.assertNotIn("evil.example", fresh)
        self.flat.status = "sold"
        self.flat.statusBeforeSold = "live"
        self.flat.save()
        collection = Collection.objects.get()
        reply = self.client.post(
            self.save, {"collection": collection.pk}, HTTP_ACCEPT="application/json"
        ).json()
        self.assertEqual((reply["saved"], reply["failed"]), (False, True))
        self.assertRedirects(self.client.post(self.save, {"next": "/saved"}), "/saved")

    def test_arriving_save_intent_opens(self):
        page = reverse("listing_detail", args=[self.flat.publicId])
        anchor = f"{page}#save-{self.flat.publicId}"
        response = self.client.get(page, {"save": "1"})
        self.assertRedirects(response, anchor, fetch_redirect_response=False)
        self.assertFalse(SavedListing.objects.exists())
        response = self.client.get(page)
        self.assertContains(response, '<details class="save-menu big" open>')
        self.assertContains(response, "Save to which list?")
        self.assertNotContains(self.client.get(page), '<details class="save-menu big" open>')
        SavedListing.objects.create(
            collection=Collection.objects.create(user=self.buyer, name="Near MRT"),
            listing=self.flat,
        )
        self.client.get(page, {"save": "1"})
        self.assertNotContains(self.client.get(page), '<details class="save-menu big" open>')
        self.client.logout()
        guest = self.client.get(page, {"save": "1"})
        self.assertEqual(guest.status_code, 200)
        self.assertNotContains(guest, "Save to…")
        signin = self.client.get(reverse("signin"), {"next": page + "?save=1"})
        self.assertContains(signin, "role=buyer")
        self.assertContains(
            self.client.get(reverse("register"), {"role": "buyer"}),
            '<input type="radio" name="role" value="buyer" id="id_role_0" required checked>',
            html=True,
        )

    def test_picker_moves_flat_another(self):
        self.client.post(self.save)
        shortlist = Collection.objects.get()
        other = Collection.objects.create(user=self.buyer, name="Near MRT")
        picker = (
            self.client.get(reverse("listings"))
            .content.decode()
            .split('class="save-pop"', 1)[1]
            .split("</details>", 1)[0]
        )
        self.assertIn("Saved in", picker)
        self.assertIn(f'name="collection" value="{other.pk}"', picker)
        self.client.post(self.save, {"collection": other.pk})
        self.client.post(self.save, {"collection": shortlist.pk})
        self.assertEqual([item.collection for item in SavedListing.objects.all()], [other])
        self.client.post(self.save, {"collection": "999999"})
        self.client.post(self.save, {"newName": "  "})
        self.assertEqual(SavedListing.objects.count(), 1)
        self.assertEqual(Collection.objects.count(), 2)

    def test_empty_page_shows_one(self):
        response = self.client.get(reverse("saved"))
        self.assertContains(response, 'class="empty-icon"', count=1)
        self.assertNotContains(response, "storefront/art/")
        self.assertContains(response, "<h3>No saved flats yet</h3>", html=True)
        self.assertContains(
            response,
            f'<a class="btn btn-brand" href="{reverse('listings')}">Browse flats</a>',
            html=True,
        )
        Collection.objects.create(user=self.buyer, name="Near MRT")
        self.assertContains(self.client.get(reverse("saved")), "<h3>This list is empty</h3>", html=True)

    def test_saving_flat_creates_normalized(self):
        self.client.post(self.save, {"newName": "  Near   MRT "})
        collection = Collection.objects.get()
        self.assertEqual(collection.name, "Near MRT")
        self.assertTrue(SavedListing.objects.filter(collection=collection, listing=self.flat).exists())
        response = self.client.get(reverse("saved"))
        self.assertEqual(response.context["current"], collection)
        self.assertEqual([flat.pk for flat in response.context["listings"]], [self.flat.pk])
        self.assertEqual(response.context["savedIds"], {self.flat.pk})

    def test_saving_again_toggles_flat(self):
        collection = Collection.objects.create(user=self.buyer, name="asdf")
        self.client.post(self.save, {"collection": collection.pk})
        self.assertEqual(SavedListing.objects.count(), 1)
        self.client.post(self.save, {"collection": collection.pk})
        self.assertEqual(SavedListing.objects.count(), 0)

    def test_collection_creation_case(self):
        self.client.post(self.save, {"newName": "asdf"})
        self.client.post(self.save, {"newName": "ASDF"})
        self.assertEqual(Collection.objects.count(), 1)
        self.assertEqual(SavedListing.objects.count(), 1)

    def test_sold_flat_blocks_added(self):
        self.flat.status = "sold"
        self.flat.save()
        self.client.post(self.save, {"newName": "asdf"})
        self.assertEqual(SavedListing.objects.count(), 0)
        other = Collection.objects.create(user=self.seller, name="zxcv")
        self.client.post(self.save, {"collection": other.pk})
        self.assertEqual(SavedListing.objects.count(), 0)

    def test_collection_rename_blocks_case(self):
        self.client.post(reverse("collection_create"), {"name": "Room to grow"})
        collection = Collection.objects.get()
        self.client.post(reverse("collection_rename", args=[collection.pk]), {"name": "Bigger"})
        collection.refresh_from_db()
        self.assertEqual(collection.name, "Bigger")
        Collection.objects.create(user=self.buyer, name="Other")
        self.client.post(reverse("collection_rename", args=[collection.pk]), {"name": "other"})
        collection.refresh_from_db()
        self.assertEqual(collection.name, "Bigger")

    def test_collection_deletion_requires(self):
        collection = Collection.objects.create(user=self.buyer, name="asdf")
        SavedListing.objects.create(collection=collection, listing=self.flat)
        self.client.get(reverse("collection_delete", args=[collection.pk]))
        self.assertTrue(Collection.objects.exists())
        self.client.post(reverse("collection_delete", args=[collection.pk]))
        self.assertFalse(Collection.objects.exists())
        self.flat.refresh_from_db()
        self.assertEqual(self.flat.status, "live")

    def test_hidden_flats_excluded_page(self):
        collection = Collection.objects.create(user=self.buyer, name="asdf")
        SavedListing.objects.create(collection=collection, listing=self.flat)
        self.flat.status = "hidden"
        self.flat.save()
        self.assertEqual(list(self.client.get(reverse("saved")).context["listings"]), [])

    def test_seller_blocks_save_own(self):
        self.client.force_login(self.seller)
        for mode in ("buyer", "seller"):
            self.client.post(reverse("switch_mode"), {"mode": mode})
            response = self.client.post(self.save, {"newName": "asdf"}, follow=True)
            self.assertContains(response, "You can’t save your own flat.")
            self.assertFalse(Collection.objects.exists())
            collection = Collection.objects.create(user=self.seller, name="Existing")
            self.client.post(self.save, {"collection": collection.pk})
            self.assertFalse(SavedListing.objects.exists())
            collection.delete()

    def test_seller_create_collections_save(self):
        otherFlat = makeListing(makeUser("other@test.com", "seller"))
        self.client.force_login(self.seller)
        self.client.post(reverse("switch_mode"), {"mode": "buyer"})
        self.assertRedirects(
            self.client.post(reverse("collection_create"), {"name": "Next home"}),
            reverse("saved") + f"?c={Collection.objects.get().pk}",
        )
        collection = Collection.objects.get()
        self.client.post(reverse("toggle_save", args=[otherFlat.publicId]), {"collection": collection.pk})
        self.assertEqual(SavedListing.objects.get().listing, otherFlat)
        self.assertContains(self.client.get(reverse("saved")), "Next home")

    def test_action_redirect_allows_local(self):
        response = self.client.post(self.save, {"newName": "asdf", "next": "//evil.example/"})
        self.assertRedirects(response, reverse("saved"))
        response = self.client.post(self.save, {"collection": "1", "next": "/listings"})
        self.assertEqual(response.url, "/listings")

    def test_page_selects_first_requested(self):
        first = Collection.objects.create(user=self.buyer, name="asdf")
        second = Collection.objects.create(user=self.buyer, name="qwer")
        SavedListing.objects.create(collection=second, listing=self.flat)
        self.assertEqual(self.client.get(reverse("saved")).context["current"], first)
        response = self.client.get(reverse("saved"), {"c": second.pk})
        self.assertEqual(response.context["current"], second)
        self.assertEqual(len(response.context["listings"]), 1)


class SavedSearchTests(TestCase):

    def setUp(self):
        self.buyer = makeUser("buyer@test.com")
        self.client.force_login(self.buyer)
        self.url = reverse("save_search")

    def saveSearch(self, **params):
        return self.client.post(self.url, params or {"town": "TAMPINES"})

    def test_save_cleans_supported_filters(self):
        self.saveSearch(
            town=" tampines ",
            flatType="4 room",
            maxPrice="0600000",
            q="  123  ",
            minSqm="90",
            maxSqm="120",
            bedrooms="3",
            bathrooms="2",
            minLease="50",
            balcony="1",
            sort="price_asc",
            page="2",
            all="1",
            unknown="ignored",
        )
        savedSearch = SavedSearch.objects.get()
        self.assertEqual(
            savedSearch.queryParams,
            {
                "q": "123",
                "town": "TAMPINES",
                "flatType": "4 ROOM",
                "maxPrice": "600000",
                "minSqm": "90.0",
                "maxSqm": "120.0",
                "bedrooms": "3",
                "bathrooms": "2",
                "minLease": "50",
                "balcony": "1",
            },
        )
        self.assertIn("4-room in Tampines", savedSearch.name)
        self.assertIn("under $600k", savedSearch.name)
        self.assertTrue(savedSearch.isActive)
        self.assertIsNone(savedSearch.lastNotifiedAt)
        self.assertEqual(tidySearch(savedSearch.queryParams), savedSearch.queryParams)
        self.assertEqual(
            parse_qs(urlsplit(savedSearch.searchUrl).query),
            {key: [value] for key, value in savedSearch.queryParams.items()},
        )
        self.assertContains(self.client.get(reverse("saved")), savedSearch.name)

    def test_region_named(self):
        from storefront.search import searchName

        self.assertEqual(searchName({"region": "North-East"}), "Flats in the North-East")
        self.assertEqual(searchName({"region": "North-East", "town": "TAMPINES"}), "Flats in Tampines")

    def test_model_cleans_filters_plain(self):
        savedSearch = SavedSearch.objects.create(
            buyer=self.buyer,
            name="ignored",
            queryParams={
                "town": "tampines",
                "flatType": "4 ROOM",
                "maxPrice": 600000,
                "unknown": "ignored",
            },
        )
        self.assertEqual(savedSearch.name, "4-room in Tampines under $600k")
        self.assertNotIn("unknown", savedSearch.queryParams)

    def test_save_typed_town_results(self):
        response = self.client.get(reverse("listings"), {"q": "tampines"})
        self.assertContains(response, "Save this search")
        self.assertEqual(response.context["searchParams"], {"town": "TAMPINES"})
        self.client.post(self.url, response.context["searchParams"])
        savedSearch = SavedSearch.objects.get()
        self.assertEqual(savedSearch.queryParams, {"town": "TAMPINES"})
        self.assertEqual(savedSearch.name, "Flats in Tampines")

    def test_invalid_unknown_filters_blocks(self):
        for params in (
            {},
            {"unknown": "value"},
            {
                "town": "Atlantis",
                "flatType": "castle",
                "maxPrice": "NaN",
                "minSqm": "401",
                "bedrooms": "11",
                "balcony": "0",
            },
        ):
            self.client.post(self.url, params)
        self.assertFalse(SavedSearch.objects.exists())

    def test_save_limit_five_including(self):
        for block in range(5):
            self.saveSearch(q=str(block))
        first = SavedSearch.objects.first()
        self.client.post(reverse("change_search", args=[first.pk]), {"action": "pause"})
        response = self.saveSearch(town="BEDOK")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SavedSearch.objects.count(), 5)
        with self.assertRaises(ValidationError):
            SavedSearch.objects.create(buyer=self.buyer, queryParams={"town": "BEDOK"})
        self.client.post(reverse("change_search", args=[first.pk]), {"action": "delete"})
        self.saveSearch(town="BEDOK")
        self.assertEqual(SavedSearch.objects.count(), 5)

    def test_saving_same_normalised_keeps(self):
        self.saveSearch(town="tampines", maxPrice="0600000")
        self.saveSearch(town="TAMPINES", maxPrice="600000", page="2")
        self.assertEqual(SavedSearch.objects.count(), 1)

    def test_pause_resume_delete_require(self):
        self.saveSearch()
        savedSearch = SavedSearch.objects.get()
        url = reverse("change_search", args=[savedSearch.pk])
        self.assertEqual(self.client.get(url, {"action": "delete"}).status_code, 405)
        for action, active in (("pause", False), ("resume", True)):
            self.client.post(url, {"action": action})
            savedSearch.refresh_from_db()
            self.assertEqual(savedSearch.isActive, active)
            self.assertContains(self.client.get(reverse("saved")), "Pause" if active else "Resume")
        other = makeUser("other@test.com")
        self.client.force_login(other)
        for action in ("pause", "resume", "delete"):
            self.assertEqual(self.client.post(url, {"action": action}).status_code, 404)
        self.client.force_login(self.buyer)
        self.client.post(url, {"action": "delete"})
        self.assertFalse(SavedSearch.objects.exists())

    def test_buying_accounts_including(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        seller = makeUser("seller@test.com", "seller")
        self.client.force_login(seller)
        self.client.post(reverse("switch_mode"), {"mode": "buyer"})
        self.assertEqual(self.saveSearch().status_code, 302)
        self.assertEqual(SavedSearch.objects.get().buyer, seller)
        self.client.force_login(makeStaff("staff@test.com"))
        self.assertEqual(self.saveSearch().status_code, 403)
        self.client.logout()
        self.assertEqual(self.saveSearch().status_code, 302)
        self.assertEqual(SavedSearch.objects.count(), 1)

    def test_save_button_shows_buying(self):
        saveForm = f'action="{self.url}"'
        for params in ({}, {"unknown": "x"}):
            response = self.client.get(reverse("listings"), params)
            self.assertNotContains(response, "Save this search")
            self.assertNotContains(response, "Set a filter first")
            self.assertNotContains(response, saveForm)
        response = self.client.get(reverse("listings"), {"town": "TAMPINES"})
        self.assertContains(response, saveForm, count=1)
        self.assertContains(response, "Save this search", count=1)
        chips = response.content.decode().split('class="applied-filters"', 1)[1].split("</nav>", 1)[0]
        self.assertLess(
            chips.index("Clear all"),
            chips.index('<button class="btn btn-link">Save this search</button>'),
        )
        self.assertIn('<input type="hidden" name="town" value="TAMPINES">', chips)
        self.assertNotContains(response, "Set a filter first")
        self.client.logout()
        response = self.client.get(reverse("listings"), {"town": "TAMPINES", "flatType": "4 ROOM"})
        self.assertNotContains(response, saveForm)
        self.assertContains(response, "Save this search", count=1)
        self.assertContains(
            response,
            '<a class="save-search" href="/signin?next=/listings%3Ftown%3DTAMPINES%26flatType%3D4%2BROOM">Save this search</a>',
        )
        self.assertNotContains(self.client.get(reverse("listings")), "Save this search")
        self.client.force_login(self.buyer)
        self.client.post(self.url, {"q": "chua chu kang 4 room"})
        self.assertEqual(
            SavedSearch.objects.get().queryParams, {"town": "CHOA CHU KANG", "flatType": "4 ROOM"}
        )
        self.client.force_login(makeUser("seller@test.com", "seller"))
        self.client.post(reverse("switch_mode"), {"mode": "buyer"})
        self.assertContains(self.client.get(reverse("listings"), {"town": "TAMPINES"}), "Save this search")
        self.client.force_login(makeStaff("staff@test.com"))
        self.assertNotContains(self.client.get(reverse("listings"), {"town": "TAMPINES"}), "Save this search")


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    PUBLIC_URL="https://flatfair.example",
    NOTICE_MAIL_SKIP=("flatfair.sg",),
)
class SearchAlertTests(TestCase):

    def setUp(self):
        self.buyer = makeUser("buyer@test.com")
        self.seller = makeUser("seller@test.com", "seller")
        self.flat = makeListing(self.seller, status="review", town="TAMPINES")
        self.savedSearch = SavedSearch.objects.create(
            buyer=self.buyer,
            queryParams={"town": "TAMPINES", "flatType": "4 ROOM", "maxPrice": "600000"},
        )

    def publish(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.flat.status = "live"
            self.flat.save(update_fields=["status"])

    def test_going_live_sends_one(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.flat.status = "live"
            self.flat.save(update_fields=["status"])
            self.assertFalse(SearchAlert.objects.exists())
            self.assertFalse(ActivityNotice.objects.exists())
        alert = SearchAlert.objects.get()
        self.assertEqual(
            (alert.savedSearch, alert.listing, alert.kind),
            (self.savedSearch, self.flat, "new_match"),
        )
        notice = ActivityNotice.objects.get()
        self.assertEqual(notice.text, 'New flat matches "4-room in Tampines under $600k"')
        self.assertEqual(notice.user, self.buyer)
        self.assertEqual(notice.url, reverse("listing_detail", args=[self.flat.publicId]))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.buyer.email])
        self.assertIn("https://flatfair.example" + notice.url, mail.outbox[0].body)
        self.savedSearch.refresh_from_db()
        self.assertIsNotNone(self.savedSearch.lastNotifiedAt)

    def test_transition_live_checked(self):
        self.savedSearch.isActive = False
        self.savedSearch.save()
        self.publish()
        self.assertFalse(SearchAlert.objects.exists())
        self.savedSearch.isActive = True
        self.savedSearch.save()
        for status in ("sold", "hidden", "review"):
            with self.subTest(status=status):
                self.flat.status = status
                self.flat.save(update_fields=["status"])
                self.publish()
        self.assertEqual(SearchAlert.objects.count(), 1)

    def test_nonmatching_paused_own_listing(self):
        self.savedSearch.queryParams = {"town": "BEDOK"}
        self.savedSearch.save()
        SavedSearch.objects.create(buyer=self.buyer, queryParams={"town": "TAMPINES"}, isActive=False)
        self.seller.profile.canBuy = True
        self.seller.profile.save()
        SavedSearch.objects.create(buyer=self.seller, queryParams={"town": "TAMPINES"})
        self.publish()
        self.assertFalse(SearchAlert.objects.exists())
        self.assertFalse(ActivityNotice.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_matching_existing_filters(self):
        self.flat.hasBalcony = True
        self.flat.leaseCommenceYear = timezone.localdate().year - 10
        self.flat.save()
        self.savedSearch.queryParams = {
            "q": self.flat.postalCode,
            "town": "TAMPINES",
            "flatType": "4 ROOM",
            "maxPrice": "600000",
            "minSqm": "90",
            "maxSqm": "100",
            "bedrooms": "3",
            "bathrooms": "2",
            "minLease": "80",
            "balcony": "1",
        }
        self.savedSearch.save()
        self.publish()
        self.assertEqual(SearchAlert.objects.count(), 1)

    def test_new_live_listing_not(self):
        with self.captureOnCommitCallbacks(execute=True):
            makeListing(makeUser("other@test.com", "seller"), town="BEDOK")
        self.assertFalse(SearchAlert.objects.exists())
        with self.captureOnCommitCallbacks(execute=True):
            flat = makeListing(makeUser("matching@test.com", "seller"), town="TAMPINES")
        self.assertEqual(SearchAlert.objects.get().listing, flat)

    def test_suspended_seller_buyer_not(self):
        self.seller.is_active = False
        self.seller.save(update_fields=["is_active"])
        self.publish()
        self.assertFalse(SearchAlert.objects.exists())
        self.seller.is_active = True
        self.seller.save(update_fields=["is_active"])
        self.buyer.is_active = False
        self.buyer.save(update_fields=["is_active"])
        self.flat.status = "review"
        self.flat.save(update_fields=["status"])
        self.publish()
        self.assertFalse(SearchAlert.objects.exists())

    def test_supported_filter_exclude(self):
        for params in (
            {"q": "no matching address"},
            {"town": "BEDOK"},
            {"flatType": "3 ROOM"},
            {"maxPrice": "500000"},
            {"minSqm": "100"},
            {"maxSqm": "80"},
            {"bedrooms": "4"},
            {"bathrooms": "3"},
            {"minLease": "99"},
            {"balcony": "1"},
        ):
            with self.subTest(params=params):
                self.savedSearch.queryParams = params
                self.savedSearch.save()
                self.flat.status = "review"
                self.flat.save(update_fields=["status"])
                self.publish()
                self.assertFalse(SearchAlert.objects.exists())

    def test_republishing_once_new_lowest(self):
        self.publish()
        with self.captureOnCommitCallbacks(execute=True):
            self.flat.status = "hidden"
            self.flat.save(update_fields=["status"])
            self.flat.status = "live"
            self.flat.save(update_fields=["status"])
        for price in [540000, 530000, 535000, 530000]:
            with self.captureOnCommitCallbacks(execute=True):
                self.flat.askingPrice = price
                self.flat.save(update_fields=["askingPrice"])
        self.assertEqual(SearchAlert.objects.filter(kind="new_match").count(), 1)
        self.assertEqual(
            sorted(SearchAlert.objects.filter(kind="price_drop").values_list("askingPrice", flat=True)),
            [530000, 540000],
        )
        self.assertEqual(ActivityNotice.objects.filter(user=self.buyer).count(), 3)
        self.assertEqual(len(mail.outbox), 3)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SearchAlert.objects.create(
                savedSearch=self.savedSearch,
                listing=self.flat,
                kind="price_drop",
                askingPrice=530000,
            )

    def test_email_links_local_site(self):
        with self.settings(PUBLIC_URL=""):
            self.publish()
        self.assertIn("http://127.0.0.1:8765/listing/", mail.outbox[-1].body)

    def test_price_drop_bring_flat(self):
        self.flat.askingPrice = 650000
        self.flat.save(update_fields=["askingPrice"])
        self.publish()
        self.assertFalse(SearchAlert.objects.exists())
        with self.captureOnCommitCallbacks(execute=True):
            self.flat.askingPrice = 590000
            self.flat.save(update_fields=["askingPrice"])
        notice = ActivityNotice.objects.get()
        self.assertEqual(notice.text, "Price dropped on a flat in your saved search")
        self.assertEqual(SearchAlert.objects.get().kind, "price_drop")

    def test_matching_reuses_listing_price(self):
        SavedSearch.objects.create(buyer=self.buyer, queryParams={"maxPrice": "500000"})
        SavedSearch.objects.create(buyer=self.buyer, queryParams={"maxPrice": "700000"})
        with patch("storefront.search.costCap", side_effect=AssertionError("Unneeded global query")):
            self.publish()
        self.assertEqual(SearchAlert.objects.count(), 2)

    def test_price_increases_equal_prices(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.flat.askingPrice = 540000
            self.flat.save(update_fields=["askingPrice"])
        self.assertFalse(SearchAlert.objects.exists())
        self.publish()
        with self.captureOnCommitCallbacks(execute=True):
            for price in (550000, 550000):
                self.flat.askingPrice = price
                self.flat.save(update_fields=["askingPrice"])
        self.assertFalse(SearchAlert.objects.filter(kind="price_drop").exists())

    def test_unpersisted_price_change_not(self):
        self.publish()
        with self.captureOnCommitCallbacks(execute=True):
            self.flat.askingPrice = 530000
            self.flat.save(update_fields=["description"])
        self.assertFalse(SearchAlert.objects.filter(kind="price_drop").exists())

    def test_full_edit_price_drop(self):
        self.publish()
        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            self.flat.status = "review"
            self.flat.askingPrice = 540000
            self.flat.save(update_fields=["status", "askingPrice"])
            self.flat.status = "live"
            self.flat.save(update_fields=["status"])
        self.assertEqual(SearchAlert.objects.filter(kind="price_drop").count(), 1)

    def test_rolled_back_publication_not(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.flat.status = "live"
                    self.flat.save(update_fields=["status"])
                    raise ValueError("rollback")
            except ValueError:
                pass
        self.assertFalse(SearchAlert.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_staff_approval_shared_hook(self):
        self.client.force_login(makeStaff("admin@test.com"))
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("admin_decide", args=[self.flat.pk]),
                {"revision": self.flat.revision, "status": "review", "decision": "approved"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SearchAlert.objects.count(), 1)

    @patch("sellerpanel.views.band", return_value=(500000, 550000, 600000))
    def test_seller_price_edit_shared(self, priceBand):
        self.publish()
        self.client.force_login(self.seller)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("update_price", args=[self.flat.publicId]), {"askingPrice": "540000"}
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SearchAlert.objects.filter(kind="price_drop").count(), 1)

    def test_deleted_cascades(self):
        self.publish()
        self.savedSearch.delete()
        self.assertFalse(SearchAlert.objects.exists())
