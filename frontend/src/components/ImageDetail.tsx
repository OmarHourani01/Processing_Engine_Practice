import { useEffect, useState } from 'react';
import type { ImageRecord } from '../types';

export function ImageDetail({ image, loading, onClose, onDelete }: { image: ImageRecord; loading: boolean; onClose: () => void; onDelete: (image: ImageRecord) => Promise<boolean> }) {
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  useEffect(() => {
    function escape(event: KeyboardEvent) { if (event.key === 'Escape') onClose(); }
    window.addEventListener('keydown', escape);
    return () => window.removeEventListener('keydown', escape);
  }, [onClose]);

  const name = image.relative_path || image.filename || image.original_filename || `Image ${image.id}`;
  const coordinates = image.location?.coordinates;
  const metadata = [
    ['Captured', image.captured_at ? new Date(image.captured_at).toLocaleString() : 'Not available'],
    ['Camera', [image.camera_make, image.camera_model].filter(Boolean).join(' ') || 'Not available'],
    ['Dimensions', image.width && image.height ? `${image.width} × ${image.height} px` : 'Not available'],
    ['Location', coordinates ? `${coordinates[1].toFixed(5)}, ${coordinates[0].toFixed(5)}` : 'No GPS data'],
    ['Format', image.content_type || 'Not available'],
    ['File size', image.size ? formatSize(image.size) : 'Not available'],
  ];

  return <div className="detail-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="image-detail" role="dialog" aria-modal="true" aria-label="Image details">
      <header className="detail-header"><div><div className="eyebrow">IMAGE DETAIL</div><h2 title={name}>{name.split('/').pop()}</h2></div><button className="icon-button close-button" aria-label="Close image details" onClick={onClose}>×</button></header>
      <div className="detail-preview">
        {loading ? <div className="detail-loading">Loading image…</div> : image.preview_url || image.thumbnail_url ? <img src={image.preview_url || image.thumbnail_url || undefined} alt={name} /> : <div className="detail-placeholder"><span>▧</span><p>Preview is not ready yet.</p></div>}
      </div>
      <div className="detail-body">
        <div className="detail-file-label">FILE</div><div className="detail-metadata">
          {metadata.map(([label, value]) => <div className="metadata-row" key={label}><span>{label}</span><strong>{value}</strong></div>)}
        </div>
      </div>
      <footer className="detail-footer">{image.original_url ? <a className="button button-secondary" href={image.original_url} target="_blank" rel="noreferrer">Open original <span>↗</span></a> : <span className="detail-muted">Original file is being prepared.</span>}<div className="detail-footer-actions">{deleteError && <span className="detail-delete-error">{deleteError}</span>}<button className="button button-danger" onClick={async () => { setDeleting(true); setDeleteError(''); try { if (await onDelete(image)) onClose(); } catch (error) { setDeleteError(error instanceof Error ? error.message : 'Could not delete this picture.'); } finally { setDeleting(false); } }} disabled={loading || deleting || image.status === 'processing'} title={image.status === 'processing' ? 'Wait for processing to finish before deleting this picture.' : 'Delete this picture and its stored files'}>{deleting ? 'Deleting…' : 'Delete picture'}</button><button className="button button-quiet" onClick={onClose}>Close</button></div></footer>
    </section>
  </div>;
}

function formatSize(bytes: number): string { return bytes >= 1_000_000 ? `${(bytes / 1_000_000).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1_000))} KB`; }
