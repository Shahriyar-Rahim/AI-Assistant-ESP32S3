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

# 2. Copy requirement files
COPY backend/requirements.txt ./requirements.txt
COPY backend/requirements-wakeword.txt ./requirements-wakeword.txt

# 3. Install core dependencies, helper packages (tqdm, requests), then openwakeword
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir onnxruntime scikit-learn tqdm requests \
    && pip install --no-cache-dir --no-deps openwakeword>=0.6.0

# 4. Download base wake word models
RUN python -c "import openwakeword; openwakeword.utils.download_models()"

# 5. Copy backend application files
COPY backend/ .

EXPOSE 8000

CMD ["sh", "-c", "alembic -c alembic.ini upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
