import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api';
import type { LocalScalingPolicy, QueueScalingLimits } from '../types';

const SCALING_QUERY_KEY = ['local-scaling'];

function policyValues(policy: LocalScalingPolicy) {
  const { slot_budget: _slotBudget, ...values } = policy;
  return values;
}

function validatePolicy(policy: LocalScalingPolicy): string | undefined {
  for (const [name, limits] of Object.entries(policy.queues)) {
    for (const [key, value] of Object.entries(limits)) {
      if (!Number.isInteger(value) || value < 1 || value > 4) return `${name}: ${key.replaceAll('_', ' ')} must be from 1 to 4.`;
    }
    if (limits.min_processes > limits.max_processes || limits.min_replicas > limits.max_replicas) {
      return `${name}: each minimum must be less than or equal to its maximum.`;
    }
  }
  const slots = policy.queues.ingest.max_processes * policy.queues.ingest.max_replicas
    + policy.queues.images.max_processes * policy.queues.images.max_replicas;
  if (slots > policy.slot_budget) return `Maximum capacity exceeds the ${policy.slot_budget}-slot local budget.`;
  return undefined;
}

function QueueCard({
  name,
  title,
  limits,
  actual,
  disabled,
  onChange,
}: {
  name: 'ingest' | 'images';
  title: string;
  limits: QueueScalingLimits;
  actual?: { queued: number; active: number; reserved: number; scheduled: number; replicas: number; processes: number };
  disabled: boolean;
  onChange: (name: 'ingest' | 'images', key: keyof QueueScalingLimits, value: number) => void;
}) {
  const fields: Array<[keyof QueueScalingLimits, string]> = [
    ['min_replicas', 'Minimum containers'],
    ['max_replicas', 'Maximum containers'],
    ['min_processes', 'Minimum processes per container'],
    ['max_processes', 'Maximum processes per container'],
  ];
  return <section className="scaling-pool panel">
    <div className="scaling-pool-heading">
      <div><h2>{title}</h2><p>Queue: <code>{name}</code></p></div>
      <span className={`scaling-pool-state ${actual ? 'available' : 'unavailable'}`}>{actual ? 'Connected' : 'Waiting'}</span>
    </div>
    <div className="scaling-metrics">
      <div><span>Containers</span><strong>{actual?.replicas ?? '—'}</strong></div>
      <div><span>Task processes</span><strong>{actual?.processes ?? '—'}</strong></div>
      <div><span>Waiting in queue</span><strong>{actual?.queued ?? '—'}</strong></div>
      <div><span>Active or reserved</span><strong>{actual ? actual.active + actual.reserved + actual.scheduled : '—'}</strong></div>
    </div>
    <div className="scaling-limits">
      {fields.map(([key, label]) => <label key={key}>
        <span>{label}</span>
        <input type="number" min={1} max={4} step={1} value={limits[key]} disabled={disabled} onChange={(event) => onChange(name, key, Number(event.target.value))} />
      </label>)}
    </div>
  </section>;
}

export function WorkerScaling() {
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: SCALING_QUERY_KEY, queryFn: api.localScaling, refetchInterval: 5_000, retry: false });
  const [draft, setDraft] = useState<LocalScalingPolicy>();
  const [editing, setEditing] = useState(false);
  const dirty = useMemo(() => Boolean(draft && query.data && JSON.stringify(policyValues(draft)) !== JSON.stringify(policyValues(query.data.policy))), [draft, query.data]);
  useEffect(() => {
    if (query.data && !editing) setDraft(query.data.policy);
  }, [query.data, editing]);

  const save = useMutation({
    mutationFn: api.saveLocalScaling,
    onSuccess: (result) => {
      setEditing(false);
      setDraft(result.policy);
      queryClient.setQueryData(SCALING_QUERY_KEY, result);
    },
  });

  const updateQueue = (name: 'ingest' | 'images', key: keyof QueueScalingLimits, value: number) => {
    setEditing(true);
    setDraft((current) => current ? {
      ...current,
      queues: { ...current.queues, [name]: { ...current.queues[name], [key]: value } },
    } : current);
  };
  const updateEnabled = (enabled: boolean) => {
    setEditing(true);
    setDraft((current) => current ? { ...current, replica_autoscaling_enabled: enabled } : current);
  };

  if (query.isLoading) return <section className="content-area scaling-page"><div className="inline-loading">Loading worker status…</div></section>;
  if (!query.data || !draft) return <section className="content-area scaling-page"><div className="scaling-error" role="alert">Could not load local scaling status. {query.error instanceof Error ? query.error.message : ''}<button className="text-button" onClick={() => void query.refetch()}>Retry</button></div></section>;

  const { policy, status } = query.data;
  const maxSlots = draft.queues.ingest.max_processes * draft.queues.ingest.max_replicas + draft.queues.images.max_processes * draft.queues.images.max_replicas;
  const currentSlots = status.state === 'healthy' && status.queues.ingest && status.queues.images
    ? status.queues.ingest.processes + status.queues.images.processes
    : undefined;
  const validationError = validatePolicy(draft);

  return <section className="content-area scaling-page">
    <div className="page-title-row">
      <div><div className="eyebrow">LOCAL CAPACITY</div><h1>Worker scaling</h1><p className="page-subtitle">Adjust task capacity for ingestion and image processing.</p></div>
      <span className={`scaling-controller-state ${status.state}`}><i /> Controller {status.state}</span>
    </div>

    {status.error && status.state !== 'offline' && <div className="scaling-error" role="status">{status.error}</div>}
    {status.state === 'offline' && <div className="scaling-notice" role="status">The local controller has not reported in the last 45 seconds. The worker counts shown below may be out of date.</div>}
    {query.error && <div className="scaling-notice" role="status">Live status could not be refreshed. Showing the last received values.</div>}

    <div className="scaling-switch-card panel">
      <label className="scaling-switch"><input type="checkbox" checked={draft.replica_autoscaling_enabled} onChange={(event) => updateEnabled(event.target.checked)} /><span className="scaling-switch-track" /><span><strong>Automatic container scaling</strong><small>Pause or resume replica changes. Celery will continue adjusting task processes within each container.</small></span></label>
      <div className="scaling-budget"><strong>{currentSlots ?? '—'} / {maxSlots}</strong><span>live processes / selected maximum</span><small>Policy budget: {policy.slot_budget} task slots</small></div>
    </div>

    {save.error && <div className="scaling-error" role="alert">{save.error instanceof Error ? save.error.message : 'Could not save worker limits.'}</div>}
    {validationError && <div className="scaling-error" role="alert">{validationError}</div>}

    <div className="scaling-pools">
      <QueueCard name="ingest" title="Ingestion" limits={draft.queues.ingest} actual={status.queues.ingest} disabled={save.isPending} onChange={updateQueue} />
      <QueueCard name="images" title="Image processing" limits={draft.queues.images} actual={status.queues.images} disabled={save.isPending} onChange={updateQueue} />
    </div>

    <div className="scaling-save-row"><span>{status.last_seen_at ? `Last controller update ${new Date(status.last_seen_at).toLocaleTimeString()}` : 'Waiting for the first controller update'}</span><button className="button button-primary" disabled={!dirty || save.isPending || !!validationError} onClick={() => save.mutate(draft)}>{save.isPending ? 'Saving…' : 'Save limits'}</button></div>
  </section>;
}
