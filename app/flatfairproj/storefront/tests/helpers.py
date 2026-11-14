from html.parser import HTMLParser
from datetime import timedelta
from decimal import Decimal
import io

from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from django.test.client import closing_iterator_wrapper
from PIL import Image

from accounts.verification import setEmailOk
from sellerpanel.photocheck import MODEL_VERSION
from storefront.models import Listing, Order, OrderItem, Product, Profile
from storefront.presentation import TIER_RANKS


def closeResponse(response):
    list(closing_iterator_wrapper((), response.close))

PASSWORD = "Asdf1234!qwer"
FLAT = {
    "town": "JURONG EAST",
    "block": "225a",
    "postalCode": "601225",
    "flatType": "3 ROOM",
    "flatModel": "New Generation",
    "floorAreaSqm": "67",
    "bedrooms": "2",
    "bathrooms": "1",
    "hasBalcony": "no",
    "storey": "8",
    "leaseCommenceYear": "1984",
    "askingPrice": "400000",
    "intentToSellDate": (timezone.localdate() - timedelta(days=10)).isoformat(),
    "ownsFlat": "on",
}
ROOMS = ["living_room", "kitchen", "master_bedroom", "bedroom_2", "common_toilet"]
FAIR = (350000, 400000, 450000)
CLEAN = {"modelVersion": MODEL_VERSION, "label": "kitchen", "irrelevant": 0.01, "aiGenerated": 0.01}


def makeUser(email, role="buyer", name="Asdf", password=None):
    user = User.objects.create_user(username=email, email=email, password=password, first_name=name)
    setEmailOk(user)
    if role:
        Profile.objects.create(user=user, role=role)
    return user


def makeStaff(email, role="Admin", name="Qwer", password=None):
    user = User.objects.create_user(
        username=email, email=email, password=password, first_name=name, is_staff=True
    )
    user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def makeListing(seller, **fields):
    values = {
        "seller": seller,
        "town": "YISHUN",
        "block": "123",
        "postalCode": "760123",
        "flatType": "4 ROOM",
        "floorAreaSqm": 92,
        "bedrooms": 3,
        "bathrooms": 2,
        "hasBalcony": False,
        "askingPrice": 550000,
        "estimatedPrice": 550000,
        "bandLow": 500000,
        "bandHigh": 600000,
        "modelVersion": "v5.3",
        "status": "live",
    }
    values.update(fields)
    return Listing.objects.create(**values)


def makeProduct(name="Boost 30 days", kind="package", price=49, days=30, tier=None):
    if tier is None:
        tier = TIER_RANKS.get(name, 0)
    return Product.objects.create(name=name, kind=kind, price=price, durationDays=days, tier=tier)


def makeOrder(seller, product, status="paid", listing=None):
    order = Order.objects.create(
        seller=seller, status=status, subtotal=product.price, gst=Decimal("0"), total=product.price
    )
    OrderItem.objects.create(
        order=order,
        product=product,
        price=product.price,
        name=product.name,
        kind=product.kind,
        tier=product.tier,
        durationDays=product.durationDays,
        listing=listing,
    )
    return order


def jpeg(colour="white"):
    out = io.BytesIO()
    Image.new("RGB", (16, 16), colour).save(out, "JPEG")
    return SimpleUploadedFile("room.jpg", out.getvalue(), content_type="image/jpeg")


class RoleMarkup(HTMLParser):

    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.defaultKpis = 0
        self.moreSections = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get("class", "").split())
        inRole = "role-page" in classes or any(("role-page" in entry[1] for entry in self.stack))
        inMore = "role-more" in classes or any(("role-more" in entry[1] for entry in self.stack))
        if inRole and tag == "details" and ("role-more" in classes):
            self.moreSections.append(attrs)
        if inRole and (not inMore) and classes.intersection({"dash-stat", "rich-kpi", "kpi"}):
            self.defaultKpis += 1
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append((tag, classes))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

