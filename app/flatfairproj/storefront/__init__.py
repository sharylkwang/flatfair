import mimetypes


from django.apps import AppConfig


class StorefrontConfig(AppConfig):



    default_auto_field = "django.db.models.BigAutoField"


    name = "storefront"

    def ready(self):



        mimetypes.add_type("application/manifest+json", ".webmanifest")


        from . import notices
