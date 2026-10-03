import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { JobList } from './JobList';

describe('JobList pagination', () => {
  it('shows total session count and requests the next API page', () => {
    const onPageChange = vi.fn();
    render(<JobList jobs={[{ id: 'job-1', kind: 'ingest_dataset', status: 'queued' }]} count={127} hasNext page={1} hasPrevious={false} onPageChange={onPageChange} isLoading={false} onRetry={vi.fn()} />);
    expect(screen.getByText('127 total sessions')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next →' }));
    expect(onPageChange).toHaveBeenCalledWith(2);
    expect(screen.getByText('Page 1')).toBeInTheDocument();
  });

  it('shows the failed file path and first error message', () => {
    render(<JobList jobs={[{ id: 'job-2', kind: 'extract_metadata', status: 'failed', image_id: 'image-2', image_path: 'survey/east/photo.tif', errors: [{ stage: 'processing', message: 'Image header is invalid.' }] }]} hasNext={false} hasPrevious={false} page={1} onPageChange={vi.fn()} isLoading={false} onRetry={vi.fn()} />);
    expect(screen.getByText('survey/east/photo.tif')).toBeInTheDocument();
    expect(screen.getByText('Image header is invalid.')).toBeInTheDocument();
  });
});
