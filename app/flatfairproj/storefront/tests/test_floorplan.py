from io import BytesIO
from PIL import Image
from storefront.models import Sale
from .helpers import makeOrder
from .helpers import makeProduct
from storefront.floorplan import planPng
from storefront.floorplan import planRooms
from storefront.floorplan import planUrl
from django.utils import timezone
import math
import time
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch
from xml.etree import ElementTree
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, Client, RequestFactory, override_settings
from django.urls import reverse
from storefront import floorplan as fp
from storefront.management.commands.floorplan_osm import blockShape
from .helpers import makeListing, makeStaff, makeUser
from .helpers import closeResponse
from django.conf.urls.static import static
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.http import Http404
from flatfair.urls import media
from sellerpanel.models import ListingDraft
from sellerpanel.photos import DRAFTS_KEY
from storefront.models import ListingPhoto
from storefront.tests.helpers import makeListing, makeStaff, makeUser
from vision.models import DetectedItem, PhotoAnalysis
from storefront.interiors import picCredits, FALLBACK_COVER, townCover
from storefront.valuation import TOWNS

PREVIEW = {
    "flatType": "4 ROOM",
    "flatModel": "Model A",
    "floorAreaSqm": "92",
    "bedrooms": "3",
    "bathrooms": "2",
}


def makeFlat(seller):
    return makeListing(
        seller,
        town="BEDOK",
        block="204",
        postalCode="460204",
        flatModel="Model A",
        floorAreaSqm=104,
        leaseCommenceYear=1985,
    )


