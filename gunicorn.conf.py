import os

# Production Gunicorn configuration for Excel Automation System
bind = f"0.0.0.0:{os.environ.get('PORT', '5050')}"

# Concurrency settings:
# We use 1 worker with multiple threads (gthread) because the application uses
# in-process threading.RLock() (_workbook_lock and _workspace_lock) to serialize
# Excel workbook writes and review workspace operations safely. Multiple threads
# handle concurrent requests (page views, downloads, health checks) simultaneously.
workers = int(os.environ.get("GUNICORN_WORKERS", 1))
threads = int(os.environ.get("GUNICORN_THREADS", 4))
worker_class = "gthread"

# Allow sufficient time for processing large workbooks and multi-page PDF invoices
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 120))
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", 5))

# Logging configuration
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")

