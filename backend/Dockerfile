FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

# 1. Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ffmpeg \
    espeak-ng \
    libgl1 \
    libglx-mesa0 \
    libglib2.0-0 \
    portaudio19-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 2. Copy and install requirements
COPY backend/requirements.txt ./requirements.txt
COPY backend/requirements-wakeword.txt ./requirements-wakeword.txt

RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir onnxruntime scikit-learn \
    && pip install --no-cache-dir --no-deps openwakeword>=0.6.0

# 3. Download base models cleanly via Python
RUN python -c "import openwakeword; openwakeword.utils.download_models()"

# 4. Copy backend source files
COPY backend/ .

EXPOSE 8000

CMD ["sh", "-c", "alembic -c alembic.ini upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
