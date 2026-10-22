import sellerpanel.billing as sellerpanel_billing
import sellerpanel.listings as sellerpanel_listings
from django.urls import path
from . import views
from adminpanel import documents

urlpatterns = [
    path("orders/<int:pk>/receipt", sellerpanel_billing.receipt, name="receipt"),
    path("orders/items/<int:pk>/rate", sellerpanel_billing.rate_item, name="rate_item"),
    path("orders/<int:pk>/received", sellerpanel_billing.confirmGot, name="confirm_delivery"),



    path("orders/items/<int:pk>/file", sellerpanel_billing.delivery_file, name="delivery_file"),
    path("", views.dashboard, name="dashboard"),



    path("sell", sellerpanel_listings.sell, name="sell"),



    path("sell/photo-check", sellerpanel_listings.photo_check, name="photo_check"),
    path("sell/photo-status", sellerpanel_listings.photo_status, name="photo_status"),



    path("sell/photo-back", views.photo_move_back, name="photo_move_back"),
    path("sell/price-check", sellerpanel_listings.cost_check, name="sell_price_check"),


    path("listings", sellerpanel_listings.my_listings, name="my_listings"),


    path("documents/<int:pk>/submit", documents.sendDocs, name="submit_documents"),
    path("documents/files/<int:pk>", documents.getDoc, name="document_file"),
    path("listings/<uuid:publicId>", sellerpanel_listings.seller_listing, name="seller_listing"),



    path("listings/<uuid:publicId>/next-steps", views.next_steps_done, name="next_steps_done"),
    path(
        "listings/<uuid:publicId>/next-steps/<slug:step>", views.next_step_go, name="next_step_go"
    ),


    path("listings/<uuid:publicId>/price", sellerpanel_listings.update_price, name="update_price"),



    path("listings/<uuid:publicId>/sold", views.toggle_sold, name="toggle_sold"),
    path("listings/<uuid:publicId>/offer-minimum", views.offer_floor, name="offer_floor"),
    path("listings/<uuid:publicId>/pause", views.pause_listing, name="pause_listing"),
    path("listings/<uuid:publicId>/resume", views.resume_listing, name="resume_listing"),
    path("listings/<uuid:publicId>/delete", views.delete_listing, name="delete_listing"),
    path("listings/<uuid:publicId>/edit", sellerpanel_listings.edit_listing, name="edit_listing"),
    path("new-seller-account", views.new_seller_account, name="new_seller_account"),
    path("viewings", views.seller_viewings, name="seller_viewings"),
    path("cart", sellerpanel_billing.cart, name="cart"),
    path("cart/add/<int:pk>", sellerpanel_billing.cart_add, name="cart_add"),


    path("cart/remove/<int:pk>", sellerpanel_billing.cart_remove, name="cart_remove"),
    path("checkout", sellerpanel_billing.checkout, name="checkout"),
    path("checkout/stripe", sellerpanel_billing.stripe_return, name="stripe_return"),


    path("orders", sellerpanel_billing.my_orders, name="my_orders"),
    path("payments/<int:pk>/check", sellerpanel_billing.checkPay, name="check_payment"),
]
