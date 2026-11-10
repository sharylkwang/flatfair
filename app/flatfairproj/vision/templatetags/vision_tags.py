from django import template
from vision.pipeline import weights_ok
from vision.models import PhotoAnalysis
from vision.services import item_words, links, find_words, like_items, like_pool, is_stale

register = template.Library()


@register.inclusion_tag("vision/panel.html", takes_context=True)
def shop_look(context, listing):
    if not weights_ok():
        return {}



    scans = PhotoAnalysis.objects.filter(photo__listing=listing)



    scans = list(scans.select_related("photo__listing").prefetch_related("items"))


    stale = []

    for scan in scans:
        if is_stale(scan):
            stale.append(scan)

    stale_ids = []

    for scan in stale:
        stale_ids.append(scan.pk)



    PhotoAnalysis.objects.filter(pk__in=stale_ids, status="done").update(status="pending")

    for scan in stale:
        scan.status = "pending"

    if listing.status != "live":
        scans = [analysis for analysis in scans if analysis.status == "done"]

        if not scans:
            return {}


    if not scans and not listing.photos.exists():
        return {}

    has_items = any(scan.status == "done" and analysis.items.all() for analysis in scans)

    if has_items:
        pool = like_pool(listing.pk)
    else:
        pool = {}

    panels = []
    pending = not scans and listing.photos.exists()
    total = 0
    lens_enabled = False


    for scan in scans:
        pending = pending or scan.status in ("pending", "running")
        cards = []



        if scan.status == "done":
            done_items = scan.items.all()
        else:
            done_items = []

        for item in done_items:
            search, lens = links(item)
            lens_enabled = lens_enabled or bool(lens)
            total += 1


            cards.append(
                {
                    "item": item,
                    "search": search,
                    "lens": lens,
                    "words": find_words(item),
                    "similar": like_items(item, pool),
                    "attributes": item_words(item)[:2],
                    "points": " ".join(
                        f"{horizontal_position},{vertical_position}"
                        for horizontal_position, vertical_position in item.polygon
                    ),
                }
            )

        size = scan.timings.get("size", [1000, 1000])

        panels.append(
            {
                "analysis": scan,
                "cards": cards,
                "width": size[0],
                "height": size[1],
                "floor": floor_words(scan.flooring) if scan.flooring else "",
            }
        )

    from vision.views import picking


    return {
        "panels": panels,
        "enabled": True,
        "picking": picking(),
        "csrf_token": context.get("csrf_token"),
        "pending": pending,
        "failed": any(scan.status == "failed" for scan in scans),
        "total": total,
        "lens_enabled": lens_enabled,
    }


@register.filter
def floor_words(flooring):
    flooring = flooring or {}


    made_of = (
        flooring.get("material") or str(flooring.get("label", "")).removeprefix("Possibly ").strip()
    )

    if not made_of:
        return "Not sure"



    return f"Probably {made_of}" if flooring.get("possible") else made_of.capitalize()
