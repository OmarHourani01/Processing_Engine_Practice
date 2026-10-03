from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import LocalScalingPolicy, LocalScalingStatus


QUEUE_NAMES = ("ingest", "images")
STALE_AFTER = timedelta(seconds=45)


class QueueScalingSerializer(serializers.Serializer):
    min_processes = serializers.IntegerField(min_value=1, max_value=4)
    max_processes = serializers.IntegerField(min_value=1, max_value=4)
    min_replicas = serializers.IntegerField(min_value=1, max_value=4)
    max_replicas = serializers.IntegerField(min_value=1, max_value=4)

    def validate(self, attrs):
        for minimum, maximum in (("min_processes", "max_processes"), ("min_replicas", "max_replicas")):
            if attrs[minimum] > attrs[maximum]:
                raise serializers.ValidationError({maximum: "Must be greater than or equal to the minimum."})
        return attrs


class ScalingPolicySerializer(serializers.Serializer):
    replica_autoscaling_enabled = serializers.BooleanField()
    queues = serializers.DictField(child=QueueScalingSerializer())

    def validate_queues(self, queues):
        if set(queues) != set(QUEUE_NAMES):
            raise serializers.ValidationError("Provide settings for both ingest and images queues.")
        total_slots = sum(queue["max_processes"] * queue["max_replicas"] for queue in queues.values())
        if total_slots > settings.LOCAL_SCALING_SLOT_BUDGET:
            raise serializers.ValidationError(
                f"Combined maximum capacity is {total_slots} task slots; the local budget is "
                f"{settings.LOCAL_SCALING_SLOT_BUDGET}."
            )
        return queues


def status_payload(row=None, *, now=None):
    now = now or timezone.now()
    if row is None or row.last_seen_at is None or now - row.last_seen_at > STALE_AFTER:
        state = "offline"
    else:
        state = row.state
    return {
        "state": state,
        "last_seen_at": row.last_seen_at.isoformat() if row and row.last_seen_at else None,
        "error": row.error if row else "",
        "queues": row.queues if row else {},
    }


def scaling_payload(policy=None, status=None):
    policy = policy or LocalScalingPolicy.get_solo()
    if status is None:
        status = LocalScalingStatus.objects.filter(pk=1).first()
    return {"policy": policy.as_dict(), "status": status_payload(status)}


@method_decorator(csrf_protect, name="dispatch")
class LocalScalingView(APIView):
    permission_classes = [IsAuthenticated]

    def _ensure_enabled(self):
        if not settings.LOCAL_SCALING_ENABLED:
            raise Http404

    def get(self, request):
        self._ensure_enabled()
        return Response(scaling_payload())

    def put(self, request):
        self._ensure_enabled()
        serializer = ScalingPolicySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        policy_data = serializer.validated_data
        fields = {
            "replica_autoscaling_enabled": policy_data["replica_autoscaling_enabled"],
            "uses_automatic_defaults": False,
            "ingest_min_processes": policy_data["queues"]["ingest"]["min_processes"],
            "ingest_max_processes": policy_data["queues"]["ingest"]["max_processes"],
            "ingest_min_replicas": policy_data["queues"]["ingest"]["min_replicas"],
            "ingest_max_replicas": policy_data["queues"]["ingest"]["max_replicas"],
            "images_min_processes": policy_data["queues"]["images"]["min_processes"],
            "images_max_processes": policy_data["queues"]["images"]["max_processes"],
            "images_min_replicas": policy_data["queues"]["images"]["min_replicas"],
            "images_max_replicas": policy_data["queues"]["images"]["max_replicas"],
        }
        with transaction.atomic():
            policy, _ = LocalScalingPolicy.objects.select_for_update().get_or_create(pk=1)
            for name, value in fields.items():
                setattr(policy, name, value)
            policy.save(update_fields=[*fields, "updated_at"])
        return Response(scaling_payload(policy))
