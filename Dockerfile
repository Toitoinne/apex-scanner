FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 10001 apex
WORKDIR /app

COPY pyproject.toml ./
COPY apex ./apex
RUN pip install .

COPY sql ./sql
COPY tests ./tests
RUN mkdir -p /data && chown apex:apex /data
USER apex
ENV APEX_CONFIG=/app/config/config.yaml APEX_SAFETY=/app/config/safety.yaml DATA_DIR=/data
ENTRYPOINT ["python", "-m", "apex"]
