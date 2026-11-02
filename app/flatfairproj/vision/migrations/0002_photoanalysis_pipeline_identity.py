from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vision", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="photoanalysis",
            name="pipelineIdentity",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
