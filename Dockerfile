FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY static/ ./static/

# Create directories
RUN mkdir -p uploads models

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV WHISPER_MODEL=base
ENV PYTHONFAULTHANDLER=1

# Expose port
EXPOSE 8000

# Run the application with explicit error handling
CMD ["sh", "-c", "ulimit -c unlimited && uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-keep-alive 300"]
