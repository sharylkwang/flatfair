import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sellerpanel", "0001_initial"),
        ("storefront", "0040_integrity_rules"),
    ]

    operations = [
        migrations.CreateModel(
            name="PackageRecommendation",
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
                ("reason", models.CharField(max_length=300)),
                ("modelVersion", models.CharField(max_length=30)),
                ("createdAt", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("converted", models.BooleanField(default=False)),
                (
                    "listing",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="packageRecommendations",
                        to="storefront.listing",
                    ),
                ),
                (
                    "orderItem",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="packageRecommendation",
                        to="storefront.orderitem",
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="packageRecommendations",
                        to="storefront.product",
                    ),
                ),
            ],
        ),
    ]
