import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api';
import type { Job } from '../types';

type Filter = 'all' | 'active' | 'failed' | 'cancelled' | 'complete';
type FileBatch = { key: string; path: string; jobs: Job[]; error?: string };

const IMAGE_TASKS = [
  ['extract_metadata', 'Metadata'],
  ['generate_thumbnail', 'Preview'],
] as const;
const TERMINAL = ['succeeded', 'succeeded_with_errors', 'failed'];

export function JobList({ jobs, count, hasNext, hasPrevious, page, onPageChange, isLoading, error, onRetry, onRetryFailed, onCancelBatch, onDeleteBatch }: { jobs: Job[]; count?: number; hasNext: boolean; hasPrevious: boolean; page: number; onPageChange: (page: number) => void; isLoading: boolean; error?: string; onRetry: (job: Job) => void; onRetryFailed?: (job: Job) => Promise<number>; onCancelBatch?: (job: Job) => Promise<void>; onDeleteBatch?: (job: Job) => Promise<void> }) {
  const [filter, setFilter] = useState<Filter>('all');
  const filtered = useMemo(() => jobs.filter((job) => {
    if (filter === 'active') return ['queued', 'running'].includes(job.status);
    if (filter === 'failed') return ['failed', 'succeeded_with_errors'].includes(job.status);
    if (filter === 'cancelled') return job.status === 'cancelled';
    if (filter === 'complete') return job.status === 'succeeded';
    return true;
  }), [jobs, filter]);
  const activeCount = jobs.filter((job) => ['queued', 'running'].includes(job.status)).length;

  return <section className="jobs-page content-area">
    <div className="page-title-row"><div><div className="eyebrow">ACTIVITY</div><h1>Processing jobs</h1><p className="page-subtitle">Track each upload session and the progress of its files.</p></div><div className="jobs-live"><i /> {activeCount} active</div></div>
    <div className="jobs-toolbar"><div className="filter-tabs" role="tablist" aria-label="Filter upload sessions">{(['all', 'active', 'failed', 'cancelled', 'complete'] as Filter[]).map((value) => <button key={value} role="tab" aria-selected={filter === value} className={filter === value ? 'selected' : ''} onClick={() => setFilter(value)}>{filterLabel(value)}{value === 'active' && activeCount > 0 && <span>{activeCount}</span>}</button>)}</div><div className="jobs-total">{count ?? jobs.length} total {(count ?? jobs.length) === 1 ? 'session' : 'sessions'}</div></div>
    <div className="jobs-card panel">
      {error ? <div className="jobs-empty"><span>!</span><strong>Could not load upload sessions</strong><p>{error}</p></div> : isLoading ? <div className="jobs-empty">Loading upload sessions…</div> : !filtered.length ? <div className="jobs-empty"><span>◷</span><strong>{filter === 'all' ? 'No upload sessions yet' : `No ${filterLabel(filter).toLowerCase()} sessions`}</strong><p>When you process an upload, its batch progress will appear here.</p></div> : <div className="job-table">
        <div className="job-table-head"><span>UPLOAD SESSION</span><span>STATUS</span><span>PROGRESS</span><span>ATTEMPTS</span><span>UPDATED</span><span /></div>
        {filtered.map((job) => job.kind === 'ingest_dataset'
          ? <BatchRow key={job.id} job={job} onRetry={onRetry} onRetryFailed={onRetryFailed} onCancelBatch={onCancelBatch} onDeleteBatch={onDeleteBatch} />
          : <JobRow key={job.id} job={job} onRetry={() => onRetry(job)} />)}
      </div>}
      {!isLoading && !error && (hasNext || hasPrevious) && <div className="job-pagination"><button className="button button-secondary button-sm" disabled={!hasPrevious} onClick={() => onPageChange(Math.max(1, page - 1))}>← Previous</button><span>Page {page}</span><button className="button button-secondary button-sm" disabled={!hasNext} onClick={() => onPageChange(page + 1)}>Next →</button></div>}
    </div>
    <p className="jobs-footnote"><span>↻</span> Active sessions refresh automatically. Expand a session to see each file.</p>
  </section>;
}

