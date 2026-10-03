import type { Dataset, FeatureCollection, ImageRecord, Job, LocalScalingPayload, LocalScalingPolicy, MultipartUploadReceipt, UploadTarget, User } from './types';

const API = '/api';

export class ApiError extends Error {
  status: number;
  details: unknown;
  constructor(message: string, status: number, details?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.details = details;
  }
}

export interface ImagePage {
  items: ImageRecord[];
  count?: number;
  next?: string | null;
  previous?: string | null;
}

export interface JobPage {
  items: Job[];
  count?: number;
  next?: string | null;
  previous?: string | null;
}

interface JobFilters {
  dataset?: string | number;
  status?: string;
  type?: string;
  parentJob?: string | number;
  page?: number;
}

function cookie(name: string): string | undefined {
  const item = document.cookie.split('; ').find((part) => part.startsWith(`${name}=`));
  return item ? decodeURIComponent(item.slice(name.length + 1)) : undefined;
}

let csrfToken: string | undefined;
async function getCsrfToken(): Promise<string> {
  csrfToken = cookie('csrftoken') || csrfToken;
  if (csrfToken) return csrfToken;
  const response = await fetch(`${API}/auth/csrf/`, { credentials: 'include' });
  if (!response.ok) throw new ApiError('Could not initialize secure session.', response.status);
  const payload = (await response.json().catch(() => ({}))) as { csrfToken?: string; csrf_token?: string; token?: string };
  csrfToken = cookie('csrftoken') || payload.csrfToken || payload.csrf_token || payload.token;
  if (!csrfToken) throw new ApiError('The server did not provide a CSRF token.', response.status);
  return csrfToken;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method || 'GET').toUpperCase();
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) headers.set('X-CSRFToken', await getCsrfToken());
  const response = await fetch(`${API}${path}`, { ...init, method, headers, credentials: 'include' });
  if (response.status === 204) return undefined as T;
  const isJson = response.headers.get('content-type')?.includes('application/json');
  const payload = isJson ? await response.json().catch(() => null) : await response.text();
  if (!response.ok) {
    const nestedError = (payload as { error?: { message?: string } } | null)?.error;
    const detail = (payload as { detail?: string; error?: string } | null)?.detail || (typeof nestedError === 'object' ? nestedError?.message : nestedError);
    throw new ApiError(detail || (typeof payload === 'string' && payload) || `Request failed (${response.status}).`, response.status, payload);
  }
  return payload as T;
}

export const api = {
  async me(): Promise<User> {
    const data = await request<User | { user: User }>('/auth/me/');
    return 'user' in data ? data.user : data;
  },
  register: (values: { username: string; password: string; email?: string }) => request<User>('/auth/register/', { method: 'POST', body: JSON.stringify(values) }),
  login: (values: { username: string; password: string }) => request<User>('/auth/login/', { method: 'POST', body: JSON.stringify(values) }),
  logout: () => request<void>('/auth/logout/', { method: 'POST', body: '{}' }),
  localScaling: () => request<LocalScalingPayload>('/local/scaling/'),
  saveLocalScaling: (policy: LocalScalingPolicy) => {
    const values = {
      replica_autoscaling_enabled: policy.replica_autoscaling_enabled,
      queues: policy.queues,
    };
    return request<LocalScalingPayload>('/local/scaling/', { method: 'PUT', body: JSON.stringify(values) });
  },
  async datasets(): Promise<Dataset[]> {
    const items: Dataset[] = [];
    let page = 1;
    while (true) {
      const payload = await request<{ results?: Dataset[]; next?: string | null }>(`/datasets/?page=${page}&page_size=100`);
      items.push(...(payload.results || []));
      if (!payload.next) return items;
      page += 1;
    }
  },
  createDataset: (name: string) => request<Dataset>('/datasets/', { method: 'POST', body: JSON.stringify({ name }) }),
  async prepareUploads(datasetId: string | number, files: File[]): Promise<UploadTarget[]> {
    const descriptors = files.map((file) => {
      const relativePath = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
      const filename = relativePath || file.name;
      const contentType = /\.zip$/i.test(filename) ? 'application/zip' : file.type || 'application/octet-stream';
      return { name: file.name, size: file.size, content_type: contentType, ...(relativePath ? { relative_path: relativePath } : {}) };
    });
    const result = await request<{ uploads: UploadTarget[] }>(`/datasets/${datasetId}/uploads/prepare/`, { method: 'POST', body: JSON.stringify({ files: descriptors }) });
    return result.uploads;
  },
  confirmUploads: (datasetId: string | number, uploadIds: Array<string | number>, multipartUploads: Array<MultipartUploadReceipt & { id: string | number }> = []) => request<{ uploads: Array<{ id: string | number; confirmed: boolean; upload_error?: string }> }>(`/datasets/${datasetId}/uploads/confirm/`, { method: 'POST', body: JSON.stringify({ upload_ids: uploadIds, ...(multipartUploads.length ? { multipart_uploads: multipartUploads } : {}) }) }),
  processDataset: (datasetId: string | number, uploadIds?: Array<string | number>, allowPartial = false) => request<{ job: Job }>(`/datasets/${datasetId}/process/`, { method: 'POST', body: JSON.stringify({ ...(uploadIds ? { upload_ids: uploadIds } : {}), allow_partial: allowPartial }) }),
  deleteDataset: (datasetId: string | number) => request<void>(`/datasets/${datasetId}/`, { method: 'DELETE' }),
  async images(datasetId: string | number, page = 1, hasLocation?: boolean): Promise<ImagePage> {
    const params = new URLSearchParams({ page: String(page) });
    if (hasLocation !== undefined) params.set('has_location', String(hasLocation));
    const payload = await request<unknown>(`/datasets/${datasetId}/images/?${params.toString()}`);
    if (Array.isArray(payload)) return { items: payload as ImageRecord[], next: null, previous: null };
    const value = payload as { results?: ImageRecord[]; images?: ImageRecord[]; count?: number; next?: string | null; previous?: string | null };
    return { items: value.results || value.images || [], count: value.count, next: value.next, previous: value.previous };
  },
  map: (datasetId: string | number) => request<FeatureCollection>(`/datasets/${datasetId}/map/`),
  image: (imageId: string | number) => request<ImageRecord>(`/images/${imageId}/`),
  async jobs(filters: JobFilters = {}): Promise<JobPage> {
    const params = new URLSearchParams();
    if (filters.dataset) params.set('dataset', String(filters.dataset));
    if (filters.status) params.set('status', filters.status);
    if (filters.type) params.set('type', filters.type);
    if (filters.parentJob) params.set('parent_job', String(filters.parentJob));
    if (filters.page && filters.page > 1) params.set('page', String(filters.page));
    const payload = await request<unknown>(`/jobs/${params.size ? `?${params.toString()}` : ''}`);
    if (Array.isArray(payload)) return { items: payload as Job[], next: null, previous: null };
    const value = payload as { results?: Job[]; jobs?: Job[]; count?: number; next?: string | null; previous?: string | null };
    return { items: value.results || value.jobs || [], count: value.count, next: value.next, previous: value.previous };
  },
  async allJobs(filters: Omit<JobFilters, 'page'> = {}): Promise<Job[]> {
    const items: Job[] = [];
    let page = 1;
    while (true) {
      const result = await api.jobs({ ...filters, page });
      items.push(...result.items);
      if (!result.next) return items;
      page += 1;
    }
  },
  job: (id: string | number) => request<Job>(`/jobs/${id}/`),
  retryJob: (kind: string, imageId: string | number, parentId?: string | number) => request<{ job: Job }>('/jobs/', { method: 'POST', body: JSON.stringify({ kind, image_id: imageId, ...(parentId ? { parent_id: parentId } : {}) }) }),
  retryFailedBatch: (jobId: string | number) => request<{ retried_count: number }>(`/jobs/${jobId}/retry-failed/`, { method: 'POST', body: '{}' }),
  cancelUploadSession: (jobId: string | number) => request<{ cancelled_count: number; job: Job }>(`/jobs/${jobId}/cancel/`, { method: 'POST', body: '{}' }),
  deleteUploadSession: (jobId: string | number) => request<void>(`/jobs/${jobId}/`, { method: 'DELETE' }),
  deleteImage: (imageId: string | number) => request<void>(`/images/${imageId}/`, { method: 'DELETE' }),
};

