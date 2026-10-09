def mainFolder(user):
    default_list = "My shortlist"

    folder = user.collections.filter(name__iexact=default_list).order_by("createdAt", "pk").first()

    if folder is None:
        folder = Collection.objects.create(user=user, name=default_list)

    return folder


def packages(request):
    packQuery = Product.objects.filter(kind="package", isActive=True).order_by("price")
    goods = list(addRatings(packQuery))
    pickFlat = ""
    theFlat = None

    if request.user.is_authenticated:
        try:
            picked = listing_id(request.GET.get("listing"))
            theFlat = Listing.objects.filter(publicId=picked, seller=request.user).first()
            pickFlat = theFlat.publicId if theFlat else ""
        except ValidationError:
            pass

        if not request.GET.get("listing") and hasRole(request.user, "seller"):
            theFlat = cartFlat(request.user, request.session.get("cartListing"))
            pickFlat = theFlat.publicId if theFlat else ""

    running = {}
    planNotes = []

    if theFlat:
        for plan in packsNow([theFlat]):
            planNotes.append(planNote(plan))
            tier = tierOf(plan["tier"])

            if tier and tier not in running:
                running[tier] = plan

    topTier = ""

    for tier in running:
        if TIER_RANKS[tier] > TIER_RANKS.get(topTier, 0):
            topTier = tier

    for product in goods:
        product.packageTier = tierOf(product.tier)
        product.includingGst = savings(0, Decimal(product.price) * Decimal("1.09"))["cost"]
        spareText = {"benefits": [product.description], "place": "With free flats"}
        copy = PACKAGE_COPY.get(product.packageTier, spareText)
        product.benefits = [
            benefit.replace("{duration}", "").strip() for benefit in copy["benefits"]
        ]
        product.place = copy["place"]
        product.runningPlan = running.get(product.packageTier)
        product.coveredBy = ""

        if (
            topTier
            and not product.runningPlan
            and TIER_RANKS.get(product.packageTier, 0) < TIER_RANKS[topTier]
        ):
            product.coveredBy = topTier

    addons = list(addRatings(Product.objects.filter(kind="addon", isActive=True)).order_by("price"))

    extraInfo = {
        "Pro photography": "Up to 20 edited photos of your flat.",
        "Floor plan": "Your HDB layout as a shareable PDF.",
        "Viewing concierge": "A host for one 2-hour viewing.",
    }

    for product in addons:
        product.summary = extraInfo.get(product.name, product.description.split(". ")[0])
        product.includingGst = savings(0, Decimal(product.price) * Decimal("1.09"))["cost"]

    tip = showTip(theFlat) if hasRole(request.user, "seller") else None

    context = {
        "packages": goods,
        "addons": addons,
        "addonPhotos": any(product.image for product in addons),
        "reviews": newReviews(),
        "starRange": RATING_CHOICES,
        "isSeller": hasRole(request.user, "seller"),
        "cartCount": Product.objects.filter(
            pk__in=request.session.get("cart", []), isActive=True
        ).count(),
        "cartIds": request.session.get("cart", []),
        "selectedListing": pickFlat or "",
        "selectedFlat": theFlat,
        "runningNotes": planNotes,
        "runningPlans": sorted(running.values(), key=lambda plan: plan["tier"], reverse=True),
        "packageRecommendation": tip,
        "recommendationReason": (
            tip["reason"].split(". ")[0].split(";")[0].rstrip(".") + "." if tip else ""
        ),
        "listingAction": (
            reverse("sell")
            if hasRole(request.user, "seller")
            else reverse("register") + "?role=seller"
        ),
        "hasFlat": hasRole(request.user, "seller")
        and request.user.listings.filter(status__in=CART_LISTING_STATUSES).exists(),
    }

    return render(request, "storefront/packages.html", context)


@login_required
def saved(request):
    if not hasRole(request.user, "buyer"):
        return notAllowed(request, "buyer")

    folders = my_folders(request.user)
    current = None

    if folders:
        current = folders[0]

        for folder in folders:
            if str(folder.pk) == request.GET.get("c"):
                current = folder

                break

    homes = []

    if current:
        homes = (
            Listing.objects.public()
            .filter(savedBy__collection=current)
            .annotate(
                acceptedCount=Count("offers", filter=Q(offers__status="accepted"), distinct=True)
            )
            .prefetch_related(picsByRoom())
            .order_by("-savedBy__createdAt")
        )

        homes = decorate(withTier(homes))

    context = {
        "current": current,
        "savedSearches": request.user.savedSearches.order_by("createdAt", "pk"),
        "listings": homes,
        "activeSaved": [flat for flat in homes if flat.status != "sold"],
        "soldSaved": [flat for flat in homes if flat.status == "sold"],
        **save_data(request.user),
    }

    return render(request, "storefront/saved.html", context)


def saveMenu(request, flat):
    goTo = request.POST.get("next", "") if request.method == "POST" else request.GET.get("next", "")
    goTo = goTo.split("#", 1)[0]

    if not url_has_allowed_host_and_scheme(
        goTo, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        goTo = reverse("listing_detail", args=[flat.publicId])

    scope = (
        request.POST.get("scope", "") if request.method == "POST" else request.GET.get("scope", "")
    )

    context = {
        "flat": flat,
        "big": (request.POST.get("big") if request.method == "POST" else request.GET.get("big"))
        == "1",
        "saveScope": scope if scope.replace("-", "").isalnum() and len(scope) <= 20 else "",
        "saveNext": goTo,
        "open": True,
        **save_data(request.user),
    }

    return render_to_string("storefront/save_menu.html", context, request=request)


def saveAnswer(request, flat, text, failed=False):
    if request.headers.get("Accept") == "application/json":
        saved = SavedListing.objects.filter(collection__user=request.user, listing=flat).exists()
        count = (
            Listing.objects.public()
            .filter(savedBy__collection__user=request.user)
            .distinct()
            .count()
        )

        return JsonResponse(
            {
                "saved": saved,
                "count": count,
                "message": text,
                "failed": failed,
                "menu": saveMenu(request, flat),
            }
        )

    if failed:
        messages.error(request, text)
    else:
        messages.success(request, text)

    return backTo(request, "saved")


