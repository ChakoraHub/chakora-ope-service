FROM python:3.10-slim

WORKDIR /app

# Install system build dependencies and curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy microservice application source code
COPY . .

# Expose Open Positions microservice port
EXPOSE 8500

# Health check to ensure service is responding
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:8500/health || exit 1

# Command to run FastAPI service on port 8500
CMD ["uvicorn", "ope_service:app", "--host", "0.0.0.0", "--port", "8500"]