@skipUnless(fp.PLANS_ON, "Floor plans are turned off")
class EngineTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.flat = makeFlat(self.seller)

    def test_svg_plan_immutable_cache(self):
        url = fp.planUrl(self.flat)
        response = self.client.get(url)
        self.assertEqual(response["Content-Type"], "image/svg+xml; charset=utf-8")
        self.assertIn("immutable", response["Cache-Control"])
        ElementTree.fromstring(response.content)
        again = self.client.get(url, HTTP_IF_NONE_MATCH=response["ETag"])
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again["ETag"], response["ETag"])
        self.assertEqual(again["Cache-Control"], response["Cache-Control"])

    def test_plan_url_changes_details(self):
        before = fp.planUrl(self.flat)
        self.flat.floorAreaSqm = 105
        self.assertNotEqual(before, fp.planUrl(self.flat))
        self.assertTrue(before.endswith("-r" + str(fp.RENDER_VERSION) + "-sqm"))

    def test_private_plan_requires_owner(self):
        self.flat.status = "review"
        self.flat.save()
        url = reverse("listing_floor_plan", args=[self.flat.publicId])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(makeUser("qwer@test.com"))
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get(url)["Cache-Control"], "private, no-store")
        self.client.force_login(makeStaff("admin@test.com"))
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_listing_page_links_plan(self):
        response = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        self.assertContains(response, fp.planUrl(self.flat))
        self.assertContains(response, 'data-lightbox data-caption="1980s Model A · 104 sqm · typical layout"')

    def test_preview_selected_flat_model(self):
        response = self.client.get(
            reverse("floor_plan_preview"),
            {**PREVIEW, "flatModel": "New Generation", "floorAreaSqm": "67", "flatType": "3 ROOM"},
        )
        self.assertEqual(response["Content-Type"], "image/svg+xml; charset=utf-8")
        self.assertIn(b"New Generation", response.content)

    def test_preview_parses_balcony_boolean(self):
        plans = {}
        for value in ["no", "yes", "false"]:
            plans[value] = self.client.get(
                reverse("floor_plan_preview"), {**PREVIEW, "hasBalcony": value}
            ).content
        self.assertEqual(plans["no"], plans["false"])
        self.assertNotEqual(plans["no"], plans["yes"])

    def test_unsupported_dimensions_fall(self):
        self.flat.flatType = "2 ROOM"
        self.flat.floorAreaSqm = 32
        self.flat.bedrooms = 2
        self.flat.bathrooms = 1
        self.flat.save()
        self.assertEqual(fp.planTitle(self.flat), "2-room · 32 sqm")
        plan = self.client.get(fp.planUrl(self.flat))
        self.assertRedirects(plan, reverse("floor_plan", args=[self.flat.publicId]))
        preview = self.client.get(
            reverse("floor_plan_preview"),
            {"flatType": "4 ROOM", "floorAreaSqm": "9", "bedrooms": "3"},
        )
        self.assertEqual(preview["Content-Type"], "image/png")

    def test_svg_plan_labels_areas(self):
        svg = self.client.get(fp.planUrl(self.flat)).content.decode()
        self.assertNotIn("m²", svg)
        self.assertIn("Total 104 sqm", svg)
        self.assertRegex(svg, "\\d+\\.\\d sqm")
        self.assertIn("data:image/png;base64,", svg)

    def test_osm_command_saves_attribution(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "shapes.json"
            with patch("storefront.floorplan.BLOCK_SHAPES_PATH", path):
                call_command("floorplan_osm", "--postal", "000000", stdout=StringIO())
            self.assertIn("OpenStreetMap", path.read_text())

    def test_private_plan_not_reuse(self):
        url = fp.planUrl(self.flat)
        response = self.client.get(url)
        self.flat.status = "review"
        self.flat.save(update_fields=["status"])
        self.client.force_login(self.seller)
        response = self.client.get(url, HTTP_IF_NONE_MATCH=response["ETag"])
        self.assertEqual(response["Cache-Control"], "private, no-store")


@patch("storefront.floorplan.PLANS_ON", False)
class FallbackTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.flat = makeFlat(self.seller)

    def test_missing_engine_png_plan(self):
        png = reverse("floor_plan", args=[self.flat.publicId])
        self.assertEqual(fp.planUrl(self.flat), png)
        self.assertEqual(fp.planTitle(self.flat), "")
        response = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        self.assertContains(response, 'src="' + png + '"')
        self.assertContains(response, 'data-caption="4-room · 104 sqm · typical layout"')
        self.assertRedirects(self.client.get(reverse("listing_floor_plan", args=[self.flat.publicId])), png)

    def test_missing_engine_preview(self):
        response = self.client.get(reverse("floor_plan_preview"), PREVIEW)
        self.assertEqual(response["Content-Type"], "image/png")
        odd = self.client.get(
            reverse("floor_plan_preview"),
            {"flatType": "asdf", "floorAreaSqm": "nan", "bedrooms": "99"},
        )
        self.assertEqual(odd.status_code, 200)

    def test_osm_command_requires_floor(self):
        with self.assertRaises(CommandError):
            call_command("floorplan_osm", stdout=StringIO())


class SellPreviewTests(TestCase):

    def test_form_loads_plan_script(self):
        self.client.force_login(makeUser("asdf@test.com", "seller"))
        response = self.client.get(reverse("sell"))
        self.assertContains(response, 'data-plan-preview="' + reverse("floor_plan_preview") + '"')
        self.assertContains(response, 'storefront/plan-preview.js" defer', count=1)
        self.assertNotContains(self.client.get(reverse("home")), "plan-preview.js")


class ShapeTests(SimpleTestCase):

    def outline(self, length, depth, bearing, lat=1.35, lon=103.85):
        angle = math.radians(bearing)
        points = []
        for u, v in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            x = u * length / 2 * math.sin(angle) + v * depth / 2 * math.cos(angle)
            y = u * length / 2 * math.cos(angle) - v * depth / 2 * math.sin(angle)
            points.append((lat + y / 111320, lon + x / (111320 * math.cos(math.radians(lat)))))
        return (points, lat, lon)

    def test_block_outline_classifies_slab(self):
        slab = blockShape(*self.outline(120, 14, 60))
        self.assertEqual(slab["form"], "slab")
        self.assertLessEqual(abs(slab["axis"] - 60), 1)
        self.assertEqual(blockShape(*self.outline(30, 26, 10))["form"], "point")


class PhotoPermissionTests(TestCase):

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        setting = override_settings(MEDIA_ROOT=directory.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.seller = makeUser("mediaowner@test.com", "seller")
        self.other = makeUser("mediaother@test.com", "seller")
        self.staff = makeStaff("mediastaff@test.com")
        self.path = default_storage.save("pending/draft.jpg", ContentFile(b"draft photo"))
        self.url = reverse("draft_photo", kwargs={"path": self.path})

    def assertPhoto(self, client, url, content):
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), content)
        closeResponse(response)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        return response

    def test_saved_draft_accessible_owner(self):
        ListingDraft.objects.create(seller=self.seller, draftToken="saved", photos={"kitchen": self.path})
        for user in (None, self.other, self.staff):
            client = Client()
            if user:
                client.force_login(user)
            self.assertEqual(client.get(self.url).status_code, 404)
        self.client.force_login(self.seller)
        response = self.assertPhoto(self.client, self.url, b"draft photo")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        ListingDraft.objects.all().delete()
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_unsaved_upload_requires(self):
        self.client.force_login(self.seller)
        session = self.client.session
        session[DRAFTS_KEY] = {"temporary": {"created": time.time(), "photos": {"kitchen": self.path}}}
        session.save()
        self.assertPhoto(self.client, self.url, b"draft photo")
        otherSession = Client()
        otherSession.force_login(self.seller)
        self.assertEqual(otherSession.get(self.url).status_code, 404)
        session = self.client.session
        session[DRAFTS_KEY]["temporary"]["created"] -= 86401
        session.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_draft_previews_checked_urls(self):
        from sellerpanel.listings import photoSlots

        slots = photoSlots({}, {"kitchen": self.path})
        self.assertEqual(next((slot["url"] for slot in slots if slot["key"] == "kitchen")), self.url)
        ListingDraft.objects.create(seller=self.seller, draftToken="saved", photos={"kitchen": self.path})
        self.client.force_login(self.seller)
        default_storage.delete(self.path)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_crop_visibility_tracks_listing(self):
        flat = makeListing(self.seller)
        photo = ListingPhoto.objects.create(listing=flat, room="kitchen", image="test.jpg")
        analysis = PhotoAnalysis.objects.create(photo=photo, status="done")
        item = DetectedItem.objects.create(analysis=analysis, label="sofa", score=0.9)
        item.crop.save("sofa.png", ContentFile(b"crop photo"))
        url = reverse("vision_crop", args=[item.pk])
        response = self.assertPhoto(self.client, url, b"crop photo")
        self.assertIn("must-revalidate", response["Cache-Control"])
        for status, before in (
            ("review", ""),
            ("hidden", ""),
            ("rejected", ""),
            ("sold", "review"),
        ):
            flat.status, flat.statusBeforeSold = (status, before)
            flat.save()
            self.assertEqual(self.client.get(url).status_code, 404)
        for user in (self.seller, self.staff):
            client = Client()
            client.force_login(user)
            response = self.assertPhoto(client, url, b"crop photo")
            self.assertEqual(response["Cache-Control"], "private, no-store")
        client = Client()
        client.force_login(self.other)
        self.assertEqual(client.get(url).status_code, 404)
        flat.statusBeforeSold = "live"
        flat.save()
        self.assertPhoto(self.client, url, b"crop photo")
        self.seller.is_active = False
        self.seller.save()
        self.assertEqual(self.client.get(url).status_code, 404)
        default_storage.delete(item.crop.name)
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get(url).status_code, 404)

    @override_settings(DEBUG=True)
    def test_debug_media_route_blocks(self):
        self.client.force_login(self.seller)
        routes = static("/media/", view=media)
        request = RequestFactory().get("/media/" + self.path)
        request.user = self.seller
        for path in (
            self.path,
            "vision/crop.png",
            "public/../pending/draft.jpg",
            "public/../vision/crop.png",
            "vision\\crop.png",
        ):
            with patch("flatfair.urls.serve") as serve:
                with self.assertRaises(Http404):
                    routes[0].callback(request, path=path)
                serve.assert_not_called()


