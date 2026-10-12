# Self-hosted AI Trading Lab (authenticated FastAPI dashboard + scheduler). NOT required: GitHub Actions + Pages is the free default.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 AITL_DB=/data/ailab.db AITL_LIVE_DIR=/data/site_live
RUN useradd -m -u 10001 app && mkdir -p /data && chown app /data
WORKDIR /srv/app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN chown -R app /srv/app
USER app
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
