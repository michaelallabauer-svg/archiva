FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ocrmypdf \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-deu \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY alembic.ini ./
COPY alembic ./alembic
COPY archiva ./archiva
COPY assets ./assets
COPY scripts ./scripts

RUN python -m pip install --upgrade pip \
    && python -m pip install -e .

RUN useradd --create-home --uid 10001 archiva \
    && mkdir -p /app/data/documents \
    && chown -R archiva:archiva /app/data

USER archiva

EXPOSE 8000

CMD ["python", "-m", "archiva.main"]
