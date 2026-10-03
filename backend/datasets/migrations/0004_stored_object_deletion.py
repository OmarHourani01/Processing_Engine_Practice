from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("datasets", "0003_local_scaling")]

    operations = [
        migrations.CreateModel(
            name="StoredObjectDeletion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("object_key", models.CharField(max_length=1024, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={"ordering": ["created_at", "pk"]},
        ),
    ]
