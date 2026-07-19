FROM python:3.12-slim-bookworm

# System deps: ffmpeg for video assembly, chromium for capture agent
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        chromium \
        fonts-liberation \
        libnss3 \
        libatk-bridge2.0-0 \
        libdrm2 \
        libxkbcommon0 \
        libgbm1 \
        libasound2 \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps (infra + app)
COPY infra/alicloud/requirements.txt /app/infra-requirements.txt
COPY pyproject.toml /app/
COPY sizzle/ /app/sizzle/
RUN pip install --no-cache-dir \
        -r /app/infra-requirements.txt \
        /app

# Install Playwright browsers (chromium only — system chromium used via channel)
RUN playwright install --with-deps chromium 2>/dev/null || true

# Copy the FC handler modules
COPY infra/alicloud/pipeline_worker.py /app/
COPY infra/alicloud/render_worker.py /app/
COPY infra/alicloud/fc_entrypoint.py /app/

# FC 3.0 custom container runtime: serves invocations over HTTP.
# FC_HANDLER env var (set per-function) selects the handler, e.g.
# "pipeline_worker.handler" or "render_worker.handler".
EXPOSE 9000
ENV PYTHONUNBUFFERED=1

CMD ["python", "/app/fc_entrypoint.py"]
