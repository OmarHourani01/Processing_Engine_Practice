"""Prepare the private object bucket and browser CORS at startup."""

import time

from botocore.exceptions import BotoCoreError, ClientError
from django.core.management.base import BaseCommand, CommandError

from datasets.storage import ensure_bucket_and_cors


class Command(BaseCommand):
    help = "Create the configured object bucket and apply its browser CORS rules."

    def handle(self, *args, **options):
        attempts = 30
        for attempt in range(attempts):
            try:
                ensure_bucket_and_cors()
                self.stdout.write(self.style.SUCCESS("Object bucket and CORS are ready."))
                return
            except (BotoCoreError, ClientError) as exc:
                if attempt + 1 == attempts:
                    raise CommandError("Could not prepare MinIO after 30 attempts.") from exc
                time.sleep(2)
