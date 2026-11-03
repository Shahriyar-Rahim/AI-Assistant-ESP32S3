FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

# 1. Install system-level C/C++ libraries FIRST so pip packages build cleanly
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ffmpeg \
    espeak-ng \
    libgl1-mesa-glx \
    libglib2.0-0 \
    portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

# 2. Copy and install Python dependencies
COPY backend/requirements.txt ./requirements.txt
COPY backend/requirements-wakeword.txt ./requirements-wakeword.txt
RUN pip install --no-cache-dir -r requirements.txt -r requirements-wakeword.txt

# 3. Pre-download openwakeword models
RUN python -c "from openwakeword.utils import download_models; download_models(model_names=['hey_buddy'])"

# 4. Copy backend source code into /app
COPY backend/ .

EXPOSE 8000

# 5. Execute DB migrations and start FastAPI using Render's dynamic $PORT environment variable
CMD ["sh", "-c", "alembic -c alembic.ini upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