function BatchRow({ job, onRetry, onRetryFailed, onCancelBatch, onDeleteBatch }: { job: Job; onRetry: (job: Job) => void; onRetryFailed?: (job: Job) => Promise<number>; onCancelBatch?: (job: Job) => Promise<void>; onDeleteBatch?: (job: Job) => Promise<void> }) {
  const [expanded, setExpanded] = useState(false);
  const total = batchFileTotal(job, 0);
  const terminal = ['succeeded', 'succeeded_with_errors', 'failed'].includes(job.status);
  const finished = terminal ? total : Math.min((job.completed_count || 0) + (job.failed_count || 0), total);
  const percent = total ? Math.min(100, Math.round(finished / total * 100)) : terminal ? 100 : 0;

  return <div className={`batch-entry ${expanded ? 'expanded' : ''}`}>
    <button className="batch-summary" type="button" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
      <div className="job-name"><span className={`job-kind-icon ${job.status}`}>{job.status === 'succeeded' ? '✓' : job.status === 'cancelled' ? 'Ⅱ' : job.status === 'failed' || job.status === 'succeeded_with_errors' ? '!' : '◌'}</span><div><strong>{job.dataset_name || 'Dataset'} upload session</strong><span>{total} {total === 1 ? 'file' : 'files'} · Started {job.created_at ? relativeTime(job.created_at) : 'recently'}</span></div></div>
      <div><span className={`status-pill ${job.status}`}>{job.status.replaceAll('_', ' ')}</span></div>
      <div className="job-progress-cell"><div className="job-progress-label"><span>{finished} / {total} files</span><span>{percent}%</span></div><div className="progress-track"><i className={(job.failed_count || job.status === 'failed' || job.status === 'succeeded_with_errors') ? 'has-failures' : ''} style={{ width: `${percent}%` }} /></div>{job.failed_count ? <small>{job.failed_count} failed</small> : null}</div>
      <span className="job-attempts">{job.attempt_count ?? 0}</span>
      <span className="job-updated">{job.updated_at || job.finished_at || job.created_at ? relativeTime(job.updated_at || job.finished_at || job.created_at!) : '—'}</span>
      <span className="batch-chevron" aria-hidden="true">{expanded ? '⌃' : '⌄'}</span>
    </button>
    {expanded && <BatchDetails job={job} onRetry={onRetry} onRetryFailed={onRetryFailed} onCancelBatch={onCancelBatch} onDeleteBatch={onDeleteBatch} />}
  </div>;
}

function BatchDetails({ job, onRetry, onRetryFailed, onCancelBatch, onDeleteBatch }: { job: Job; onRetry: (job: Job) => void; onRetryFailed?: (job: Job) => Promise<number>; onCancelBatch?: (job: Job) => Promise<void>; onDeleteBatch?: (job: Job) => Promise<void> }) {
  const [operation, setOperation] = useState<'retry' | 'cancel' | 'delete' | null>(null);
  const [operationMessage, setOperationMessage] = useState('');
  const active = ['queued', 'running'].includes(job.status);
  const childrenQuery = useQuery({
    queryKey: ['jobs', 'batch', job.id],
    queryFn: () => api.allJobs({ parentJob: job.id }),
    refetchInterval: active ? 2_500 : false,
  });
  const files = useMemo(() => groupFiles(job, childrenQuery.data || []), [job, childrenQuery.data]);
  const total = batchFileTotal(job, files.length);
  const finished = files.filter(isFileFinished).length;
  const failedTaskCount = files.reduce((count, file) => count + Object.values(latestByKind(file.jobs)).filter((task) => task?.status === 'failed' && task.image_id).length, 0);
  const retryableCount = Math.max(failedTaskCount, job.failed_count || 0);

  async function cancelProcessing() {
    if (!onCancelBatch) return;
    if (!window.confirm(`Cancel processing for ${job.dataset_name || 'this upload session'}? Completed pictures will remain, but this session cannot be resumed.`)) return;
    setOperation('cancel');
    setOperationMessage('');
    try {
      await onCancelBatch(job);
      setOperationMessage('Cancellation requested. Running tasks are stopping.');
    } catch (error) {
      setOperationMessage(error instanceof Error ? error.message : 'Could not cancel this upload session.');
    } finally {
      setOperation(null);
    }
  }

  async function retryFailed() {
    if (!onRetryFailed) return;
    setOperation('retry');
    setOperationMessage('');
    try {
      const count = await onRetryFailed(job);
      setOperationMessage(count ? `Queued ${count} failed task${count === 1 ? '' : 's'} for retry.` : 'No failed image tasks to retry.');
    } catch (error) {
      setOperationMessage(error instanceof Error ? error.message : 'Could not retry failed tasks.');
    } finally {
      setOperation(null);
    }
  }

  async function deleteBatch() {
    if (!onDeleteBatch || active) return;
    const name = job.dataset_name || 'this dataset';
    if (!window.confirm(`Delete the ${name} upload session and its pictures? This cannot be undone.`)) return;
    setOperation('delete');
    setOperationMessage('');
    try {
      await onDeleteBatch(job);
    } catch (error) {
      setOperationMessage(error instanceof Error ? error.message : 'Could not delete this upload session.');
    } finally {
      setOperation(null);
    }
  }

  return <div className="batch-details">
    <div className="batch-detail-heading"><div className="batch-detail-summary"><strong>Files in this session</strong><span>{files.length} shown · {finished} complete of {total}</span></div><div className="batch-detail-actions">
      {active && onCancelBatch && <button className="button button-danger button-sm" onClick={() => void cancelProcessing()} disabled={operation !== null}>{operation === 'cancel' ? 'Cancelling…' : 'Cancel processing'}</button>}
      {job.status !== 'cancelled' && retryableCount > 0 && onRetryFailed && <button className="button button-secondary button-sm" onClick={() => void retryFailed()} disabled={operation !== null}>{operation === 'retry' ? 'Retrying…' : `Retry failed (${retryableCount})`}</button>}
      {onDeleteBatch && <button className="button button-danger button-sm" onClick={() => void deleteBatch()} disabled={active || operation !== null} title={active ? 'Wait for this upload session to finish processing.' : 'Delete this upload session and its pictures'}>{operation === 'delete' ? 'Deleting…' : 'Delete upload session'}</button>}
    </div></div>
    {operationMessage && <div className={`batch-message ${operationMessage.startsWith('Could not') ? 'error' : ''}`}>{operationMessage}</div>}
    {childrenQuery.isLoading ? <div className="batch-message">Loading file progress…</div> : childrenQuery.error ? <div className="batch-message error">Could not load file progress. {childrenQuery.error instanceof Error ? childrenQuery.error.message : ''}</div> : !files.length ? <div className="batch-message">File details will appear when ingestion starts.</div> : <div className="batch-file-list">
      {files.map((file) => <FileRow key={file.key} file={file} onRetry={onRetry} allowRetry={job.status !== 'cancelled'} />)}
    </div>}
    {job.errors?.length ? <div className="batch-errors"><strong>Session notes</strong>{job.errors.map((item, index) => <span key={index}>{errorText(item)}</span>)}</div> : null}
  </div>;
}

