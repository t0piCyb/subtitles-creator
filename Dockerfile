FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    fonts-liberation \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Slabo 27px font
RUN mkdir -p /usr/share/fonts/truetype/slabo && \
    curl -fsSL -o /usr/share/fonts/truetype/slabo/Slabo27px-Regular.ttf \
    "https://raw.githubusercontent.com/google/fonts/main/ofl/slabo27px/Slabo27px-Regular.ttf" && \
    fc-cache -f -v

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/

RUN mkdir -p uploads processed

ENV PYTHONUNBUFFERED=1
ENV WHISPER_MODEL=base

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "300"]
