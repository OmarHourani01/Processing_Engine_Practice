# Image Processing Engine

Image Processing Engine is a private image dataset workspace. Upload image files, a browser-selected folder, or ZIP archives; the app extracts image dimensions, capture time, camera details, and EXIF GPS coordinates, then creates WebP previews. Located images appear on a map, and images without a location appear in a gallery. You can inspect an image and open or download its original.

Each account sees only its own datasets. GeoTIFF files are accepted as TIFF images; map locations come from EXIF GPS metadata in the file.

## Run locally

You need Docker Engine, Docker Compose v2, and Python 3. From the repository root:

```sh
cp .env.example .env
```

```sh
python3 scripts/local_autoscale.py
```

The launcher builds and starts PostgreSQL/PostGIS, Redis, MinIO, Django, Celery workers and Beat, and the frontend. It runs database migrations and prepares the object bucket. Keep the launcher running while you use the app; it polls queue activity and adjusts the number of local worker containers.

Open <http://localhost:5173> and register an account. A public Mapbox access token in `.env` enables the map. Without a token, uploads and the image gallery still work. The API health endpoint is <http://localhost:8000/api/health/>. The MinIO console is at <http://localhost:9001>; the example credentials are `local-minio` / `local-minio-password`.

The **Worker scaling** page is available when the host launcher starts the stack. Any signed-in user can change the shared local process and container limits. `LOCAL_SCALING_SLOT_BUDGET` in `.env` caps the combined configured maximum. App and storage ports in Compose bind to `127.0.0.1`.

Press `Ctrl+C` to stop the launcher. Then stop the containers with:

```sh
docker compose down
```

Named volumes retain the database, Redis, and uploaded objects. `docker compose down -v` also removes those volumes and their data.

## Use the workspace

1. Create a dataset and select JPEG, PNG, WebP, TIFF (`.tif` or `.tiff`), GeoTIFF (`.geotiff`), or ZIP files.
2. Follow upload confirmation and processing progress in **Processing jobs**. Failed browser uploads can be retried or the confirmed files can be processed. Failed per-image metadata and preview tasks can be retried.
3. Browse images with EXIF GPS on the map and images without a location in the gallery. Open an image for metadata, previews, and a signed link to its original.
4. Cancel an active upload session or delete an inactive session, image, or dataset in the app. Cancellation of a task already executing is best effort.

## Job API

The API uses authenticated sessions; browser writes require a CSRF token. After preparing, transferring, and confirming uploads, submit them for background processing:

```http
POST /api/datasets/{dataset_id}/process/
```

The API responds with `202 Accepted` and a job ID while workers process the uploads:

```json
{
  "job": {
    "id": "<job-uuid>",
    "kind": "ingest_dataset",
    "status": "queued"
  }
}
```

Poll `GET /api/jobs/{job_id}/` for the current status, progress counts, errors, and result. The `result` field contains operation-specific output when processing completes. `GET /api/jobs/` lists jobs and supports `status`, `type`, `dataset`, and `parent_job` filters, for example:

```http
GET /api/jobs/?type=generate_thumbnail&status=failed&dataset={dataset_id}
```

The `POST /api/jobs/` endpoint also accepts `extract_metadata` and `generate_thumbnail` jobs for an existing image, with `kind` and `image_id` in the JSON body. The [architecture document](docs/architecture.md) describes the components, processing flow, retry behavior, and job response fields.

The default dataset quota is **1,000 uploaded items and 10 GiB of uploaded bytes**. A ZIP's expanded content is checked against those limits when its upload session is processed. Files larger than 5 GB use multipart transfer. Upload links expire after 12 hours by default; signed image links expire after 5 minutes.