function FileRow({ file, onRetry, allowRetry }: { file: FileBatch; onRetry: (job: Job) => void; allowRetry: boolean }) {
  const latestJobs = latestByKind(file.jobs);
  const taskJobs = IMAGE_TASKS.map(([kind]) => latestJobs[kind]).filter((job): job is Job => !!job);
  const completeTasks = taskJobs.filter((job) => TERMINAL.includes(job.status)).length;
  const percent = file.error ? 100 : Math.round(completeTasks / IMAGE_TASKS.length * 100);
  const failed = !!file.error || taskJobs.some((job) => job.status === 'failed');
  const cancelled = !file.error && taskJobs.some((job) => job.status === 'cancelled');
  const finished = !!file.error || IMAGE_TASKS.every(([kind]) => latestJobs[kind] && TERMINAL.includes(latestJobs[kind]!.status));
  return <div className="batch-file-row">
    <div className="batch-file-name"><strong title={file.path}>{file.path}</strong><span className={file.error || failed ? 'failed' : ''}>{file.error || (cancelled ? 'Processing cancelled' : finished ? 'File processing complete' : `${completeTasks} of ${IMAGE_TASKS.length} steps complete`)}</span></div>
    <div className="file-task-list">{IMAGE_TASKS.map(([kind, label]) => {
      const task = latestJobs[kind];
      const taskStatus = task?.status || 'queued';
      return <div className="file-task" key={kind}><span>{label}</span><span className={`mini-status ${taskStatus}`}>{taskStatus.replaceAll('_', ' ')}</span>{allowRetry && task?.status === 'failed' && task.image_id && <button className="text-button retry-link" onClick={() => onRetry(task)}>Retry</button>}</div>;
    })}</div>
    <div className="file-progress"><div className="job-progress-label"><span>{file.error ? 'Import failed' : `${completeTasks} / ${IMAGE_TASKS.length} steps`}</span><span>{percent}%</span></div><div className="progress-track"><i className={failed ? 'has-failures' : ''} style={{ width: `${percent}%` }} /></div></div>
  </div>;
}

