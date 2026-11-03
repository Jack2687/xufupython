# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM golang:1.25-bookworm AS postpayment
WORKDIR /build/postpayment
COPY go-post-payment/go.mod go-post-payment/go.sum ./
RUN go mod download
COPY go-post-payment/ ./
RUN CGO_ENABLED=0 go build -trimpath -o /post-payment .

FROM python:3.11-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=18970 LISTEN_HOST=0.0.0.0
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home app
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=app:app backend/ ./backend/
COPY --chown=app:app generator.py ./
COPY --chown=app:app scripts/serve.py scripts/recover_post_payment.py ./scripts/
COPY --from=frontend --chown=app:app /build/webui/ ./webui/
COPY --from=postpayment --chown=app:app /post-payment ./bin/post-payment
ARG VCS_REF=local
LABEL org.opencontainers.image.source="https://github.com/yigerende/xufupython" \
      org.opencontainers.image.revision=$VCS_REF
USER app
EXPOSE 18970
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18970/health/ready', timeout=4)"
CMD ["python", "scripts/serve.py"]
