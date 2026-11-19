# syntax=docker/dockerfile:1
#
# The solana-future-coin image. ARM64, because the instance is a Graviton t4g.micro. Two stages (ported from
# ai-kucoin-future): the builder compiles wheels into a venv; the runtime copies the venv only, so no compiler ships
# on a box that will one day hold a trading key. No tests, no .env in the image: the .env is bind-mounted at run time.

# deploy.yml passes whichever registry answered first (public.ecr.aws, mirror.gcr.io, docker.io serve the same image).
ARG BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim-bookworm

FROM ${BASE_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_DEFAULT_TIMEOUT=60 PIP_RETRIES=10
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN python -m venv /venv
ENV PATH="/venv/bin:${PATH}"
COPY app/requirements.txt /build/requirements.txt
RUN pip install --no-cache-dir -r /build/requirements.txt

FROM ${BASE_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/venv/bin:${PATH}" \
    TZ=UTC
COPY --from=builder /venv /venv
# uid 1000 = the owner hostSetup.sh gives /opt/solana-future-coin/data; a mismatch fails at the first write, not at start.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin bot
WORKDIR /app
COPY --chown=1000:1000 app /app/app
RUN rm -rf /app/app/tests \
    && find /app -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && mkdir -p /app/data /app/logs \
    && chown -R 1000:1000 /app/data /app/logs
USER 1000:1000
# One image, two programs (D-30): the scout's /health on 8080, the trader's on 8081.
EXPOSE 8080 8081
# /health fails when a loop goes quiet, not only when the process dies. 120 s start: migrations + first market load.
# HEALTH_URL is set per service in docker-compose.yml; the default is the trader's.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(os.environ.get('HEALTH_URL', 'http://127.0.0.1:8081/health'), timeout=8).status == 200 else 1)"]
# CMD, never ENTRYPOINT (LESSON L-14): docker-compose picks the program with `entrypoint:`. A compose `command:` is
# APPENDED to an image ENTRYPOINT as arguments, so with the old `ENTRYPOINT app.main` both services would have run the
# whole old bot twice. Exec form: SIGTERM reaches Python, which flushes the recorder and stops the loops cleanly.
CMD ["python", "-m", "app.trader"]