STATIC_ROOT = Path(__file__).resolve().parents[2] / "assets" / "static"
PHOTO_FOLDERS = ("storefront/photos", "storefront/towns", "storefront/estate")
ROOM_PICTURES = {
    "storefront/photos/home-check.jpg",
    "storefront/photos/home-check-960.jpg",
    "storefront/estate/snap_room.jpg",
    "storefront/estate/snap_room_960.jpg",
}
TOWNS_WITHOUT_COVER = set()


class PhotoCreditTests(TestCase):

    def creditedFiles(self):
        files = set()
        for photo in picCredits()["photos"]:
            files.add(photo["file"])
            files.update(photo["variants"])
        return files

    def test_page_lists_author_licence(self):
        photos = picCredits()["photos"]
        self.assertTrue(photos)
        response = self.client.get(reverse("credits"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cropped them and lightly corrected their colour")
        self.assertContains(response, "CC BY-SA licence remain under that licence")
        self.assertContains(response, "<tr data-photo=", count=len(photos))
        for photo in photos:
            with self.subTest(photo=photo["file"]):
                self.assertTrue(photo["author"])
                self.assertTrue(photo["licence"])
                self.assertContains(response, f'data-photo="{photo['file']}"')
                self.assertContains(response, f'href="{photo['sourceUrl']}"')
                self.assertContains(response, photo["author"])
                self.assertContains(response, f'href="{photo['licenceUrl']}"')

    def test_static_credited(self):
        credited = self.creditedFiles()
        found = set()
        for folder in PHOTO_FOLDERS:
            for path in (STATIC_ROOT / folder).iterdir():
                if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                    found.add(f"{folder}/{path.name}")
        self.assertEqual(found - credited - ROOM_PICTURES, set())
        self.assertEqual(credited - found, set())
        self.assertEqual(credited & ROOM_PICTURES, set())

    def test_source_used_once(self):
        sources = [photo["sourceUrl"] for photo in picCredits()["photos"]]
        self.assertEqual(len(sources), len(set(sources)))

    def test_town_cover_file_fallback(self):
        covered = [town for town in TOWNS if town not in TOWNS_WITHOUT_COVER]
        self.assertEqual(len(covered), 26)
        covers = {townCover(town) for town in covered}
        self.assertEqual(len(covers), 26)
        self.assertEqual(townCover("KALLANG/WHAMPOA"), "storefront/towns/kallang-whampoa.jpg")
        for cover in covers:
            self.assertTrue(cover.startswith("storefront/towns/"))
            self.assertTrue((STATIC_ROOT / cover).is_file())
        for town in TOWNS_WITHOUT_COVER:
            self.assertEqual(townCover(town), FALLBACK_COVER)
        self.assertEqual(townCover("NOWHERE"), FALLBACK_COVER)
        self.assertIn(FALLBACK_COVER, self.creditedFiles())

    def test_footer_links_to_credits(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, f'<a href="{reverse('credits')}">Photo credits</a>', count=2)


class FloorPlanTests(TestCase):

    def setUp(self):
        self.seller = makeUser("asdf@test.com", "seller")
        self.flat = makeListing(self.seller)
        self.url = reverse("floor_plan", args=[self.flat.publicId])

    def test_png_access_follows_listing(self):
        response = self.client.get(self.url)
        self.assertEqual(response["Content-Type"], "image/png")
        image = Image.open(BytesIO(response.content))
        self.assertEqual(image.size, (900, 760))
        self.flat.status = "hidden"
        self.flat.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_login(makeUser("qwer@test.com"))
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_login(self.seller)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.client.force_login(makeStaff("admin@test.com"))
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_rooms_area_variants_cache(self):
        names = {}
        for flatType in ("2 ROOM", "3 ROOM", "4 ROOM", "5 ROOM", "EXECUTIVE"):
            width, depth, rooms = planRooms(flatType, 3, 2, 110)
            names[flatType] = [room["name"] for room in rooms]
            self.assertAlmostEqual(width * depth, 110)
        self.assertIn("Living and dining", names["4 ROOM"])
        self.assertIn("Dining", names["5 ROOM"])
        self.assertIn("Study", names["EXECUTIVE"])
        self.assertNotIn("Study", names["5 ROOM"])
        self.assertIn("Bedroom", [room["name"] for room in planRooms("2 ROOM", 1, 1, 45)[2]])
        self.assertNotEqual(planPng("4 ROOM", 3, 2, 110), planPng("5 ROOM", 3, 2, 110))
        self.assertEqual(planPng("4 ROOM", 3, 2, 110), planPng("4 ROOM", 3, 2, 110))

    def test_typical_disclaimer_visible(self):
        response = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        self.assertContains(
            response,
            '<p class="muted">A typical 4-room layout at this size. The real flat may look different.</p>',
            html=True,
        )
        self.assertNotContains(response, 'title="Rooms in the real flat may differ."')

    def test_delivered_seller_requires(self):
        Sale.objects.all().delete()
        page = reverse("listing_detail", args=[self.flat.publicId])
        response = self.client.get(page)
        self.assertContains(response, planUrl(self.flat))
        self.assertNotContains(response, "Seller's floor plan")
        self.assertContains(response, "Not enough recent sales to compare floors yet.")
        order = makeOrder(self.seller, makeProduct("Floor plan", "addon", 40, None), "completed", self.flat)
        item = order.items.get()
        item.deliveryFile.save("plan.pdf", ContentFile(b"%PDF-1.4 asdf"))
        pdf = reverse("seller_floor_plan", args=[self.flat.publicId])
        self.assertContains(self.client.get(page), pdf)
        response = self.client.get(pdf)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4 asdf")
        order.status = "processing"
        order.save()
        self.assertEqual(self.client.get(pdf).status_code, 404)

    def test_flat_page_shows_price(self):
        Sale.objects.all().delete()
        month = timezone.localdate().replace(day=1)
        for storeys, price in (("01 TO 03", 500000), ("07 TO 09", 530000), ("13 TO 15", 560000)):
            for n in range(5):
                Sale.objects.create(
                    month=month,
                    town="YISHUN",
                    flatType="4 ROOM",
                    block="1",
                    street="asdf",
                    storeyRange=storeys,
                    floorAreaSqm=92,
                    flatModel="Model A",
                    leaseCommenceYear=1990,
                    price=price,
                )
        response = self.client.get(reverse("listing_detail", args=[self.flat.publicId]))
        for check_text_1 in (
            "Prices by floor",
            "4-room flats in Yishun.",
            "High floors +$60k · last 12 months",
            "High floors averaged $60,000 more than low floors.",
            "Other things affect price too, so a higher floor doesn’t guarantee a higher price.",
        ):
            self.assertContains(response, check_text_1)
        self.assertContains(response, 'class="is-this"', count=1)
