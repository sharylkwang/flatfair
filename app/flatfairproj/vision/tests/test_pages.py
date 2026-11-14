from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import User
from django.template import Context, Template
from django.test import TestCase, override_settings
from django.urls import reverse

from pretrained import pipelineIdentity
from storefront.models import ListingPhoto, Profile, Listing
from vision.models import PhotoAnalysis
from vision.tests.helpers import makeListing

PROJECT = Path(__file__).resolve().parents[2]
NO_TORCH = '\nimport os, sys\nos.environ.setdefault("DJANGO_SETTINGS_MODULE", "flatfair.settings")\nimport django\ndjango.setup()\nfrom vision.templatetags import vision_tags\nfrom vision import queue, services, worker\nprint("torch" in sys.modules, "transformers" in sys.modules)\n'


class PageTests(TestCase):

    def setUp(self):
        self.media = TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        setting = override_settings(MEDIA_ROOT=self.media.name)
        setting.enable()
        self.addCleanup(setting.disable)
        seller = User.objects.create_user("qwer@test.com")
        Profile.objects.create(user=seller, role="seller")
        self.flat = makeListing(seller, "live")
        photo = ListingPhoto.objects.create(listing=self.flat, room="living_room", image="test.jpg")
        PhotoAnalysis.objects.create(photo=photo, status="pending")

    def test_listing_shows_shop_look(self):
        with patch("vision.templatetags.vision_tags.weights_ok", return_value=True):
            response = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        for check_text_1 in ('id="shop-the-look"', "vision/vision.js", "data-lightbox-group"):
            self.assertContains(response, check_text_1)

    def test_listing_keeps_calm_shop(self):
        with patch("vision.templatetags.vision_tags.weights_ok", return_value=False):
            response = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        for check_text_2 in (
            'id="shop-the-look"',
            "Couldn’t check photos",
            "We couldn’t check these photos. Try again later.",
        ):
            self.assertContains(response, check_text_2)
        self.assertNotContains(response, "vision/vision.js")

    def test_importing_web_vision_modules(self):
        done = subprocess.run(
            [sys.executable, "-c", NO_TORCH],
            cwd=PROJECT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        self.assertEqual(done.stdout.split()[-2:], ["False", "False"])


class PendingTests(TestCase):

    def setUp(self):
        self.media = TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        setting = override_settings(MEDIA_ROOT=self.media.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.seller = User.objects.create_user("asdf@test.com", password="Zxcv-9876!")
        Profile.objects.create(user=self.seller, role="seller")
        self.flat = self.make_flat()

    def make_flat(self, **values):
        data = dict(
            seller=self.seller,
            town="SENGKANG",
            block="123",
            postalCode="540123",
            flatType="4 ROOM",
            flatModel="Model A",
            floorAreaSqm=93,
            storey=8,
            leaseCommenceYear=1995,
            bedrooms=3,
            bathrooms=2,
            hasBalcony=False,
            askingPrice=600000,
            estimatedPrice=600000,
            bandLow=500000,
            bandHigh=700000,
        )
        return Listing.objects.create(**dict(data, **values))

    @patch("vision.templatetags.vision_tags.weights_ok", return_value=True)
    def test_analysis_shows_checking_message(self, weights):
        photo = ListingPhoto.objects.create(listing=self.flat, room="living_room", image="test.jpg")
        PhotoAnalysis.objects.get_or_create(photo=photo, defaults={"status": "pending"})
        html = Template("{% load vision_tags %}{% shop_look flat %}").render(Context({"flat": self.flat}))
        self.assertIn("Checking the photos", html)
        self.assertIn("Refresh to see items", html)
        self.assertIn("vision/vision.js", html)


class PanelTests(TestCase):

    def setUp(self):
        self.seller = User.objects.create_user("asdf")
        Profile.objects.create(user=self.seller, role="seller")
        self.flat = makeListing(self.seller, "live")
        self.media = TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        setting = override_settings(MEDIA_ROOT=self.media.name)
        setting.enable()
        self.addCleanup(setting.disable)

    @patch("vision.templatetags.vision_tags.weights_ok", return_value=True)
    def test_shows_pending_empty_completion(self, weights):
        photo = ListingPhoto.objects.create(listing=self.flat, room="living", image="test.jpg")
        record = PhotoAnalysis.objects.create(photo=photo, status="pending")
        template = Template("{% load vision_tags %}{% shop_look flat %}")
        for status in ["pending", "done", "failed"]:
            record.status = status
            record.pipelineIdentity = pipelineIdentity()
            record.save(update_fields=["status", "pipelineIdentity"])
            html = template.render(Context({"flat": self.flat}))
            self.assertIn('class="panel calm-details vision-panel"', html)
            self.assertIn(f'data-vision-state="{('pending' if status == 'pending' else 'complete')}"', html)
            if status != "pending":
                self.assertIn("data-vision-empty", html)
                self.assertNotIn("data-vision-pending", html)

    @patch("vision.templatetags.vision_tags.weights_ok", return_value=True)
    def test_photo_preview_urls_rendered(self, weights):
        photos = []
        for room in ["living_room", "kitchen"]:
            photo = ListingPhoto.objects.create(listing=self.flat, room=room, image=f"listings/{room}.jpg")
            PhotoAnalysis.objects.create(photo=photo, status="pending")
            photos.append(photo)
        template = Template("{% load vision_tags %}{% shop_look flat %}")
        with self.assertNumQueries(2):
            html = template.render(Context({"flat": self.flat}))
        for photo in photos:
            self.assertIn(f'src="{photo.publicUrl}"', html)

    @patch("vision.templatetags.vision_tags.weights_ok", return_value=True)
    def test_sold_listing_shows_completed(self, weights):
        Listing.objects.filter(pk=self.flat.pk).update(status="sold", statusBeforeSold="live")
        self.flat.refresh_from_db()
        living = ListingPhoto.objects.create(
            listing=self.flat, room="living_room", image="listings/living.jpg"
        )
        kitchen = ListingPhoto.objects.create(listing=self.flat, room="kitchen", image="listings/kitchen.jpg")
        PhotoAnalysis.objects.update_or_create(photo=living, defaults={"status": "pending"})
        template = Template("{% load vision_tags %}{% shop_look flat %}")
        self.assertNotIn("vision-panel", template.render(Context({"flat": self.flat})))
        PhotoAnalysis.objects.filter(photo=living).update(status="done", pipelineIdentity=pipelineIdentity())
        html = template.render(Context({"flat": self.flat}))
        self.assertIn('data-vision-state="complete"', html)
        self.assertNotIn("Checking", html)
        self.assertNotIn(f'src="{kitchen.publicUrl}"', html)