function JobRow({ job, onRetry }: { job: Job; onRetry: () => void }) {
  const done = job.completed_count ?? job.completed ?? 0;
  const total = job.total_count ?? job.total ?? 0;
  const failed = job.failed_count ?? job.failed ?? 0;
  const percent = job.progress ?? (total ? Math.round(done / total * 100) : ['succeeded', 'succeeded_with_errors'].includes(job.status) ? 100 : 0);
  const title = job.kind?.replaceAll('_', ' ') || job.type?.replaceAll('_', ' ') || 'Processing job';
  const retryable = job.status === 'failed' && job.image_id != null && /extract_metadata|generate_thumbnail/.test(job.kind || job.type || '');
  const firstError = job.error || (job.errors?.[0] ? errorText(job.errors[0]) : undefined);
  const fileLabel = job.image_path || (job.image_id ? `Image ${job.image_id}` : `Job ${String(job.id).slice(0, 8)}`);
  return <div className="job-row">
    <div className="job-name"><span className={`job-kind-icon ${job.status}`}>{job.status === 'succeeded' ? '✓' : job.status === 'failed' || job.status === 'succeeded_with_errors' ? '!' : '◌'}</span><div><strong>{title}</strong><span title={fileLabel}>{fileLabel}</span>{firstError && <span className="job-error" title={firstError}>{firstError}</span>}</div></div>
    <div><span className={`status-pill ${job.status}`}>{job.status.replaceAll('_', ' ')}</span></div>
    <div className="job-progress-cell"><div className="job-progress-label"><span>{total ? `${done} / ${total}` : job.status === 'running' ? 'Working…' : '—'}</span><span>{total || job.progress != null ? `${percent}%` : ''}</span></div><div className="progress-track"><i className={failed ? 'has-failures' : ''} style={{ width: `${Math.max(0, Math.min(100, percent))}%` }} /></div>{failed > 0 && <small>{failed} failed</small>}</div>
    <span className="job-attempts">{job.attempt_count ?? 0}</span>
    <span className="job-updated">{job.updated_at || job.finished_at || job.created_at ? relativeTime(job.updated_at || job.finished_at || job.created_at!) : '—'}</span>
    {retryable ? <button className="text-button retry-link" onClick={onRetry}>Retry</button> : <span />}
  </div>;
}

function groupFiles(batch: Job, jobs: Job[]): FileBatch[] {
  const grouped = new Map<string, Job[]>();
  for (const job of jobs) {
    const key = job.image_id ? `image:${job.image_id}` : `path:${job.image_path || job.id}`;
    grouped.set(key, [...(grouped.get(key) || []), job]);
  }
  const files: FileBatch[] = [...grouped.entries()].map(([key, items]) => ({
    key,
    path: items.find((job) => job.image_path)?.image_path || (items[0]?.image_id ? `Image ${items[0].image_id}` : 'Image file'),
    jobs: items,
  }));
  for (const [index, item] of (batch.errors || []).entries()) {
    if (!item || typeof item !== 'object') continue;
    const path = typeof item.path === 'string' ? item.path : undefined;
    if (!path || files.some((file) => file.path === path)) continue;
    files.push({ key: `error:${index}:${path}`, path, jobs: [], error: errorText(item) });
  }
  return files.sort((a, b) => a.path.localeCompare(b.path));
}

function latestByKind(jobs: Job[]): Partial<Record<(typeof IMAGE_TASKS)[number][0], Job>> {
  const latest: Partial<Record<(typeof IMAGE_TASKS)[number][0], Job>> = {};
  const ordered = [...jobs].sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
  for (const job of ordered) {
    if (IMAGE_TASKS.some(([kind]) => kind === job.kind) && job.kind && !latest[job.kind as (typeof IMAGE_TASKS)[number][0]]) {
      latest[job.kind as (typeof IMAGE_TASKS)[number][0]] = job;
    }
  }
  return latest;
}

function isFileFinished(file: FileBatch): boolean {
  if (file.error) return true;
  const latest = latestByKind(file.jobs);
  return IMAGE_TASKS.every(([kind]) => latest[kind] && TERMINAL.includes(latest[kind]!.status));
}

function batchFileTotal(job: Job, fileCount: number): number {
  const result = job.result || {};
  const input = job.input || {};
  const inputAssets = Array.isArray(input.upload_asset_ids) ? input.upload_asset_ids.length : 0;
  const created = typeof result.images_created === 'number' ? result.images_created : undefined;
  const ingestErrors = typeof result.ingest_failed_count === 'number' ? result.ingest_failed_count : 0;
  return Math.max(fileCount, created === undefined ? inputAssets || job.total_count || 0 : created + ingestErrors);
}

function errorText(item: string | Record<string, unknown>): string {
  if (typeof item === 'string') return item;
  const detail = item.message || item.error || item.detail;
  return typeof detail === 'string' ? detail : 'This file could not be processed.';
}

function filterLabel(filter: Filter): string { return ({ all: 'All', active: 'Active', failed: 'Failed', cancelled: 'Cancelled', complete: 'Complete' })[filter]; }
function relativeTime(date: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(date).getTime()) / 1000);
  if (seconds < 60) return 'Just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' }).format(new Date(date));
}
