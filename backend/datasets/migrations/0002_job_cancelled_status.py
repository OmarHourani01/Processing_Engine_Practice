from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("datasets", "0001_initial")]

    operations = [
        migrations.AlterField(
            model_name="job",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("running", "Running"),
                    ("succeeded", "Succeeded"),
                    ("succeeded_with_errors", "Succeeded with errors"),
                    ("failed", "Failed"),
                    ("cancelled", "Cancelled"),
                ],
                db_index=True,
                default="queued",
                max_length=24,
            ),
        ),
    ]
