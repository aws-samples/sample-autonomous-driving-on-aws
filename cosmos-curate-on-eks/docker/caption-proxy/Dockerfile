# Strands-on-Bedrock caption endpoint (OpenAI-compatible) for Curate's openai captioner.
FROM public.ecr.aws/docker/library/python:3.12-slim
ARG STRANDS_VERSION=1.60.0
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "strands-agents==${STRANDS_VERSION}"
COPY app.py /app/app.py
USER 1000:1000
EXPOSE 8080
CMD ["python", "/app/app.py"]
