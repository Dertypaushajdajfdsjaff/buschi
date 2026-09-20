FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl unzip libopus0 \
    && curl -fsSL https://deno.land/install.sh | sh \
    && mv /root/.deno/bin/deno /usr/local/bin/deno \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -U --pre "yt-dlp[default]"
# ^ Nightly + Extras (u.a. yt-dlp-ejs): YouTube ändert sich ständig, Fixes landen
#   zuerst im Nightly-Build.

COPY bot.py .

# Optionale cookies.txt fürs YouTube-Login (siehe README).
# Das Klammer-Pattern sorgt dafür, dass der Build auch klappt, wenn die
# Datei (noch) nicht existiert - Docker bricht dann nicht ab.
COPY cookies.tx[t] ./

ENV PYTHONUNBUFFERED=1

# yt-dlp bei jedem Start aktualisieren, damit ein zwischengespeicherter
# Docker-Build-Layer nie eine veraltete Version festhält (Fehler ist unkritisch).
CMD ["sh", "-c", "pip install --no-cache-dir -U --pre 'yt-dlp[default]' || true; exec python -u bot.py"]
