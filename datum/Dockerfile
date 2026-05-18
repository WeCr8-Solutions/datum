# ============================================================
# DATUM — Dockerfile
# Single image used by forge, anvil, panel, and watcher services.
# Source code is bind-mounted at runtime so changes are live.
# ============================================================

FROM python:3.12-slim

# System deps: git (for GitPython), tesseract (OCR), libreoffice-less
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        tesseract-ocr \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached layer — only rebuilds when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-create runtime dirs so services start cleanly even on first boot
RUN mkdir -p \
    forge/logs forge/staging forge/processed forge/reports \
    forge/review_queue forge/memory \
    anvil/logs anvil/reports anvil/review_queue \
    panel logs

# Source bind-mounted at /app by docker-compose — no COPY needed
# This means code edits on host are instantly live inside containers.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
