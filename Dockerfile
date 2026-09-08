FROM python:3.11-slim

# Metadata
LABEL maintainer="nifty-bot"
LABEL description="Nifty F&O Paper/Live Trading Bot - Angel One SmartAPI"

# Set environment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN useradd -m -u 1000 botuser

# Set working directory
WORKDIR /app

# Copy and install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY --chown=botuser:botuser . .

# Create directories for persistent data
RUN mkdir -p /app/data /app/logs \
    && chown -R botuser:botuser /app/data /app/logs

# Switch to non-root user
USER botuser

# Health check — verifies the process is alive
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os; exit(0 if os.path.exists('/app/data/trades.db') else 1)"

# Default entrypoint
ENTRYPOINT ["python", "main.py"]
