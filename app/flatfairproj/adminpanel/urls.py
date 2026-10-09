import adminpanel.moderation as adminpanel_moderation
import adminpanel.orders as adminpanel_orders
import adminpanel.reports as adminpanel_reports
from django.urls import path
from . import views, views as tickets

urlpatterns = [
    path("", views.overview, name="admin_overview"),
    path("moderation", adminpanel_moderation.queue, name="admin_queue"),


    path(
        "moderation/approve-selected",
        adminpanel_moderation.bulkPass,
        name="admin_bulk_approve",
    ),
    path(
        "moderation/<int:pk>", adminpanel_moderation.queue_item, name="admin_queue_item"
    ),
    path(
        "moderation/<int:pk>/decide", adminpanel_moderation.decide, name="admin_decide"
    ),
    path(
        "moderation/<int:pk>/photos/<int:photoId>",
        adminpanel_moderation.pic_choice,
        name="admin_photo_decide",
    ),
    path("listings", adminpanel_moderation.listings, name="admin_listings"),
    path("listings/<int:pk>", adminpanel_moderation.flat_view, name="admin_listing"),
    path(
        "listings/<int:pk>/action",
        adminpanel_moderation.flat_do,
        name="admin_listing_action",
    ),



    path(
        "listings/<int:pk>/edit",
        adminpanel_moderation.flat_edit,
        name="admin_listing_edit",
    ),


    path("orders", adminpanel_orders.orders, name="admin_orders"),
    path("deals", adminpanel_orders.deals, name="admin_deals"),


    path("payments", adminpanel_orders.payments, name="admin_payments"),
    path(
        "payments/<int:pk>/action",
        adminpanel_orders.pay_action,
        name="admin_payment_action",
    ),
    path("orders/<int:pk>", adminpanel_orders.see_order, name="admin_order"),
    path(
        "orders/<int:pk>/status", adminpanel_orders.order_set, name="admin_order_status"
    ),



    path("customers", views.user_list, name="admin_customers"),
    path("customers/<int:pk>", views.see_user, name="admin_customer"),



    path(
        "customers/<int:pk>/suspend",
        views.user_block,
        name="admin_customer_suspend",
    ),
    path("catalogue", views.catalogue, name="admin_catalogue"),



    path("catalogue/new", views.item_edit, name="admin_product_new"),


    path("catalogue/<int:pk>", views.item_edit, name="admin_product_edit"),
    path("catalogue/<int:pk>/toggle", views.item_flip, name="admin_product_toggle"),
    path("catalogue/<int:pk>/photo", views.item_pic, name="admin_product_photo"),
    path("staff", views.staff, name="admin_staff"),



    path("staff/<int:pk>/remove", views.staff_drop, name="admin_staff_remove"),
    path("audit", adminpanel_reports.audit_log, name="admin_audit"),
    path("export/audit", adminpanel_reports.audit_csv, name="admin_export_audit"),


    path("export/orders", adminpanel_reports.orders_csv, name="admin_export_orders"),
    path(
        "export/customers", adminpanel_reports.users_csv, name="admin_export_customers"
    ),
    path("export/listings", adminpanel_reports.flats_csv, name="admin_export_listings"),
    path("tickets", tickets.tickets, name="admin_tickets"),
    path("tickets/<int:pk>", tickets.see_ticket, name="admin_ticket"),


    path("tickets/<int:pk>/action", tickets.ticket_do, name="admin_ticket_action"),
    path("reviews", adminpanel_moderation.reviews, name="admin_reviews"),



    path("reviews/<int:pk>", adminpanel_moderation.see_review, name="admin_review"),


    path(
        "reviews/<int:pk>/visibility",
        adminpanel_moderation.flip_review,
        name="admin_review_visibility",
    ),
    path("account", views.account, name="admin_account"),
    path("price-model", views.price_page, name="admin_price_model"),
]
