from django.urls import path
from django.templatetags.static import static
from django.views.generic import RedirectView
from . import (
    views,
    activity,
    floorplan,
    search as saved_searches,
    deals,
    presentation,
    notices,
    offeradvice as pricecoach,
)
from vision import views as visionViews
from accounts import views as accountViews

urlpatterns = [
    path("start-selling", activity.startSell, name="start_selling"),
    path("start-buying", activity.startBuy, name="start_buying"),
    path("switch-mode", activity.switchMode, name="switch_mode"),



    path("activity", activity.action, name="activity_action"),


    path("viewings/cancel", activity.cancel_viewing, name="cancel_viewing"),
    path("viewings/move", activity.move_viewing, name="move_viewing"),
    path("deals/<int:pk>", deals.deal_page, name="deal_page"),
    path("price-suggestions/<int:pk>/keep", pricecoach.keep_price, name="keep_price"),
    path(
        "listing/<uuid:publicId>/ask-viewing",
        activity.ask_viewing,
        name="ask_viewing",
    ),
    path(
        "viewings/<int:pk>/calendar.ics",
        activity.booking_calendar,
        name="booking_calendar",
    ),



    path(
        "viewings/times/<int:pk>/calendar.ics",
        activity.slot_calendar,
        name="slot_calendar",
    ),
    path("deals/<int:pk>/step", deals.deal_step, name="deal_step"),


    path(
        "favicon.ico",
        RedirectView.as_view(url=static("storefront/favicon.svg")),
        name="favicon",
    ),


    path("", views.home, name="home"),
    path("listings", views.listings, name="listings"),
    path("lens", views.lens, name="lens"),



    path("valuation", views.valuation, name="valuation"),
    path("valuation/check", views.valuation_check, name="valuation_check"),
    path("api/addresses", views.address_search, name="address_search"),



    path("api/search-suggestions", views.findHints, name="search_suggestions"),
    path("packages", presentation.packages, name="packages"),


    path("terms", views.terms, name="terms"),
    path("credits", views.credits, name="credits"),



    path("listing/<uuid:publicId>", views.listing_detail, name="listing_detail"),


    path("saved", presentation.saved, name="saved"),
    path("saved/searches/new", saved_searches.saveSearch, name="save_search"),
    path("saved/searches/<int:pk>", saved_searches.editSearch, name="change_search"),
    path(
        "listing/<uuid:publicId>/photos/<slug:room>",
        views.listing_photo,
        name="listing_photo",
    ),
    path("photos/draft/<path:path>", views.draftPhoto, name="draft_photo"),
    path("photos/items/<int:pk>", views.visionCrop, name="vision_crop"),
    path("photos/look/<int:pk>/pick", visionViews.pick, name="vision_pick"),
    path("photos/picks/<int:pk>", visionViews.pick_state, name="vision_pick_status"),
    path("photos/picks/<int:pk>/image", visionViews.pick_crop, name="vision_pick_crop"),


    path(
        "listing/<uuid:publicId>/floor-plan.png",
        floorplan.floor_plan,
        name="floor_plan",
    ),
    path(
        "listing/<uuid:publicId>/floor-plan.pdf",
        floorplan.seller_floor_plan,
        name="seller_floor_plan",
    ),
    path(
        "listing/<uuid:publicId>/floor-plan.svg",
        floorplan.listing_floor_plan,
        name="listing_floor_plan",
    ),
    path(
        "floor-plan/preview",
        floorplan.floor_plan_preview,
        name="floor_plan_preview",
    ),
    path("listing/<uuid:publicId>/save", presentation.toggle_save, name="toggle_save"),
    path("saved/new", presentation.collection_create, name="collection_create"),
    path("saved/<int:pk>/rename", presentation.collection_rename, name="collection_rename"),



    path("saved/<int:pk>/delete", presentation.collection_delete, name="collection_delete"),
    path("my-offers", views.my_offers, name="my_offers"),
    path("notifications", notices.notifications, name="notifications"),
    path("support", notices.support, name="support"),
    path("support/messages", notices.my_messages, name="my_messages"),
    path("support/messages/<int:pk>", notices.my_ticket, name="my_ticket"),
    path("register", accountViews.register, name="register"),
    path("signin", accountViews.signin, name="signin"),
    path("signout", accountViews.signout, name="signout"),
]
