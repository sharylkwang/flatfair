
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("storefront", "0035_listingphoto_embedding_listingphoto_tags"),
    ]

    operations = [
        migrations.CreateModel(
            name="PhotoAnalysis",
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
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("running", "Running"),
                            ("done", "Done"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                        max_length=10,
                    ),
                ),
                ("version", models.CharField(default="1", max_length=20)),
                ("error", models.TextField(blank=True)),
                ("timings", models.JSONField(default=dict)),
                ("flooring", models.JSONField(default=dict)),
                ("layout", models.TextField(blank=True)),
                (
                    "photo",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="vision_analysis",
                        to="storefront.listingphoto",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="DetectedItem",
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
                ("label", models.CharField(max_length=60)),
                ("score", models.FloatField()),
                ("box", models.JSONField(default=list)),
                ("polygon", models.JSONField(default=list)),
                ("crop", models.ImageField(upload_to="vision/")),
                ("attributes", models.JSONField(default=dict)),
                ("placement", models.CharField(blank=True, max_length=40)),
                ("embedding", models.JSONField(default=list)),
                (
                    "analysis",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="vision.photoanalysis",
                    ),
                ),
            ],
        ),
    ]
