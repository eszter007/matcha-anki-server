FROM python:3.12-slim

# The Anki library ships its own Rust backend; no system packages are needed.
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY matcha_anki_server ./matcha_anki_server

RUN useradd --create-home --uid 10001 matcha && mkdir /data && chown matcha /data
USER matcha
ENV DATA_DIR=/data PORT=8080 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health')"
CMD ["python", "-m", "matcha_anki_server"]
