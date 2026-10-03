from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("datasets", "0004_stored_object_deletion")]

    operations = [
        migrations.AlterModelOptions(
            name="dataset",
            options={"ordering": ["-created_at", "name"]},
        ),
    ]
