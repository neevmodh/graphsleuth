FROM python:3.11-slim

# System deps for DuckDB, numpy, scikit-learn
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the full application source (data/store/ included; tg/ excluded via .dockerignore)
COPY . .

# Railway injects $PORT; uvicorn binds to it
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

# Pre-create the volume mount point directories
RUN mkdir -p /data/cases /data/store

# docker-entrypoint.sh was copied as part of COPY . . above (lives at /app/docker-entrypoint.sh)
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

CMD ["/app/docker-entrypoint.sh"]
