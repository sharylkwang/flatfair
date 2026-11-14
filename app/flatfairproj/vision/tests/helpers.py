import io

from PIL import Image

from storefront.models import Listing


def makeListing(seller, status):
    return Listing.objects.create(
        seller=seller,
        status=status,
        town="TAMPINES",
        flatType="4 ROOM",
        floorAreaSqm=90,
        bedrooms=3,
        bathrooms=2,
        hasBalcony=False,
        askingPrice=500000,
    )


def listing():
    return Listing.objects.create(
        town="Tampines",
        flatType="4 ROOM",
        floorAreaSqm=90,
        bedrooms=3,
        bathrooms=2,
        hasBalcony=False,
        askingPrice=500000,
    )


def output():
    buf = io.BytesIO()
    Image.new("RGBA", (10, 10)).save(buf, "PNG")
    return {
        "items": [
            dict(
                label="sofa",
                score=0.8,
                box=[0, 0, 10, 10],
                polygon=[[0, 0], [10, 0], [10, 10]],
                attributes={"colour": "grey", "material": "fabric"},
                placement="back-left",
                embedding=[1.0] + [0.0] * 511,
                png=buf.getvalue(),
            )
        ],
        "flooring": {"label": "parquet"},
        "layout": "Sofa back-left",
        "timings": {"total_seconds": 1},
        "size": [10, 10],
    }
