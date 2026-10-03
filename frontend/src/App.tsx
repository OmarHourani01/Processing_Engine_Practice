import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, api } from './api';
import { useAppStore } from './store';
import type { Dataset, FeatureCollection, ImageRecord, Job } from './types';
import { AuthScreen } from './components/AuthScreen';
import { UploadDialog } from './components/UploadDialog';
import { DatasetMap } from './components/DatasetMap';
import { ImageDetail } from './components/ImageDetail';
import { JobList } from './components/JobList';
import { WorkerScaling } from './components/WorkerScaling';
import { useJobsQuery } from './queries';

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : 'Something went wrong. Please try again.';
}

export default function App() {
  const queryClient = useQueryClient();
  const { activeView, selectedDataset, selectedImage, setDataset, setImage, setView } = useAppStore();
  const me = useQuery({ queryKey: ['me'], queryFn: api.me, retry: false });
  const datasetsQuery = useQuery({ queryKey: ['datasets'], queryFn: api.datasets, enabled: !!me.data });
  const [showUpload, setShowUpload] = useState(false);
  const [startWithNewDataset, setStartWithNewDataset] = useState(false);
  const [page, setPage] = useState(1);
  const [jobPage, setJobPage] = useState(1);
  const [actionError, setActionError] = useState('');

  useEffect(() => {
    if (!selectedDataset && datasetsQuery.data?.length) setDataset(datasetsQuery.data[0]);
    if (selectedDataset && datasetsQuery.data) {
      const refreshed = datasetsQuery.data.find((dataset) => dataset.id === selectedDataset.id);
      if (refreshed && (refreshed.name !== selectedDataset.name || refreshed.image_count !== selectedDataset.image_count)) setDataset(refreshed);
    }
  }, [datasetsQuery.data, selectedDataset, setDataset]);
  useEffect(() => { setPage(1); }, [selectedDataset?.id]);
  useEffect(() => { setJobPage(1); }, [selectedDataset?.id]);

  const imagesQuery = useQuery({
    queryKey: ['images', selectedDataset?.id, page],
    queryFn: () => api.images(selectedDataset!.id, page, false),
    enabled: !!selectedDataset,
  });
  const mapQuery = useQuery({
    queryKey: ['map', selectedDataset?.id],
    queryFn: () => api.map(selectedDataset!.id),
    enabled: !!selectedDataset,
    refetchInterval: 12_000,
  });
  const jobsQuery = useJobsQuery(
    activeView === 'jobs' ? undefined : selectedDataset?.id,
    jobPage,
    !!me.data && activeView !== 'scaling',
    activeView === 'jobs' ? 'ingest_dataset' : undefined,
  );
  const imageQuery = useQuery({
    queryKey: ['image', selectedImage?.id],
    queryFn: () => api.image(selectedImage!.id),
    enabled: !!selectedImage,
  });
  const logout = useMutation({
    mutationFn: api.logout,
    onSuccess: async () => {
      setDataset(null);
      setImage(null);
      await queryClient.resetQueries({ queryKey: ['me'] });
      queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== 'me' });
    },
  });

  const images = imagesQuery.data?.items || [];
  const unplacedImages = useMemo(() => images.filter((image) => !coordinatesOf(image)), [images]);
  const jobs = jobsQuery.data?.items || [];
  const activeJobs = jobs.filter((job) => ['queued', 'running'].includes(job.status));
  const hadActiveJobs = useRef(false);
  useEffect(() => {
    const active = activeJobs.length > 0;
    if (hadActiveJobs.current && !active) {
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ['datasets'] }),
        queryClient.invalidateQueries({ queryKey: ['images', selectedDataset?.id] }),
        queryClient.invalidateQueries({ queryKey: ['map', selectedDataset?.id] }),
      ]);
    }
    hadActiveJobs.current = active;
    if (!active) return;
    const interval = window.setInterval(() => {
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ['datasets'] }),
        queryClient.invalidateQueries({ queryKey: ['images', selectedDataset?.id] }),
        queryClient.invalidateQueries({ queryKey: ['map', selectedDataset?.id] }),
      ]);
    }, 5_000);
    return () => window.clearInterval(interval);
  }, [activeJobs.length, queryClient, selectedDataset?.id]);
  const displayName = me.data?.username || 'Your workspace';

  if (me.isPending) return <div className="boot-screen"><div className="brand-mark">F</div><span>Preparing your workspace…</span></div>;
  if (!me.data) return <AuthScreen onAuthenticated={() => queryClient.invalidateQueries({ queryKey: ['me'] })} />;

  const afterUpload = async (dataset: Dataset, job?: Job) => {
    setDataset(dataset);
    setShowUpload(false);
    setStartWithNewDataset(false);
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['datasets'] }),
      queryClient.invalidateQueries({ queryKey: ['images', dataset.id] }),
      queryClient.invalidateQueries({ queryKey: ['map', dataset.id] }),
      queryClient.invalidateQueries({ queryKey: ['jobs'] }),
    ]);
    if (job) setView('jobs');
  };

  const openNewDataset = () => {
    setStartWithNewDataset(true);
    setShowUpload(true);
  };
  const openImageUpload = () => {
    setStartWithNewDataset(false);
    setShowUpload(true);
  };

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#home" aria-label="Image Processing Engine home"><span className="brand-mark">F</span><span>Image Processing Engine<span className="brand-dot">.</span></span></a>
        <div className="workspace-label">WORKSPACE</div>
        <button className={`nav-item ${activeView === 'explore' ? 'active' : ''}`} onClick={() => setView('explore')}><span className="nav-icon">◉</span> Explore</button>
        <button className={`nav-item ${activeView === 'jobs' ? 'active' : ''}`} onClick={() => setView('jobs')}><span className="nav-icon">◷</span> Processing jobs {activeJobs.length > 0 && <span className="nav-count">{activeJobs.length}</span>}</button>
        {me.data.local_scaling_available && <button className={`nav-item ${activeView === 'scaling' ? 'active' : ''}`} onClick={() => setView('scaling')}><span className="nav-icon">⌁</span> Worker scaling</button>}
        <div className="sidebar-heading"><span>DATASETS</span><button className="icon-button small" aria-label="Create dataset" onClick={openNewDataset}>＋</button></div>
        <div className="dataset-list">
          {datasetsQuery.data?.map((dataset) => (
            <button key={dataset.id} className={`dataset-nav ${selectedDataset?.id === dataset.id ? 'selected' : ''}`} onClick={() => { setDataset(dataset); setView('explore'); }}>
              <span className="dataset-symbol">▦</span><span className="dataset-nav-name">{dataset.name}</span><span className="dataset-nav-count">{dataset.image_count ?? 0}</span>
            </button>
          ))}
          {!datasetsQuery.data?.length && <p className="sidebar-empty">Your datasets will appear here.</p>}
        </div>
        <div className="sidebar-bottom">
          <div className="avatar">{displayName.slice(0, 1).toUpperCase()}</div>
          <div className="account-label"><strong>{displayName}</strong><span>Personal workspace</span></div>
          <button className="icon-button logout-button" aria-label="Log out" title="Log out" onClick={() => logout.mutate()} disabled={logout.isPending}>↗</button>
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumb"><span>Workspace</span><span className="crumb-separator">/</span><strong>{activeView === 'jobs' ? 'Processing jobs' : activeView === 'scaling' ? 'Worker scaling' : selectedDataset?.name || 'Overview'}</strong></div>
          <div className="top-actions">
            <span className="system-status"><i /> All systems operational</span>
            <button className="button button-primary button-sm" onClick={openNewDataset}><span>＋</span> New dataset</button>
          </div>
        </header>
        {activeView === 'scaling' && me.data.local_scaling_available ? (
          <WorkerScaling />
        ) : activeView === 'jobs' ? (
          <JobList jobs={jobs} count={jobsQuery.data?.count} hasNext={!!jobsQuery.data?.next} hasPrevious={!!jobsQuery.data?.previous} page={jobPage} onPageChange={setJobPage} isLoading={jobsQuery.isLoading} error={jobsQuery.error ? errorText(jobsQuery.error) : undefined} onRetry={async (job) => {
            if (!job.image_id) return;
            await api.retryJob(job.kind || job.type || 'extract_metadata', job.image_id, job.parent_id);
            await queryClient.invalidateQueries({ queryKey: ['jobs'] });
          }} onRetryFailed={async (job) => {
            const result = await api.retryFailedBatch(job.id);
            await queryClient.invalidateQueries({ queryKey: ['jobs'] });
            return result.retried_count;
          }} onCancelBatch={async (job) => {
            await api.cancelUploadSession(job.id);
            await Promise.all([
              queryClient.invalidateQueries({ queryKey: ['jobs'] }),
              queryClient.invalidateQueries({ queryKey: ['datasets'] }),
              queryClient.invalidateQueries({ queryKey: ['images', job.dataset_id] }),
              queryClient.invalidateQueries({ queryKey: ['map', job.dataset_id] }),
            ]);
          }} onDeleteBatch={async (job) => {
            await api.deleteUploadSession(job.id);
            setActionError('');
            await Promise.all([
              queryClient.invalidateQueries({ queryKey: ['jobs'] }),
              queryClient.invalidateQueries({ queryKey: ['datasets'] }),
              queryClient.invalidateQueries({ queryKey: ['images', job.dataset_id] }),
              queryClient.invalidateQueries({ queryKey: ['map', job.dataset_id] }),
            ]);
          }} />
        ) : selectedDataset ? (
          <section className="content-area">
            <div className="page-title-row">
              <div><div className="eyebrow">DATASET</div><h1>{selectedDataset.name}</h1><p className="page-subtitle">Images captured across the field, organized in one place.</p></div>
              <div className="title-actions"><span className="dataset-date">{selectedDataset.created_at ? `Created ${formatDate(selectedDataset.created_at)}` : 'Private dataset'}</span><button className="button button-danger" onClick={async () => {
                if (!window.confirm(`Delete dataset “${selectedDataset.name}” and all its pictures and upload sessions? This cannot be undone.`)) return;
                try {
                  await api.deleteDataset(selectedDataset.id);
                  setActionError('');
                  setDataset(null);
                  setImage(null);
                  await Promise.all([
                    queryClient.invalidateQueries({ queryKey: ['datasets'] }),
                    queryClient.invalidateQueries({ queryKey: ['jobs'] }),
                    queryClient.invalidateQueries({ queryKey: ['images', selectedDataset.id] }),
                    queryClient.invalidateQueries({ queryKey: ['map', selectedDataset.id] }),
                  ]);
                } catch (error) {
                  setActionError(errorText(error));
                }
              }}>Delete dataset</button><button className="button button-secondary" onClick={openImageUpload}>＋ Upload images</button></div>
            </div>
            <div className="stats-row">
              <StatCard label="Images" value={selectedDataset.image_count ?? 0} icon="▧" />
              <StatCard label="Located" value={selectedDataset.geotagged_count ?? countLocated(mapQuery.data)} icon="⌖" tone="green" />
              <StatCard label="In queue" value={activeJobs.reduce((sum, job) => sum + (job.total_count ?? job.total ?? 1) - (job.completed_count ?? job.completed ?? 0), 0)} icon="◷" tone="amber" />
              <StatCard label="Latest activity" value={jobs[0] ? statusLabel(jobs[0].status) : 'No activity'} icon="✳" tone="blue" compact />
            </div>
            <div className="explore-grid">
              <div className="map-panel panel">
                <div className="panel-header"><div><h2>Image map</h2><p>Explore images with location data</p></div><span className="map-count"><i /> {mapQuery.data?.features.length || 0} places</span></div>
                <DatasetMap datasetId={selectedDataset.id} data={mapQuery.data} onSelectId={(id) => setImage({ id })} />
              </div>
              <div className="gallery-panel panel">
                <div className="panel-header gallery-header"><div><h2>Without location</h2><p>Images without GPS information</p></div><span className="count-pill">{selectedDataset.image_count == null ? unplacedImages.length : Math.max(0, (selectedDataset.image_count || 0) - (selectedDataset.geotagged_count ?? countLocated(mapQuery.data)))}</span></div>
                <div className="gallery-content">
                  {imagesQuery.isLoading ? <div className="inline-loading">Loading images…</div> : imagesQuery.error ? <InlineError message={errorText(imagesQuery.error)} onRetry={() => imagesQuery.refetch()} /> : unplacedImages.length ? (
                    <div className="image-grid">{unplacedImages.map((image) => <ImageTile key={image.id} image={image} onClick={() => setImage(image)} />)}</div>
                  ) : <div className="empty-gallery"><span className="empty-icon">⌖</span><strong>All caught up</strong><p>Images without GPS metadata will show up here.</p></div>}
                </div>
                {imagesQuery.data && <div className="pagination"><button className="text-button" disabled={page <= 1} onClick={() => setPage((n) => Math.max(1, n - 1))}>← Previous</button><span>Page {page}</span><button className="text-button" disabled={!imagesQuery.data.next} onClick={() => setPage((n) => n + 1)}>Next →</button></div>}
              </div>
            </div>
            {activeJobs.length > 0 && <div className="active-job-strip"><div className="spinner"/><div><strong>Processing {activeJobs.length === 1 ? 'dataset' : `${activeJobs.length} jobs`}</strong><span>{activeJobs.map((job) => `${job.completed_count ?? job.completed ?? 0}/${job.total_count ?? job.total ?? '…'} complete`).join(' · ')}</span></div><button className="text-button" onClick={() => setView('jobs')}>View jobs →</button></div>}
          </section>
        ) : (
          <section className="welcome content-area"><div className="welcome-orb">✦</div><div className="eyebrow">IMAGE WORKSPACE</div><h1>Make sense of<br />your field images.</h1><p>Upload a folder or ZIP and let Image Processing Engine organize image metadata, locations, and previews for you.</p><button className="button button-primary" onClick={openNewDataset}>Create your first dataset <span>→</span></button><div className="welcome-note"><span>⌁</span> Private by default · Your data stays in your workspace</div></section>
        )}
      </main>
      {showUpload && <UploadDialog datasets={datasetsQuery.data || []} selectedDataset={selectedDataset} createNew={startWithNewDataset} onClose={() => setShowUpload(false)} onComplete={afterUpload} />}
      {selectedImage && <ImageDetail image={imageQuery.data || selectedImage} loading={imageQuery.isLoading} onClose={() => setImage(null)} onDelete={async (image) => {
        const name = image.relative_path || `Image ${image.id}`;
        if (!window.confirm(`Delete picture “${name}” and its stored files? This cannot be undone.`)) return false;
        try {
          await api.deleteImage(image.id);
          setActionError('');
          await Promise.all([
            queryClient.invalidateQueries({ queryKey: ['datasets'] }),
            queryClient.invalidateQueries({ queryKey: ['images', image.dataset_id] }),
            queryClient.invalidateQueries({ queryKey: ['map', image.dataset_id] }),
            queryClient.invalidateQueries({ queryKey: ['jobs'] }),
          ]);
          return true;
        } catch (error) {
          setActionError(errorText(error));
          throw error;
        }
      }} />}
      {actionError && <div className="toast-error">{actionError}</div>}
      {datasetsQuery.error && <div className="toast-error">Could not load datasets: {errorText(datasetsQuery.error)}</div>}
    </div>
  );
}

