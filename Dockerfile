FROM python:3.11-slim

# Metadata
LABEL maintainer="nifty-bot"
LABEL description="Nifty F&O Paper/Live Trading Bot - Angel One SmartAPI"

# Set environment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata

# Install system dependencies (gosu for privilege drop in entrypoint)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    curl \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN useradd -m -u 1000 botuser

# Set working directory
WORKDIR /app

# Copy and install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# Copy application source
COPY --chown=botuser:botuser . .

# Create directories — owned by botuser in image layer.
# At runtime, named volumes may mount over these as root,
# so entrypoint.sh re-chowns them before dropping to botuser.
RUN mkdir -p /app/data /app/logs \
    && chown -R botuser:botuser /app/data /app/logs

# Copy entrypoint script (runs as root briefly to fix volume perms)
COPY --chown=root:root entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os; exit(0 if os.path.exists('/app/data/trades.db') else 1)"

# Run as root so entrypoint.sh can chown volumes, then it drops to botuser
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "main.py"]
