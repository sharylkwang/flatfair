from django.urls import path


from . import google, settings, turnstile, verification, views


urlpatterns = [
    path("check-email", verification.checkEmail, name="email_check"),


    path("authenticator", verification.authenticator, name="authenticator"),



    path(
        "authenticator/setup",
        verification.totpSetup,
        name="authenticator_setup",
    ),



    path("account", settings.myAccount, name="account_settings"),



    path("check", turnstile.humanCheck, name="human_check"),


    path("forgot-password", views.forgot, name="forgot_password"),



    path("forgot-password/code", views.forgotCode, name="forgot_code"),


    path("forgot-password/new", views.forgotNew, name="forgot_new"),


    path("accounts/google", google.googleGo, name="google_start"),



    path("accounts/google/callback", google.googleBack, name="google_callback"),



    path("accounts/google/relay", google.googleRelay, name="google_relay"),
]
