
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("forum", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="post",
            name="editedAt",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
