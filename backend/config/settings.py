import os
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


def normalize_endpoint(value):
    if "://" not in value:
        scheme = "https" if env_bool("MINIO_SECURE", False) else "http"
        return f"{scheme}://{value}"
    return value


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,api")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.gis",
    "corsheaders",
    "rest_framework",
    "datasets",
    "processing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.contrib.gis.db.backends.postgis",
        "NAME": os.getenv("POSTGRES_DB", os.getenv("DB_NAME", "images")),
        "USER": os.getenv("POSTGRES_USER", os.getenv("DB_USER", "images")),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", os.getenv("DB_PASSWORD", "images")),
        "HOST": os.getenv("POSTGRES_HOST", os.getenv("DB_HOST", "localhost")),
        "PORT": os.getenv("POSTGRES_PORT", os.getenv("DB_PORT", "5432")),
        "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
    }
}
if os.getenv("DATABASE_URL"):
    db_url = urlparse(os.environ["DATABASE_URL"])
    if db_url.scheme not in {"postgres", "postgresql", "postgis"}:
        raise ValueError("DATABASE_URL must use postgres://, postgresql://, or postgis://")
    DATABASES["default"].update(
        {
            "NAME": unquote(db_url.path.lstrip("/")),
            "USER": unquote(db_url.username or ""),
            "PASSWORD": unquote(db_url.password or ""),
            "HOST": db_url.hostname or "localhost",
            "PORT": str(db_url.port or 5432),
        }
    )

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("TZ", "UTC")
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SAMESITE = os.getenv("CSRF_COOKIE_SAMESITE", "Lax")
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", not DEBUG)
CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS", os.getenv("FRONTEND_ORIGIN", "http://localhost:5173") + ",http://127.0.0.1:5173"
)
CSRF_TRUSTED_ORIGINS = env_list(
    "CSRF_TRUSTED_ORIGINS", ",".join(CORS_ALLOWED_ORIGINS)
)

CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = ["accept", "authorization", "content-type", "x-csrftoken", "x-requested-with"]
CORS_EXPOSE_HEADERS = ["content-disposition"]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "datasets.pagination.StandardPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", os.getenv("REDIS_URL", "redis://localhost:6379/0"))
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_DEFAULT_QUEUE = "ingest"
CELERY_TASK_ROUTES = {
    "processing.tasks.ingest_dataset": {"queue": "ingest"},
    "processing.tasks.extract_metadata": {"queue": "images"},
    "processing.tasks.generate_thumbnail": {"queue": "images"},
    "processing.tasks.reconcile_jobs": {"queue": "ingest"},
    "processing.tasks.cleanup_stored_objects": {"queue": "ingest"},
}

LOCAL_SCALING_ENABLED = os.getenv("LOCAL_SCALING_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
LOCAL_SCALING_SLOT_BUDGET = int(os.getenv("LOCAL_SCALING_SLOT_BUDGET", "8"))
if not 4 <= LOCAL_SCALING_SLOT_BUDGET <= 32:
    raise ValueError("LOCAL_SCALING_SLOT_BUDGET must be between 4 and 32")

MINIO_ENDPOINT_URL = normalize_endpoint(os.getenv("MINIO_ENDPOINT_URL", os.getenv("MINIO_ENDPOINT", "http://localhost:9000")))
MINIO_PUBLIC_ENDPOINT_URL = normalize_endpoint(
    os.getenv("MINIO_PUBLIC_ENDPOINT_URL", os.getenv("MINIO_EXTERNAL_ENDPOINT", "http://localhost:9000"))
)
MINIO_ACCESS_KEY = os.getenv("MINIO_ROOT_USER", os.getenv("MINIO_ACCESS_KEY", "minioadmin"))
MINIO_SECRET_KEY = os.getenv("MINIO_ROOT_PASSWORD", os.getenv("MINIO_SECRET_KEY", "minioadmin"))
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "image-datasets")
MINIO_REGION = os.getenv("MINIO_REGION", "us-east-1")
MINIO_ALLOWED_ORIGINS = env_list("MINIO_ALLOWED_ORIGINS", ",".join(CORS_ALLOWED_ORIGINS))
UPLOAD_MAX_FILES = int(os.getenv("UPLOAD_MAX_FILES", "1000"))
UPLOAD_MAX_BYTES = int(os.getenv("UPLOAD_MAX_BYTES", str(10 * 1024 * 1024 * 1024)))
UPLOAD_URL_TTL_SECONDS = int(os.getenv("UPLOAD_URL_TTL_SECONDS", "43200"))
MEDIA_URL_TTL_SECONDS = int(os.getenv("MEDIA_URL_TTL_SECONDS", "300"))