function StatCard({ label, value, icon, tone = 'slate', compact = false }: { label: string; value: string | number; icon: string; tone?: string; compact?: boolean }) {
  return <div className="stat-card"><span className={`stat-icon ${tone}`}>{icon}</span><div className="stat-copy"><span>{label}</span><strong className={compact ? 'stat-compact' : ''}>{value}</strong></div></div>;
}

function ImageTile({ image, onClick }: { image: ImageRecord; onClick: () => void }) {
  const src = image.thumbnail_url || image.preview_url;
  return <button className="image-tile" onClick={onClick} aria-label={`View ${image.relative_path || image.filename || 'image'}`}>
    {src ? <img src={src} alt="" loading="lazy" /> : <div className="image-placeholder">▧</div>}
    <span className="tile-overlay"><strong>{image.relative_path?.split('/').pop() || image.filename || 'Image'}</strong><small>{image.width && image.height ? `${image.width} × ${image.height}` : image.status || 'Preview'}</small></span>
  </button>;
}

function InlineError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return <div className="inline-error"><span>{message}</span><button className="text-button" onClick={onRetry}>Retry</button></div>;
}

function countLocated(data?: FeatureCollection): number { return data?.features?.filter((feature) => feature.geometry?.type === 'Point').length || 0; }

export function coordinatesOf(image: ImageRecord): [number, number] | undefined {
  if (image.location?.coordinates?.length === 2) return image.location.coordinates;
  if (typeof image.longitude === 'number' && typeof image.latitude === 'number') return [image.longitude, image.latitude];
  return undefined;
}

function statusLabel(status: string): string { return status.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()); }
function formatDate(value: string): string { return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric' }).format(new Date(value)); }
