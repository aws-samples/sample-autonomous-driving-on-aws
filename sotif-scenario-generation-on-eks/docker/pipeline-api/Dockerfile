# Pipeline API (EKS Deployment). Build context: repository root.
#   docker build -f docker/pipeline-api/Dockerfile -t drive-atlas/pipeline-api:<tag> .
FROM public.ecr.aws/docker/library/python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir fastapi==0.162.4 uvicorn==0.54.0 boto3==1.44.0 kubernetes==37.0.1

WORKDIR /app
COPY augment /app/augment
COPY sotif /app/sotif
COPY scripts/augment_generate.py /app/scripts/
COPY pipeline_api /app/pipeline_api

RUN useradd --uid 10001 --create-home api
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/pipeline/health', timeout=2)"]
CMD ["python3", "-m", "uvicorn", "pipeline_api.app:app", "--host", "0.0.0.0", "--port", "8080"]
