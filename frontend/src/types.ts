export type Id = string | number;

export interface User {
  id: Id;
  username: string;
  email?: string;
  local_scaling_available?: boolean;
}

export interface QueueScalingLimits {
  min_processes: number;
  max_processes: number;
  min_replicas: number;
  max_replicas: number;
}

export interface LocalScalingPolicy {
  replica_autoscaling_enabled: boolean;
  slot_budget: number;
  queues: {
    ingest: QueueScalingLimits;
    images: QueueScalingLimits;
  };
}

export interface LocalQueueScalingStatus {
  queued: number;
  active: number;
  reserved: number;
  scheduled: number;
  replicas: number;
  processes: number;
}

export interface LocalScalingPayload {
  policy: LocalScalingPolicy;
  status: {
    state: 'healthy' | 'degraded' | 'offline' | string;
    last_seen_at: string | null;
    error: string;
    queues: Partial<Record<'ingest' | 'images', LocalQueueScalingStatus>>;
  };
}

export interface Dataset {
  id: Id;
  name: string;
  status?: string;
  image_count?: number;
  geotagged_count?: number;
  total_images?: number;
  located_image_count?: number;
  created_at?: string;
}

export type JobStatus = 'queued' | 'running' | 'succeeded' | 'succeeded_with_errors' | 'failed' | 'cancelled' | string;

export interface Job {
  id: Id;
  kind?: string;
  type?: string;
  status: JobStatus;
  dataset_id?: Id;
  dataset_name?: string;
  input?: Record<string, unknown>;
  image_id?: Id;
  image_path?: string | null;
  parent_job_id?: Id;
  parent_id?: Id;
  progress?: number;
  total?: number;
  completed?: number;
  failed?: number;
  total_count?: number;
  completed_count?: number;
  failed_count?: number;
  attempt_count?: number;
  error?: string;
  errors?: Array<string | Record<string, unknown>>;
  result?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface ImageRecord {
  id: Id;
  dataset_id?: Id;
  relative_path?: string;
  content_type?: string;
  size?: number;
  filename?: string;
  original_filename?: string;
  status?: string;
  width?: number | null;
  height?: number | null;
  captured_at?: string | null;
  camera_make?: string | null;
  camera_model?: string | null;
  latitude?: number | null;
  longitude?: number | null;
  location?: { type: 'Point'; coordinates: [number, number] } | null;
  original_url?: string | null;
  thumbnail_url?: string | null;
  preview_url?: string | null;
  metadata?: Record<string, unknown>;
  error?: string | null;
}

export interface UploadTarget {
  id: Id;
  object_key: string;
  upload_url: string;
  fields: Record<string, string>;
  method?: 'POST' | 'PUT' | 'MULTIPART';
  multipart?: {
    upload_id: string;
    part_size: number;
    parts: Array<{ part_number: number; upload_url: string }>;
    abort_url: string;
  };
}

export interface MultipartUploadReceipt {
  upload_id: string;
}

export interface UploadFailure {
  file: File;
  message: string;
}

export interface GeoJsonFeature {
  type: 'Feature';
  geometry: { type: 'Point'; coordinates: [number, number] };
  properties: { id?: Id; image_id?: Id; [key: string]: unknown };
}

export interface FeatureCollection {
  type: 'FeatureCollection';
  features: GeoJsonFeature[];
}

declare global {
  interface Window {
    __APP_CONFIG__?: { mapboxAccessToken?: string };
  }
}
