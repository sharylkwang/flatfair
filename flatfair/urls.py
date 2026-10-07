import posixpath


from django.conf import settings
from django.conf.urls.static import static



from django.http import Http404



from django.urls import path, include



from django.views.static import serve


def media(request, path):
    path = posixpath.normpath(path.replace("\\", "/")).lstrip("/")


    if path.split("/", 1)[0] in ("listings", "demo", "deliveries", "pending", "vision"):
        raise Http404


    return serve(request, path, document_root=settings.MEDIA_ROOT)


urlpatterns = [
    path("staff/", include("adminpanel.urls")),


    path("seller/", include("sellerpanel.urls")),


    path("", include("accounts.urls")),


    path("", include("storefront.urls")),


    path("", include("forum.urls")),
]



urlpatterns += static(settings.MEDIA_URL, view=media)
