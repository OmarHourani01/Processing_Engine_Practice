import { ChangeEvent, DragEvent, useEffect, useMemo, useRef, useState } from 'react';
import { abortMultipartUpload, api, uploadToMinio } from '../api';
import type { Dataset, Job, MultipartUploadReceipt, UploadFailure, UploadTarget } from '../types';

const MAX_FILES = 1_000;
const MAX_BYTES = 10_737_418_240;
const ACCEPTED = /\.(jpe?g|png|webp|tif|tiff|geotiff)$/i;
const ARCHIVE = /\.zip$/i;

interface FileRow extends UploadFailure {
  key: string;
  status: 'ready' | 'uploading' | 'uploaded' | 'failed';
  progress: number;
  uploadId?: string | number;
  retryable: boolean;
}

export function UploadDialog({ datasets, selectedDataset, createNew = false, onClose, onComplete }: {
  datasets: Dataset[];
  selectedDataset: Dataset | null;
  createNew?: boolean;
  onClose: () => void;
  onComplete: (dataset: Dataset, job?: Job) => void;
}) {
  const [destination, setDestination] = useState<string>(createNew ? 'new' : selectedDataset?.id ? String(selectedDataset.id) : 'new');
  const [datasetName, setDatasetName] = useState('');
  const [rows, setRows] = useState<FileRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [message, setMessage] = useState('');
  const [uploadHadFailures, setUploadHadFailures] = useState(false);
  const [confirmedIds, setConfirmedIds] = useState<Array<string | number>>([]);
  const [activeDataset, setActiveDataset] = useState<Dataset | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    folderInputRef.current?.setAttribute('webkitdirectory', '');
    folderInputRef.current?.setAttribute('directory', '');
  }, []);

  const totalBytes = useMemo(() => rows.reduce((sum, row) => sum + row.file.size, 0), [rows]);
  const uploadedBytes = rows.reduce((sum, row) => sum + (row.status === 'uploaded' ? row.file.size : row.status === 'uploading' ? row.file.size * row.progress : 0), 0);
  const uploadedCount = rows.filter((row) => row.status === 'uploaded').length;
  const failures = rows.filter((row) => row.status === 'failed');
  const hasErrors = failures.length > 0;

  function addFiles(fileList: FileList | File[]) {
    if (confirmedIds.length > 0) {
      setMessage('Finish processing the confirmed files before starting another upload batch.');
      return;
    }
    const incoming = Array.from(fileList);
    const baseCount = rows.filter((row) => row.retryable || row.status === 'uploaded').length;
    const incomingBytes = incoming.reduce((sum, file) => sum + file.size, 0);
    const overCount = baseCount + incoming.length > MAX_FILES;
    const overSize = totalBytes + incomingBytes > MAX_BYTES;
    const newRows = incoming.map((file, index): FileRow => {
      const path = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
      const supported = ACCEPTED.test(path || file.name) || ARCHIVE.test(path || file.name);
      let error = '';
      if (!supported) error = 'Unsupported format. Use JPEG, PNG, WebP, TIFF/GeoTIFF, or ZIP.';
      else if (overCount) error = `This dataset supports up to ${MAX_FILES.toLocaleString()} files.`;
      else if (overSize) error = 'This upload would exceed the 10 GB dataset limit.';
      return {
        key: `${Date.now()}-${index}-${Math.random().toString(16).slice(2)}`,
        file,
        message: error,
        status: error ? 'failed' : 'ready',
        progress: 0,
        retryable: !error,
      };
    });
    setRows((existing) => [...existing, ...newRows]);
    setMessage('');
  }

  function onInput(event: ChangeEvent<HTMLInputElement>) {
    if (event.target.files) addFiles(event.target.files);
    event.target.value = '';
  }
  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault(); setDragging(false);
    if (event.dataTransfer.files.length) addFiles(event.dataTransfer.files);
  }

  function updateRow(key: string, values: Partial<FileRow>) {
    setRows((current) => current.map((row) => row.key === key ? { ...row, ...values } : row));
  }

  async function ensureDataset(): Promise<Dataset> {
    if (destination !== 'new') {
      const found = datasets.find((item) => String(item.id) === destination);
      if (found) return found;
    }
    const trimmed = datasetName.trim();
    if (!trimmed) throw new Error('Enter a name for this dataset.');
    const created = await api.createDataset(trimmed);
    setActiveDataset(created);
    setDestination(String(created.id));
    return created;
  }

  async function uploadPending(dataset: Dataset, pending: FileRow[]) {
    const untouchedErrors = rows.some((row) => row.status === 'failed' && !pending.some((item) => item.key === row.key));
    const targets = await api.prepareUploads(dataset.id, pending.map((row) => row.file));
    if (targets.length !== pending.length) throw new Error('The server returned an unexpected number of upload slots. Please retry.');
    const outcomes: Array<{ row: FileRow; target: UploadTarget; multipart?: MultipartUploadReceipt; error?: string }> = new Array(pending.length);
    let cursor = 0;
    const workers = Array.from({ length: Math.min(4, pending.length) }, async () => {
      while (cursor < pending.length) {
        const index = cursor++;
        const row = pending[index];
        const target = targets[index];
        updateRow(row.key, { status: 'uploading', progress: 0, message: '' });
        try {
          const multipart = await uploadToMinio(target, row.file, (progress) => updateRow(row.key, { progress }));
          outcomes[index] = { row, target, multipart };
        } catch (error) {
          outcomes[index] = { row, target, error: error instanceof Error ? error.message : 'Upload failed.' };
        }
      }
    });
    await Promise.all(workers);
    const succeeded = outcomes.filter((item) => !item.error);
    let confirmed = new Set<string | number>();
    if (succeeded.length) {
      try {
        const multipartUploads = succeeded.flatMap(({ target, multipart }) => multipart ? [{ id: target.id, ...multipart }] : []);
        const response = await api.confirmUploads(dataset.id, succeeded.map(({ target }) => target.id), multipartUploads);
        response.uploads.forEach((item) => {
          if (item.confirmed) confirmed.add(item.id);
          else {
            const outcome = succeeded.find((success) => String(success.target.id) === String(item.id));
            if (outcome) {
              outcome.error = item.upload_error || 'The uploaded object could not be verified.';
              void abortMultipartUpload(outcome.target);
            }
          }
        });
      } catch (error) {
        const detail = error instanceof Error ? error.message : 'Upload verification failed.';
        succeeded.forEach((item) => { item.error = detail; void abortMultipartUpload(item.target); });
      }
    }
    for (const item of outcomes) {
      if (!item) continue;
      if (item.error) updateRow(item.row.key, { status: 'failed', message: item.error, progress: item.row.file.size ? item.row.progress : 0, uploadId: undefined, retryable: true });
      else if (confirmed.has(item.target.id)) updateRow(item.row.key, { status: 'uploaded', message: '', progress: 1, uploadId: item.target.id });
      else {
        item.error = 'The server did not confirm this file.';
        updateRow(item.row.key, { status: 'failed', message: item.error, progress: 1, retryable: true });
      }
    }
    const newlyConfirmed = succeeded.filter((item) => confirmed.has(item.target.id)).map((item) => item.target.id);
    setConfirmedIds((current) => Array.from(new Set([...current, ...newlyConfirmed])));
    return { hadErrors: untouchedErrors || outcomes.some((item) => !!item?.error), newlyConfirmed };
  }

  async function processUploaded(dataset: Dataset, ids: Array<string | number>, allowPartial: boolean) {
    if (!ids.length) throw new Error('There are no confirmed files to process yet.');
    const response = await api.processDataset(dataset.id, ids, allowPartial);
    await onComplete(dataset, response.job);
  }

  async function submitUpload() {
    setMessage('');
    const ready = rows.filter((row) => row.status === 'ready');
    if (!ready.length) { setMessage('Choose at least one valid image or ZIP file to upload.'); return; }
    setBusy(true);
    let uploadStarted = false;
    try {
      const dataset = activeDataset || await ensureDataset();
      uploadStarted = true;
      const result = await uploadPending(dataset, ready);
      const failedAfterAttempt = result.hadErrors;
      const ids = Array.from(new Set([...confirmedIds, ...result.newlyConfirmed]));
      if (failedAfterAttempt) {
        setUploadHadFailures(true);
        setMessage('Some files could not be uploaded. Retry them, or process the confirmed files below.');
      } else if (ids.length) await processUploaded(dataset, ids, false);
    } catch (error) {
      const detail = error instanceof Error ? error.message : 'Upload could not be started.';
      if (uploadStarted) {
        setUploadHadFailures(true);
        setRows((current) => current.map((row) => row.status === 'ready' || row.status === 'uploading' ? { ...row, status: 'failed', message: detail, retryable: true } : row));
      }
      setMessage(detail);
    } finally { setBusy(false); }
  }

  async function retryFailures() {
    const retryable = failures.filter((row) => row.retryable);
    if (!retryable.length) return;
    setBusy(true); setMessage('');
    try {
      const dataset = activeDataset || await ensureDataset();
      setRows((current) => current.map((row) => retryable.some((failed) => failed.key === row.key) ? { ...row, status: 'ready', message: '', progress: 0 } : row));
      const result = await uploadPending(dataset, retryable.map((row) => ({ ...row, status: 'ready', message: '' })));
      const ids = Array.from(new Set([...confirmedIds, ...result.newlyConfirmed]));
      const remainingFailures = result.hadErrors;
      if (remainingFailures) {
        setUploadHadFailures(true);
        setMessage('Some files still need attention. You can retry again or process confirmed files.');
      } else if (ids.length) {
        setUploadHadFailures(true);
        setMessage('All retry uploads are confirmed. Review the files, then start processing when ready.');
      }
    } catch (error) {
      const detail = error instanceof Error ? error.message : 'Retry failed.';
      setUploadHadFailures(true);
      setRows((current) => current.map((row) => retryable.some((item) => item.key === row.key) ? { ...row, status: 'failed', message: detail, retryable: true } : row));
      setMessage(detail);
    }
    finally { setBusy(false); }
  }

  async function processConfirmed(allowPartial: boolean) {
    setBusy(true); setMessage('');
    try {
      const dataset = activeDataset || await ensureDataset();
      await processUploaded(dataset, confirmedIds, allowPartial);
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Could not process these files.'); }
    finally { setBusy(false); }
  }

  const progress = totalBytes ? Math.round(uploadedBytes / totalBytes * 100) : 0;

  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <section className="upload-dialog" role="dialog" aria-modal="true" aria-labelledby="upload-title">
      <header className="modal-header"><div><div className="eyebrow">DATA INGESTION</div><h2 id="upload-title">Add images to Image Processing Engine</h2><p>Upload individual images, a folder, or a ZIP archive.</p></div><button className="icon-button close-button" aria-label="Close" onClick={onClose} disabled={busy}>×</button></header>
      <div className="upload-form">
        <label className="field-label" htmlFor="dataset-destination">Dataset</label>
        <select id="dataset-destination" className="select-input" value={destination} onChange={(event) => setDestination(event.target.value)} disabled={busy}>
          <option value="new">Create a new dataset</option>{datasets.map((dataset) => <option key={dataset.id} value={dataset.id}>{dataset.name}</option>)}
        </select>
        {destination === 'new' && <label className="dataset-name-label">Dataset name<input value={datasetName} onChange={(event) => setDatasetName(event.target.value)} placeholder="For example: North Ridge Survey" disabled={busy} maxLength={120} /></label>}
        <div className={`dropzone ${dragging ? 'dragging' : ''}`} onDragOver={(event) => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={onDrop}>
          <input ref={inputRef} className="visually-hidden" type="file" multiple accept=".jpg,.jpeg,.png,.webp,.tif,.tiff,.geotiff,.zip" onChange={onInput} aria-label="Choose image files or ZIP" />
          <input ref={folderInputRef} className="visually-hidden" type="file" multiple accept=".jpg,.jpeg,.png,.webp,.tif,.tiff,.geotiff" onChange={onInput} aria-label="Choose image folder" />
          <div className="drop-icon">↑</div><strong>Drop your files or a folder here</strong><span>JPEG, PNG, WebP, TIFF/GeoTIFF, or ZIP · up to 1,000 files / 10 GB</span>
          <div className="drop-actions"><button className="button button-secondary button-sm" type="button" onClick={() => inputRef.current?.click()} disabled={busy}>Choose files</button><button className="button button-quiet button-sm" type="button" onClick={() => folderInputRef.current?.click()} disabled={busy}>Choose folder</button></div>
        </div>
        {rows.length > 0 && <div className="file-list-wrap">
          <div className="file-list-summary"><strong>{rows.length} {rows.length === 1 ? 'file' : 'files'} selected</strong><span>{formatBytes(totalBytes)}</span><button className="text-button" onClick={() => { setRows([]); setConfirmedIds([]); setMessage(''); }} disabled={busy}>Clear all</button></div>
          {(busy || uploadedCount > 0) && <div className="upload-progress"><div><span>{uploadedCount} of {rows.length} confirmed</span><strong>{progress}%</strong></div><div className="progress-track"><i style={{ width: `${progress}%` }} /></div></div>}
          <div className="file-list">{rows.map((row) => <div className="file-row" key={row.key}>
            <span className={`file-type ${ARCHIVE.test(row.file.name) ? 'zip' : ''}`}>{ARCHIVE.test(row.file.name) ? 'ZIP' : 'IMG'}</span><div className="file-info"><strong title={row.file.name}>{(row.file as File & { webkitRelativePath?: string }).webkitRelativePath || row.file.name}</strong><span>{row.message || `${formatBytes(row.file.size)}${row.status === 'uploaded' ? ' · Confirmed' : row.status === 'uploading' ? ` · Uploading ${Math.round(row.progress * 100)}%` : ''}`}</span></div><span className={`file-state ${row.status}`}>{row.status === 'uploaded' ? '✓' : row.status === 'failed' ? '!' : row.status === 'uploading' ? '…' : '•'}</span>
          </div>)}</div>
        </div>}
        {message && <div className={`upload-message ${hasErrors || uploadHadFailures ? 'warning' : 'error'}`} role="status">{message}</div>}
        {confirmedIds.length > 0 && (hasErrors || uploadHadFailures) && <div className="partial-note"><span>✓</span> {confirmedIds.length} file{confirmedIds.length === 1 ? '' : 's'} uploaded and verified. {hasErrors ? 'You can process these while failed files remain available to retry.' : 'All retry attempts are complete; start processing when ready.'}</div>}
      </div>
      <footer className="modal-footer">
        <button className="button button-quiet" onClick={onClose} disabled={busy}>Cancel</button>
        <div className="footer-actions">
          {hasErrors && failures.some((row) => row.retryable) && <button className="button button-secondary" onClick={retryFailures} disabled={busy}>Retry failed files</button>}
          {hasErrors && confirmedIds.length > 0 && <button className="button button-primary" onClick={() => processConfirmed(true)} disabled={busy}>{busy ? 'Working…' : 'Process confirmed files'}</button>}
          {!hasErrors && uploadHadFailures && confirmedIds.length > 0 && <button className="button button-primary" onClick={() => processConfirmed(false)} disabled={busy}>{busy ? 'Starting…' : 'Start processing'} <span>→</span></button>}
          {!hasErrors && !uploadHadFailures && <button className="button button-primary" onClick={submitUpload} disabled={busy || !rows.some((row) => row.status === 'ready')}>{busy ? 'Uploading…' : rows.some((row) => row.status === 'uploaded') ? 'Upload more and process' : 'Upload and process'} <span>→</span></button>}
        </div>
      </footer>
    </section>
  </div>;
}

function formatBytes(bytes: number): string {
  if (bytes < 1_000_000) return `${Math.max(1, Math.round(bytes / 1_000))} KB`;
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}
