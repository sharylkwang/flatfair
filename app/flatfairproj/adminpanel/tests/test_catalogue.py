import io

from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from PIL import Image

from adminpanel.models import AuditLog
from storefront.tests.helpers import makeProduct, makeStaff, makeUser


def picture(colour="red", kind="PNG", name="photo.png", contentType="image/png"):
    out = io.BytesIO()
    Image.new("RGB", (640, 480), colour).save(out, kind)
    return SimpleUploadedFile(name, out.getvalue(), content_type=contentType)


class ProductPhotoTests(TestCase):

    def setUp(self):
        self.admin = makeStaff("admin@test.com")
        self.product = makeProduct("Pro photography", "addon", 180, None)
        self.url = reverse("admin_product_photo", args=[self.product.pk])
        self.page = reverse("admin_product_edit", args=[self.product.pk])
        self.client.force_login(self.admin)

    def upload(self, **details):
        return self.client.post(self.url, {"image": picture(**details)})

    def test_admin_uploads_sellers_see(self):
        self.assertContains(self.client.get(self.page), "No photo yet")
        self.assertRedirects(self.upload(), self.page)
        self.product.refresh_from_db()
        self.assertTrue(self.product.image.name.startswith("products/"))
        self.assertTrue(self.product.image.name.endswith(".jpg"))
        self.assertTrue(default_storage.exists(self.product.image.name))
        self.assertContains(self.client.get(self.page), self.product.image.url)
        self.assertContains(self.client.get(reverse("admin_catalogue")), self.product.image.url)
        self.assertTrue(
            AuditLog.objects.filter(action="product photo changed", target="Pro photography").exists()
        )
        self.client.logout()
        self.assertContains(self.client.get(reverse("packages")), self.product.image.url)

    def test_replacing_deletes_old_file(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.upload()
        self.product.refresh_from_db()
        first = self.product.image.name
        with self.captureOnCommitCallbacks(execute=True):
            self.upload(colour="blue")
        self.product.refresh_from_db()
        self.assertNotEqual(self.product.image.name, first)
        self.assertFalse(default_storage.exists(first))
        self.assertTrue(default_storage.exists(self.product.image.name))

    def test_admin_removes_a_photo(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.upload()
        self.product.refresh_from_db()
        name, url = (self.product.image.name, self.product.image.url)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertRedirects(self.client.post(self.url, {"remove": "1"}), self.page)
        self.product.refresh_from_db()
        self.assertFalse(self.product.image)
        self.assertFalse(default_storage.exists(name))
        self.assertNotContains(self.client.get(reverse("packages")), url)
        self.assertTrue(AuditLog.objects.filter(action="product photo removed").exists())

    def test_file_not_refused(self):
        bad = SimpleUploadedFile("notes.png", b"this is not a picture", content_type="image/png")
        response = self.client.post(self.url, {"image": bad}, follow=True)
        self.assertContains(response, "This file is not a photo")
        pdf = SimpleUploadedFile("file.pdf", b"%PDF-1.4", content_type="application/pdf")
        self.assertContains(
            self.client.post(self.url, {"image": pdf}, follow=True), "This file is not a photo"
        )
        self.assertContains(self.client.post(self.url, {}, follow=True), "Choose a photo first.")
        self.product.refresh_from_db()
        self.assertFalse(self.product.image)

    def test_admins_change_by_post(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.force_login(makeUser("seller@test.com", "seller"))
        self.assertEqual(self.upload().status_code, 403)
        self.client.logout()
        self.assertEqual(self.upload().status_code, 302)
        self.product.refresh_from_db()
        self.assertFalse(self.product.image)

    def test_other_services_get_blank(self):
        makeProduct("Floor plan", "addon", 40, None)
        self.assertNotContains(self.client.get(reverse("packages")), "addon-photo")
        self.upload()
        page = self.client.get(reverse("packages"))
        self.assertContains(page, 'class="addon-photo"', count=1)
        self.assertContains(page, "addon-photo-none", count=1)
