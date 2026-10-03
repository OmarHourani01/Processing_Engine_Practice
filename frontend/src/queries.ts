import { useQuery } from '@tanstack/react-query';
import { api } from './api';
import type { Id, Job } from './types';

export function useJobsQuery(datasetId?: Id, page = 1, enabled = true, type?: string) {
  return useQuery({
    queryKey: ['jobs', datasetId, type, page],
    queryFn: () => api.jobs({ dataset: datasetId, type, page }),
    enabled,
    refetchInterval: (query) => {
      const jobs = query.state.data?.items as Job[] | undefined;
      return jobs?.some((job) => ['queued', 'running'].includes(job.status)) ? 2_500 : 15_000;
    },
  });
}
