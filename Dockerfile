FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home app \
    && install -d -o app -g app /opt/venv

COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# The virtualenv belongs to the app user so the daily yt-dlp update can write to it.
USER app
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app

COPY requirements.txt .
RUN python -m venv /opt/venv && pip install --no-cache-dir --pre -r requirements.txt

COPY bot ./bot

CMD ["python", "-m", "bot"]
