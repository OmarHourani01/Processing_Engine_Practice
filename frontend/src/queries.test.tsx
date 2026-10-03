import { ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useJobsQuery } from './queries';

const mocks = vi.hoisted(() => ({ api: { jobs: vi.fn() } }));
vi.mock('./api', () => mocks);

function Probe() {
  const query = useJobsQuery(undefined, 1);
  return <span>{query.data?.items[0]?.status || 'loading'}</span>;
}

function Wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe('job polling', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.api.jobs
      .mockResolvedValueOnce({ items: [{ id: 'job-1', kind: 'ingest_dataset', status: 'running' }], count: 1, next: null })
      .mockResolvedValue({ items: [{ id: 'job-1', kind: 'ingest_dataset', status: 'succeeded' }], count: 1, next: null });
  });
  afterEach(() => vi.useRealTimers());

  it('refreshes active job status automatically until it completes', async () => {
    render(<Probe />, { wrapper: Wrapper });
    expect(await screen.findByText('running')).toBeInTheDocument();
    await waitFor(() => expect(mocks.api.jobs).toHaveBeenCalledTimes(2), { timeout: 6_000 });
    expect(await screen.findByText('succeeded')).toBeInTheDocument();
  }, 8_000);
});
