import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ListingDraft",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("formData", models.JSONField(default=dict)),
                ("draftToken", models.CharField(max_length=32)),
                ("photos", models.JSONField(default=dict)),
                ("checks", models.JSONField(default=dict)),
                ("updatedAt", models.DateTimeField(auto_now=True)),
                (
                    "seller",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="listingDraft",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
    ]
