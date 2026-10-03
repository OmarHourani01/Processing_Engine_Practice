import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { UploadDialog } from './UploadDialog';

const mocks = vi.hoisted(() => ({
  api: {
    createDataset: vi.fn(),
    prepareUploads: vi.fn(),
    confirmUploads: vi.fn(),
    processDataset: vi.fn(),
  },
  uploadToMinio: vi.fn(),
}));

vi.mock('../api', () => mocks);

const dataset = { id: 'dataset-1', name: 'Field images', image_count: 0 };

beforeEach(() => {
  vi.clearAllMocks();
  mocks.api.prepareUploads.mockImplementation(async (_datasetId: string, files: File[]) =>
    files.map((file) => ({ id: file.name, object_key: file.name, upload_url: 'https://minio.test/upload', fields: {}, method: 'POST' })),
  );
  mocks.api.confirmUploads.mockImplementation(async (_datasetId: string, ids: string[]) => ({ uploads: ids.map((id) => ({ id, confirmed: true })) }));
  mocks.api.processDataset.mockResolvedValue({ job: { id: 'ingest-1', status: 'queued' } });
});

describe('UploadDialog recovery', () => {
  it('retries a failed file and waits for an explicit processing action after recovery', async () => {
    const good = new File(['image'], 'good.jpg', { type: 'image/jpeg' });
    const bad = new File(['image'], 'retry.png', { type: 'image/png' });
    let failedOnce = false;
    mocks.uploadToMinio.mockImplementation(async (_target, file: File) => {
      if (file.name === 'retry.png' && !failedOnce) { failedOnce = true; throw new Error('Temporary connection issue.'); }
    });
    const onComplete = vi.fn();
    render(<UploadDialog datasets={[dataset]} selectedDataset={dataset} onClose={vi.fn()} onComplete={onComplete} />);

    fireEvent.change(screen.getByLabelText(/Choose image files/), { target: { files: [good, bad] } });
    fireEvent.click(screen.getByRole('button', { name: /Upload and process/ }));
    expect(await screen.findByText(/Some files could not be uploaded/)).toBeInTheDocument();
    expect(mocks.api.processDataset).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Retry failed files' }));
    const startProcessing = await screen.findByRole('button', { name: /Start processing/ });
    expect(mocks.api.processDataset).not.toHaveBeenCalled();
    fireEvent.click(startProcessing);
    await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
    expect(mocks.api.prepareUploads).toHaveBeenCalledTimes(2);
    expect(mocks.api.processDataset).toHaveBeenCalledWith('dataset-1', ['good.jpg', 'retry.png'], false);
  });

  it('allows processing only confirmed files after a failed upload', async () => {
    const good = new File(['image'], 'good.jpg', { type: 'image/jpeg' });
    const bad = new File(['image'], 'bad.png', { type: 'image/png' });
    mocks.uploadToMinio.mockImplementation(async (_target, file: File) => {
      if (file.name === 'bad.png') throw new Error('Upload failed.');
    });
    const onComplete = vi.fn();
    render(<UploadDialog datasets={[dataset]} selectedDataset={dataset} onClose={vi.fn()} onComplete={onComplete} />);
    fireEvent.change(screen.getByLabelText(/Choose image files/), { target: { files: [good, bad] } });
    fireEvent.click(screen.getByRole('button', { name: /Upload and process/ }));

    fireEvent.click(await screen.findByRole('button', { name: 'Process confirmed files' }));
    await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
    expect(mocks.api.processDataset).toHaveBeenCalledWith('dataset-1', ['good.jpg'], true);
  });
});
