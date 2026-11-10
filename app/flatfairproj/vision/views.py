import math
from datetime import timedelta
from urllib.parse import urlencode

from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from accounts.tokens import addTry
from adminpanel.access import canSeeFlat
from storefront.views import picReply
from . import queue
from .models import ItemPick, PhotoAnalysis
from .pipeline import weights_ok
from .services import nearest, like_pool


def picking():
    return weights_ok() and queue.running()


def pick_data(pick):
    data = {
        "id": pick.pk,
        "status": pick.status,
        "url": reverse("vision_pick_status", args=[pick.pk]),
    }


    if pick.status == "empty":
        data["message"] = (
            "We couldn’t find an item there. Try tapping the middle of a piece of furniture."
        )
    elif pick.status == "failed":
        data["message"] = "Sorry, that didn’t work. Please try again."
    elif pick.status == "done":
        label = pick.label if pick.confident else "item you picked"



        flat = pick.analysis.photo.listing



        similar = nearest(pick.label, pick.embedding, like_pool(flat.pk)) if pick.confident else []


        data.update(
            label=label[:1].upper() + label[1:],
            confident=pick.confident,
            crop=reverse("vision_pick_crop", args=[pick.pk]),
            points=" ".join(
                f"{horizontal_position},{vertical_position}"
                for horizontal_position, vertical_position in pick.polygon
            ),
            words=pick.label if pick.confident else "",
            search="https://www.google.com/search?"
            + urlencode({"tbm": "isch", "q": pick.label if pick.confident else "furniture"}),
            similar=[
                {
                    "label": other.label[:1].upper() + other.label[1:],
                    "town": other.analysis.photo.listing.town.title(),
                    "url": reverse("listing_detail", args=[other.analysis.photo.listing.publicId]),
                }
                for other in similar
            ],
        )


    return data


def own_pick(request, pk):
    key = request.session.session_key

    if not key:
        raise Http404

    return get_object_or_404(
        ItemPick.objects.select_related("analysis__photo__listing"), pk=pk, sessionKey=key
    )


def readPoint(request):
    try:
        x_spot, y_spot = float(request.POST.get("x", "")), float(request.POST.get("y", ""))
    except ValueError:
        return None

    if not (
        math.isfinite(x_spot) and math.isfinite(y_spot) and 0 <= x_spot <= 1 and 0 <= y_spot <= 1
    ):
        return None

    return x_spot, y_spot


@require_POST
def pick(request, pk):
    near = 0.02
    analysis = get_object_or_404(
        PhotoAnalysis.objects.select_related("photo__listing"), pk=pk, status="done"
    )



    if not canSeeFlat(request.user, analysis.photo.listing):
        raise Http404

    point = readPoint(request)


    if point is None:
        return JsonResponse({"message": "Please tap inside the photo."}, status=400)


    if not picking():
        return JsonResponse(
            {"message": "Tap to search isn’t available right now. Please try again later."},
            status=503,
        )


    if not request.session.session_key:
        request.session.save()

    key = request.session.session_key
    x_spot, y_spot = point

    earlier = (
        ItemPick.objects.filter(
            analysis=analysis,
            sessionKey=key,
            x__range=(x_spot - near, x_spot + near),
            y__range=(y_spot - near, y_spot + near),
            status__in=("pending", "running", "done", "empty"),
        )
        .select_related("analysis__photo__listing")
        .order_by("-pk")
        .first()
    )


    if earlier:
        return JsonResponse(pick_data(earlier))

    if addTry(f"vision-pick:{key}", 3600) > 30:
        return JsonResponse(
            {"message": "You’ve looked up a lot of items. Please try again in an hour."}, status=429
        )


    ItemPick.objects.filter(createdAt__lt=timezone.now() - timedelta(days=1)).delete()
    made = ItemPick.objects.create(analysis=analysis, sessionKey=key, x=x_spot, y=y_spot)
    queue.wake()

    return JsonResponse(pick_data(made), status=202)


@require_GET
def pick_state(request, pk):
    return JsonResponse(pick_data(own_pick(request, pk)))


@require_GET
def pick_crop(request, pk):
    found = own_pick(request, pk)

    if found.status != "done" or not found.crop:
        raise Http404

    return picReply(found.crop)
