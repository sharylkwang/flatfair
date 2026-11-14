from django.test import TestCase
from django.urls import reverse

from sellerpanel.photocheck import MODEL_VERSION
from sellerpanel.recommend import picPitch
from storefront.models import ListingPhoto, Product
from storefront.tests.helpers import makeListing, makeOrder, makeUser


class PhotoUpsellTests(TestCase):

    def setUp(self):
        self.seller = makeUser("upsell-seller@test.com", "seller")
        self.flat = makeListing(self.seller)
        self.product = Product.objects.create(name="Pro photography", kind="addon", price=180, isActive=True)
        ListingPhoto.objects.create(
            listing=self.flat,
            room="kitchen",
            image="a.jpg",
            modelVersion=MODEL_VERSION,
            aiIrrelevant=0.01,
            aiGenerated=0.99,
        )
        ListingPhoto.objects.create(
            listing=self.flat,
            room="living_room",
            image="b.jpg",
            modelVersion=MODEL_VERSION,
            aiIrrelevant=0.01,
            aiGenerated=0.01,
        )

    def test_flagged_suggests_pro(self):
        upsell = picPitch(self.flat)
        self.assertEqual((upsell["count"], upsell["rooms"]), (1, ["kitchen"]))
        self.client.force_login(self.seller)
        for url in (
            reverse("packages") + f"?listing={self.flat.publicId}",
            reverse("seller_listing", args=[self.flat.publicId]),
        ):
            page = self.client.get(url)
            self.assertContains(page, "1 photo needs a better shot")
            self.assertContains(page, "Add Pro photography")

    def test_no_flag_already_bought(self):
        ListingPhoto.objects.filter(room="kitchen").update(aiGenerated=0.01)
        self.assertIsNone(picPitch(self.flat))
        ListingPhoto.objects.filter(room="kitchen").update(aiGenerated=0.99)
        makeOrder(self.seller, self.product, listing=self.flat)
        self.assertIsNone(picPitch(self.flat))

    def test_message_points_service(self):
        from sellerpanel.photos import reviewNote

        message = reviewNote({"aiGenerated": 0.99, "reasons": ["may be AI-generated"]}, "kitchen")
        self.assertIn("Pro photography in Packages can retake it for you.", message)
