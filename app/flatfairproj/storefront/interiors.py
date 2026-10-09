def roomPhotos():
    picked = []

    for room in ("kitchen", "master_bedroom", "bedroom_2"):
        photo = (
            ListingPhoto.objects.filter(
                listing__in=Listing.objects.public().filter(status="live"), room=room
            )
            .exclude(image="")
            .exclude(listing__in=[chosen.listing_id for chosen in picked])
            .select_related("listing")
            .order_by("pk")
            .first()
        )

        if photo:
            picked.append(photo)

    return picked
