#!/usr/bin/env python3
"""Start the local Compose stack and scale its worker replicas from queue pressure."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.yml"
POLL_SECONDS = 15
QUEUES = {
    "ingest": "worker-ingest",
    "images": "worker-images",
}

sys.path.insert(0, str(ROOT / "backend"))
from processing.autoscaling import QueueTimers, next_replica_count


def compose_command(*args: str) -> list[str]:
    return ["docker", "compose", "-f", str(COMPOSE_FILE), *args]


def run_checked(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        env=os.environ.copy(),
    )


def project_name() -> str:
    configured = os.environ.get("COMPOSE_PROJECT_NAME", "")
    env_path = ROOT / ".env"
    if not configured and env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            key, separator, value = raw_line.partition("=")
            if separator and key.strip() == "COMPOSE_PROJECT_NAME":
                configured = value.strip().strip("\"'")
                break
    configured = configured or ROOT.name.lower().replace(" ", "-")
    return configured


def acquire_lock():
    lock_name = hashlib.sha256(project_name().encode()).hexdigest()[:16]
    lock_path = Path("/tmp") / f"nxgen-local-autoscaler-{lock_name}.lock"
    handle = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("A local autoscaler is already running for this Compose project") from exc
    return handle


def warn_legacy_settings() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    legacy_names = {"INGEST_CONCURRENCY", "IMAGE_CONCURRENCY"}
    found = set()
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        key, separator, _ = raw_line.partition("=")
        if separator and key.strip() in legacy_names:
            found.add(key.strip())
    if found:
        print(
            "Ignoring superseded fixed concurrency setting(s): " + ", ".join(sorted(found))
            + ". Worker limits are stored in the app's Worker scaling page.",
            file=sys.stderr,
        )


def running_replicas(service: str) -> int:
    result = run_checked(compose_command("ps", "--status", "running", "-q", service), capture=True)
    return len([line for line in result.stdout.splitlines() if line.strip()])


def collect_snapshot(replicas: dict[str, int]) -> dict:
    args = compose_command(
        "exec", "-T", "backend", "python", "manage.py", "local_scaling_status",
        "--ingest-replicas", str(replicas["ingest"]),
        "--images-replicas", str(replicas["images"]),
    )
    result = run_checked(args, capture=True)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Backend returned invalid scaling telemetry") from exc


def report_error(message: str) -> None:
    try:
        run_checked(compose_command(
            "exec", "-T", "backend", "python", "manage.py", "local_scaling_status",
            "--report-error", message[:500],
        ), capture=True)
    except (OSError, subprocess.CalledProcessError):
        pass


def run_controller() -> None:
    os.environ["LOCAL_SCALING_ENABLED"] = "1"
    _lock = acquire_lock()  # Keep the file descriptor open for the lifetime of this process.
    warn_legacy_settings()
    print("Starting the local Compose stack with worker autoscaling enabled…", flush=True)
    run_checked(compose_command("up", "-d", "--build"))
    timers = {queue: QueueTimers() for queue in QUEUES}

    def reset_demand_timers() -> None:
        for queue_timers in timers.values():
            queue_timers.backlog_since = None
            queue_timers.idle_since = None

    try:
        while True:
            tick_started = time.monotonic()
            try:
                replicas = {queue: running_replicas(service) for queue, service in QUEUES.items()}
                snapshot = collect_snapshot(replicas)
                if not snapshot.get("ok"):
                    reset_demand_timers()
                    print(f"Autoscaler degraded: {snapshot.get('error', 'worker inspection failed')}", flush=True)
                else:
                    policy = snapshot["policy"]
                    enabled = policy["replica_autoscaling_enabled"]
                    desired = dict(replicas)
                    for queue in QUEUES:
                        queue_policy = policy["queues"][queue]
                        metrics = snapshot["queues"][queue]
                        busy = bool(metrics["active"] or metrics["reserved"] or metrics["scheduled"])
                        desired[queue] = next_replica_count(
                            enabled=enabled,
                            current=replicas[queue],
                            minimum=queue_policy["min_replicas"],
                            maximum=queue_policy["max_replicas"],
                            queued=metrics["queued"],
                            busy=busy,
                            now=tick_started,
                            timers=timers[queue],
                        )
                        print(
                            f"{queue}: replicas {replicas[queue]}→{desired[queue]}, "
                            f"queued {metrics['queued']}, active {metrics['active']}, "
                            f"reserved {metrics['reserved']}, processes {metrics['processes']}",
                            flush=True,
                        )
                    if desired != replicas:
                        scale_args = ["scale", "--no-deps"] + [
                            f"{QUEUES[queue]}={count}" for queue, count in desired.items()
                        ]
                        try:
                            run_checked(compose_command(*scale_args))
                        except (OSError, subprocess.CalledProcessError) as exc:
                            message = f"Compose scaling failed: {exc}"
                            report_error(message)
                            print(f"Autoscaler degraded: {message}", file=sys.stderr, flush=True)
            except (OSError, RuntimeError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
                reset_demand_timers()
                message = f"{type(exc).__name__}: {exc}"
                report_error(message)
                print(f"Autoscaler degraded: {message}", file=sys.stderr, flush=True)

            elapsed = time.monotonic() - tick_started
            time.sleep(max(0, POLL_SECONDS - elapsed))
    except KeyboardInterrupt:
        print("Local autoscaler stopped. The Compose stack is still running; use docker compose down to stop it.", flush=True)


if __name__ == "__main__":
    try:
        run_controller()
    except KeyboardInterrupt:
        print("Local autoscaler stopped during startup.", flush=True)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Could not start local autoscaling: {exc}", file=sys.stderr)
        sys.exit(1)
