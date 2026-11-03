FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

# 1. Install system-level C/C++ libraries FIRST
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ffmpeg \
    espeak-ng \
    libgl1 \
    libglx-mesa0 \
    libglib2.0-0 \
    portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

# 2. Copy requirement files
COPY backend/requirements.txt ./requirements.txt
COPY backend/requirements-wakeword.txt ./requirements-wakeword.txt

# 3. Install core dependencies, then install openwakeword bypassing tflite-runtime check
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir onnxruntime scikit-learn \
    && pip install --no-cache-dir --no-deps openwakeword>=0.6.0

# 4. Pre-download openwakeword models
RUN python -c "from openwakeword.utils import download_models; download_models(model_names=['hey_buddy'])"

# 5. Copy backend source code into /app
COPY backend/ .

EXPOSE 8000

# 6. Run database migrations and launch FastAPI
CMD ["sh", "-c", "alembic -c alembic.ini upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