export function uploadToMinio(target: UploadTarget, file: File, onProgress: (fraction: number) => void): Promise<MultipartUploadReceipt | undefined> {
  if (target.method === 'MULTIPART' && target.multipart) return uploadMultipart(target.multipart, file, onProgress);
  return new Promise<undefined>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(target.method || 'POST', target.upload_url);
    xhr.upload.onprogress = (event) => { if (event.lengthComputable) onProgress(event.loaded / event.total); };
    xhr.onerror = () => reject(new Error('Network error while uploading.'));
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) { onProgress(1); resolve(undefined); }
      else reject(new Error(`Storage rejected this file (${xhr.status}).`));
    };
    if ((target.method || 'POST') === 'PUT') {
      xhr.send(file);
    } else {
      const form = new FormData();
      Object.entries(target.fields || {}).forEach(([key, value]) => form.append(key, value));
      if (!('key' in target.fields)) form.append('key', target.object_key);
      form.append('file', file, file.name);
      xhr.send(form);
    }
  });
}

export async function abortMultipartUpload(target: UploadTarget): Promise<void> {
  if (!target.multipart?.abort_url) return;
  try {
    await fetch(target.multipart.abort_url, { method: 'DELETE' });
  } catch {
    // Cleanup is best effort; the upload error should remain visible.
  }
}

async function uploadMultipart(
  multipart: NonNullable<UploadTarget['multipart']>,
  file: File,
  onProgress: (fraction: number) => void,
): Promise<MultipartUploadReceipt> {
  const loadedByPart = new Array(multipart.parts.length).fill(0) as number[];
  let cursor = 0;
  const reportProgress = () => onProgress(Math.min(1, loadedByPart.reduce((sum, loaded) => sum + loaded, 0) / file.size));
  const uploadPart = (index: number): Promise<void> => new Promise((resolve, reject) => {
    const part = multipart.parts[index];
    const start = (part.part_number - 1) * multipart.part_size;
    const body = file.slice(start, Math.min(start + multipart.part_size, file.size));
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', part.upload_url);
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      loadedByPart[index] = Math.min(body.size, event.loaded);
      reportProgress();
    };
    xhr.onerror = () => reject(new Error('Network error while uploading a large file part.'));
    xhr.onload = () => {
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new Error(`Storage rejected a large file part (${xhr.status}).`));
        return;
      }
      loadedByPart[index] = body.size;
      reportProgress();
      resolve();
    };
    xhr.send(body);
  });

  try {
    let failure: unknown;
    const worker = async () => {
      while (!failure && cursor < multipart.parts.length) {
        const index = cursor++;
        try {
          await uploadPart(index);
        } catch (error) {
          failure = error;
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(4, multipart.parts.length) }, worker));
    if (failure) throw failure;
    onProgress(1);
    return { upload_id: multipart.upload_id };
  } catch (error) {
    try {
      await fetch(multipart.abort_url, { method: 'DELETE' });
    } catch {
      // The main upload error is more useful than a cleanup error.
    }
    throw error;
  }
}
