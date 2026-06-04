ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base:latest
FROM $BUILD_FROM

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN apk add --no-cache ffmpeg python3

ENV VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY requirements.txt .
RUN uv venv $VIRTUAL_ENV && uv pip install --no-cache -r requirements.txt

COPY run.sh sync.py ./
RUN chmod +x run.sh

CMD ["/app/run.sh"]
