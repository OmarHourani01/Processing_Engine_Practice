from django.db import migrations, models


def mark_legacy_defaults(apps, schema_editor):
    Policy = apps.get_model("datasets", "LocalScalingPolicy")
    for policy in Policy.objects.all().iterator():
        queue_defaults = (
            policy.ingest_max_processes == 2
            and policy.images_max_processes == 2
            and policy.ingest_max_replicas in (1, 2)
            and policy.images_max_replicas in (1, 2)
            and policy.ingest_min_processes == 1
            and policy.images_min_processes == 1
            and policy.ingest_min_replicas == 1
            and policy.images_min_replicas == 1
        )
        if not queue_defaults:
            policy.uses_automatic_defaults = False
            policy.save(update_fields=["uses_automatic_defaults"])


class Migration(migrations.Migration):
    dependencies = [("datasets", "0005_alter_dataset_options")]

    operations = [
        migrations.AddField(
            model_name="localscalingpolicy",
            name="uses_automatic_defaults",
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(mark_legacy_defaults, migrations.RunPython.noop),
    ]
