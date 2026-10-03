import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './api';

function jsonResponse(payload: unknown) {
  return {
    ok: true,
    status: 200,
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => payload,
  };
}

describe('API list pagination', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('loads every dataset page for the sidebar', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ results: [{ id: 'one', name: 'One' }], next: '/api/datasets/?page=2' }))
      .mockResolvedValueOnce(jsonResponse({ results: [{ id: 'two', name: 'Two' }], next: null }));
    vi.stubGlobal('fetch', fetchMock);

    const result = await api.datasets();

    expect(result.map((dataset) => dataset.id)).toEqual(['one', 'two']);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/datasets/?page=1&page_size=100');
    expect(fetchMock.mock.calls[1][0]).toBe('/api/datasets/?page=2&page_size=100');
  });

  it('loads all child-job pages and requests the gallery location filter', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ results: [{ id: 'job-one', status: 'succeeded' }], next: '/api/jobs/?page=2' }))
      .mockResolvedValueOnce(jsonResponse({ results: [{ id: 'job-two', status: 'failed' }], next: null }));
    vi.stubGlobal('fetch', fetchMock);

    const jobs = await api.allJobs({ parentJob: 'parent-one' });
    expect(jobs.map((job) => job.id)).toEqual(['job-one', 'job-two']);

    fetchMock.mockResolvedValueOnce(jsonResponse({ results: [], next: null }));
    await api.images('dataset-one', 1, false);
    expect(fetchMock.mock.calls[2][0]).toBe('/api/datasets/dataset-one/images/?page=1&has_location=false');
  });
});
