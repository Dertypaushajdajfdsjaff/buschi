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
    && pip install --no-cache-dir -U yt-dlp

COPY bot.py .

# Optionale cookies.txt fürs YouTube-Login (siehe README).
# Das Klammer-Pattern sorgt dafür, dass der Build auch klappt, wenn die
# Datei (noch) nicht existiert - Docker bricht dann nicht ab.
COPY cookies.tx[t] ./

ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "bot.py"]
