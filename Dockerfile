FROM python:3.11-slim-bookworm

# Base environment and Chromium installation
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    MUSE2API_HOME=/app \
    MUSE2API_HOST=0.0.0.0 \
    MUSE2API_PORT=18610 \
    MUSE2API_CHROMIUM=/usr/bin/chromium \
    MUSE2API_CDP_PORT=19210

ARG BUILD_SHA=
ENV MUSE2API_BUILD_SHA=$BUILD_SHA
LABEL org.opencontainers.image.revision=$BUILD_SHA

WORKDIR /app

# Install Chromium, CJK fonts, and core dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    ffmpeg \
    fonts-wqy-zenhei \
    fonts-freefont-ttf \
    procps \
    curl \
    git \
            ca-certificates \
            rclone \
            age \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy code and required directories
COPY admin.html app.py cdp.py config.py engine.py store.py proxy.py generation_timing.py thumbnails.py version.json ./
COPY deploy/ ./deploy/
COPY extension/ ./extension/
# scripts/ is needed at runtime: the first-run setup wizard calls
# scripts/muse2api-data.sh (setup + restore). Without it, restore fails
# with "No such file or directory" on Docker installs.
COPY scripts/ ./scripts/
COPY tools/ ./tools/

# Create the persistent data directory
RUN mkdir -p /app/data/media /app/data/downloads /root/muse2api-profiles

VOLUME ["/app/data"]

EXPOSE 18610

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "18610"]
