FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# psycopg needs libpq; everything else is wheels
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 postgresql-client curl \
 && rm -rf /var/lib/apt/lists/*

# requirements first, so a code change does not reinstall the world
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The model runtime, in its own layer: it is most of the image and changes far
# less often than the code. Build with --build-arg WITH_MODELS=0 for a lean
# image that answers through Gemma and lexical retrieval instead.
ARG WITH_MODELS=1
COPY requirements-ml.txt .
RUN if [ "$WITH_MODELS" = "1" ]; then \
      pip install --no-cache-dir -r requirements-ml.txt; \
    else echo "skipping the model runtime"; fi

# Transformers must not reach for the hub at request time: the checkpoints are
# mounted, and a network call on the read path is a timeout waiting to happen.
# USE_TF because transformers imports TensorFlow if it can find one.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    USE_TF=0

COPY . .

# Not root. The API has no reason to be.
RUN useradd --create-home --uid 10001 speakql && chown -R speakql:speakql /srv
USER speakql

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
