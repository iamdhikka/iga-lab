FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY catalog.yaml .
COPY iga ./iga

# User non-root
RUN useradd --uid 10001 --no-create-home app
USER app

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=60s --retries=12 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"

# Satu proses saja: worker latar belakang berjalan di dalam proses aplikasi
CMD ["uvicorn", "iga.web:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
