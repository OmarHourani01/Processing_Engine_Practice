import unittest

from processing.autoscaling import QueueTimers, next_replica_count


class LocalReplicaPolicyTests(unittest.TestCase):
    def setUp(self):
        self.timers = QueueTimers()

    def decide(self, **overrides):
        values = {
            "enabled": True,
            "current": 1,
            "minimum": 1,
            "maximum": 3,
            "queued": 0,
            "busy": False,
            "now": 0,
            "timers": self.timers,
        }
        values.update(overrides)
        return next_replica_count(**values)

    def test_adds_one_replica_after_backlog_is_sustained(self):
        self.assertEqual(self.decide(queued=5, now=0), 1)
        self.assertEqual(self.decide(queued=5, now=29), 1)
        self.assertEqual(self.decide(queued=5, now=30), 2)
        self.assertEqual(self.decide(current=2, queued=5, now=59), 2)
        self.assertEqual(self.decide(current=2, queued=5, now=60), 3)
        self.assertEqual(self.decide(current=3, queued=5, now=90), 3)

    def test_saturated_workers_scale_up_when_prefetch_hides_broker_queue(self):
        self.assertEqual(self.decide(queued=0, demand=True, busy=True, now=0), 1)
        self.assertEqual(self.decide(queued=0, demand=True, busy=True, now=29), 1)
        self.assertEqual(self.decide(queued=0, demand=True, busy=True, now=30), 2)

    def test_saturation_demand_must_persist_before_scaling(self):
        self.assertEqual(self.decide(demand=True, now=0), 1)
        self.assertEqual(self.decide(demand=False, now=20), 1)
        self.assertEqual(self.decide(demand=True, now=29), 1)
        self.assertEqual(self.decide(demand=True, now=59), 2)

    def test_backlog_must_persist_and_queues_have_independent_timers(self):
        ingest = QueueTimers()
        images = QueueTimers()
        self.assertEqual(next_replica_count(enabled=True, current=1, minimum=1, maximum=2, queued=1, busy=False, now=0, timers=ingest), 1)
        self.assertEqual(next_replica_count(enabled=True, current=1, minimum=1, maximum=2, queued=0, busy=False, now=20, timers=ingest), 1)
        self.assertEqual(next_replica_count(enabled=True, current=1, minimum=1, maximum=2, queued=1, busy=False, now=29, timers=ingest), 1)
        self.assertEqual(next_replica_count(enabled=True, current=1, minimum=1, maximum=2, queued=1, busy=False, now=59, timers=ingest), 2)
        self.assertEqual(next_replica_count(enabled=True, current=1, minimum=1, maximum=2, queued=1, busy=False, now=50, timers=images), 1)

    def test_scale_down_waits_for_idle_and_cooldown(self):
        timers = QueueTimers(last_scaled_at=0)
        self.assertEqual(next_replica_count(enabled=True, current=3, minimum=1, maximum=3, queued=0, busy=True, now=200, timers=timers), 3)
        self.assertEqual(next_replica_count(enabled=True, current=3, minimum=1, maximum=3, queued=0, busy=False, now=201, timers=timers), 3)
        self.assertEqual(next_replica_count(enabled=True, current=3, minimum=1, maximum=3, queued=0, busy=False, now=260, timers=timers), 3)
        self.assertEqual(next_replica_count(enabled=True, current=3, minimum=1, maximum=3, queued=0, busy=False, now=321, timers=timers), 2)

    def test_pause_freezes_replicas_and_minimum_is_restored_when_enabled(self):
        self.assertEqual(self.decide(enabled=False, current=3, minimum=1, maximum=2, queued=20, now=30), 3)
        self.assertEqual(self.decide(current=0, minimum=1, maximum=2, now=31), 1)

    def test_no_scale_down_below_minimum(self):
        self.assertEqual(self.decide(current=1, minimum=1, maximum=3, now=1000), 1)


if __name__ == "__main__":
    unittest.main()
