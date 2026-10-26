import ipaddress

from django.conf import settings
from django.http import HttpResponse


from django.shortcuts import render


from django.template.loader import render_to_string
from django.utils.cache import patch_vary_headers


class FriendlyDebug404Middleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        reply = self.get_response(request)

        if (
            settings.DEBUG
            and reply.status_code == 404
            and request.method == "GET"
            and request.GET.get("debug404") != "1"
            and "text/html" in request.headers.get("Accept", "text/html")
        ):
            return render(request, "404.html", status=404)

        return reply


class SingleAddressMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not self.onPublicSite(request):
            return self.get_response(request)

        if self.opensInTheTab(request):
            reply = HttpResponse(
                render_to_string("storefront/shell.html", {"path": request.get_full_path()})
            )

            reply["Cache-Control"] = "no-store"
        else:
            reply = self.get_response(request)

        patch_vary_headers(reply, ["Sec-Fetch-Dest"])

        return reply

    def onPublicSite(self, request):
        return (
            bool(settings.SINGLE_ADDRESS and settings.SINGLE_ADDRESS_HOST)
            and request.get_host().split(":")[0] == settings.SINGLE_ADDRESS_HOST
        )

    def opensInTheTab(self, request):
        return (
            request.method == "GET"
            and request.headers.get("Sec-Fetch-Dest") == "document"
            and request.headers.get("Sec-Fetch-Mode") == "navigate"
        )


class RealClientAddressMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        sentOn = request.META.get("HTTP_CF_CONNECTING_IP", "").strip()

        if (
            sentOn
            and self.fromThisComputer(request.META.get("REMOTE_ADDR", ""))
            and self.validAddress(sentOn)
        ):
            request.META["REMOTE_ADDR"] = sentOn

        return self.get_response(request)

    def fromThisComputer(self, address):
        try:
            return ipaddress.ip_address(address).is_loopback
        except Exception as error:
            return False

    def validAddress(self, address):
        try:
            ipaddress.ip_address(address)

            return True
        except Exception as error:
            return False
