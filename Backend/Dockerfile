FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt requirements-ml.txt ./
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.10.0 \
    && pip install --no-cache-dir -r requirements-ml.txt

COPY . .

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
