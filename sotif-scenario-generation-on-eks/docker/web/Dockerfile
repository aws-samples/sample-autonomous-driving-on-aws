# Static web UI (generation page, gallery). Build context: repository root.
#   docker build -f docker/web/Dockerfile -t drive-atlas/web:<tag> .
FROM public.ecr.aws/docker/library/python:3.13-slim-trixie

WORKDIR /app
COPY web/ ./
COPY docker/web/serve.py /srv/serve.py

RUN useradd --uid 10001 --create-home web
USER 10001
EXPOSE 4173
ENV PORT=4173
HEALTHCHECK --interval=30s --timeout=3s CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4173/', timeout=2)"]
CMD ["python3", "/srv/serve.py"]
