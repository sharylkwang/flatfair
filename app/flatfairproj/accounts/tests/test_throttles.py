from datetime import timedelta
from hashlib import sha256
from unittest.mock import patch

from django.core.cache import cache as defaultCache
from django.core.cache.backends.db import DatabaseCache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.settings import AccountDeletionForm, PASSWORD_LOCKED, triesKey
from accounts.tokens import cache, addTry
from storefront.models import ResourceCacheEntry
from storefront.tests.helpers import PASSWORD, makeUser


class SharedAuthenticationLimitTests(TestCase):

    def setUp(self):
        cache.clear()
        defaultCache.clear()
        self.user = makeUser("shared@test.com", password=PASSWORD)
        self.otherWorker = DatabaseCache("storefront_resourcecacheentry", {})

    def test_sign_in_survives_default(self):
        key = "signin:" + sha256(f"{self.user.email}:127.0.0.1".encode()).hexdigest()
        for attempt in range(5):
            Client().post(reverse("signin"), {"email": self.user.email, "password": "wrong"})
            defaultCache.clear()
        self.assertEqual(self.otherWorker.get(key), 5)
        self.assertIsNone(defaultCache.get(key))
        response = Client().post(reverse("signin"), {"email": self.user.email, "password": PASSWORD})
        self.assertContains(response, PASSWORD_LOCKED)
        self.assertNotIn("_auth_user_id", response.wsgi_request.session)

    def test_valid_sign_in_clears(self):
        key = "signin:" + sha256(f"{self.user.email}:127.0.0.1".encode()).hexdigest()
        self.otherWorker.set(key, 2, 900)
        response = self.client.post(reverse("signin"), {"email": self.user.email, "password": PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.otherWorker.get(key))

    def test_settings_password_between_form(self):
        key = triesKey(self.user)
        self.otherWorker.set(key, 4, 900)
        self.assertFalse(AccountDeletionForm(self.user, {"currentPassword": "wrong"}).is_valid())
        self.assertEqual(self.otherWorker.get(key), 5)
        defaultCache.clear()
        form = AccountDeletionForm(self.user, {"currentPassword": PASSWORD})
        self.assertFalse(form.is_valid())
        self.assertIn(PASSWORD_LOCKED, form.errors["currentPassword"])

    def test_increment_keeps_expiry_expired(self):
        now = timezone.now().replace(microsecond=0)
        with patch("time.time", return_value=now.timestamp()):
            self.assertEqual(addTry("test-attempts", 900), 1)
        entry = ResourceCacheEntry.objects.get(cacheKey=cache.make_key("test-attempts"))
        expires = entry.expires
        later = now + timedelta(seconds=120)
        with (
            patch("time.time", return_value=later.timestamp()),
            patch("accounts.tokens.timezone.now", return_value=later),
        ):
            self.assertEqual(addTry("test-attempts", 900), 2)
        entry.refresh_from_db()
        self.assertEqual(entry.expires, expires)
        ResourceCacheEntry.objects.filter(pk=entry.pk).update(expires=now - timedelta(seconds=1))
        self.assertEqual(addTry("test-attempts", 900), 1)
