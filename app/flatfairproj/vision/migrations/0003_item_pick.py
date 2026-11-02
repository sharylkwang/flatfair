import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vision", "0002_photoanalysis_pipeline_identity"),
    ]

    operations = [
        migrations.CreateModel(
            name="ItemPick",
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
                ("sessionKey", models.CharField(db_index=True, max_length=40)),
                ("x", models.FloatField()),
                ("y", models.FloatField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("running", "Running"),
                            ("done", "Done"),
                            ("empty", "Nothing found"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                        max_length=10,
                    ),
                ),
                ("label", models.CharField(blank=True, max_length=60)),
                ("confident", models.BooleanField(default=False)),
                ("box", models.JSONField(default=list)),
                ("polygon", models.JSONField(default=list)),
                ("crop", models.ImageField(blank=True, upload_to="vision/picks/")),
                ("embedding", models.JSONField(default=list)),
                ("createdAt", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "analysis",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="picks",
                        to="vision.photoanalysis",
                    ),
                ),
            ],
        ),
    ]
