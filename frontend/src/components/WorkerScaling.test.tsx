import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api';
import type { LocalScalingPayload } from '../types';
import { WorkerScaling } from './WorkerScaling';

vi.mock('../api', () => ({ api: { localScaling: vi.fn(), saveLocalScaling: vi.fn() } }));

const initial: LocalScalingPayload = {
  policy: {
    replica_autoscaling_enabled: true,
    slot_budget: 8,
    queues: {
      ingest: { min_processes: 1, max_processes: 2, min_replicas: 1, max_replicas: 2 },
      images: { min_processes: 1, max_processes: 2, min_replicas: 1, max_replicas: 2 },
    },
  },
  status: {
    state: 'healthy',
    last_seen_at: new Date().toISOString(),
    error: '',
    queues: {
      ingest: { queued: 3, active: 1, reserved: 0, scheduled: 0, replicas: 1, processes: 2 },
      images: { queued: 0, active: 0, reserved: 0, scheduled: 0, replicas: 1, processes: 1 },
    },
  },
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><WorkerScaling /></QueryClientProvider>);
}

describe('WorkerScaling', () => {
  beforeEach(() => {
    vi.mocked(api.localScaling).mockReset();
    vi.mocked(api.saveLocalScaling).mockReset();
    vi.mocked(api.localScaling).mockResolvedValue(initial);
  });

  it('shows live queue capacity and saves both pools as one policy', async () => {
    vi.mocked(api.saveLocalScaling).mockImplementation(async (policy) => ({ ...initial, policy }));
    renderPage();

    expect(await screen.findByText('Worker scaling')).toBeInTheDocument();
    expect(screen.getByText('3 / 8')).toBeInTheDocument();
    expect(screen.getByText('live processes / selected maximum')).toBeInTheDocument();
    expect(screen.getAllByText('Waiting in queue')).toHaveLength(2);
    expect(screen.getByText('3')).toBeInTheDocument();

    fireEvent.change(screen.getAllByLabelText('Maximum processes per container')[0], { target: { value: '3' } });
    fireEvent.change(screen.getAllByLabelText('Maximum containers')[1], { target: { value: '1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save limits' }));

    await waitFor(() => expect(api.saveLocalScaling).toHaveBeenCalledTimes(1));
    const savedPolicy = vi.mocked(api.saveLocalScaling).mock.calls[0][0];
    expect(savedPolicy.queues.ingest.max_processes).toBe(3);
    expect(savedPolicy.queues.images.max_replicas).toBe(1);
  });

  it('shows controller offline and API error states', async () => {
    vi.mocked(api.localScaling).mockResolvedValue({ ...initial, status: { ...initial.status, state: 'offline' } });
    const offlinePage = renderPage();
    expect(await screen.findByText(/controller has not reported in the last 45 seconds/i)).toBeInTheDocument();
    expect(screen.getByText('— / 8')).toBeInTheDocument();
    offlinePage.unmount();

    vi.mocked(api.localScaling).mockRejectedValueOnce(new Error('Scaling API unavailable'));
    const errorPage = renderPage();
    expect(await screen.findByRole('alert')).toHaveTextContent('Scaling API unavailable');
    errorPage.unmount();
  });
});
