FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y ffmpeg curl unzip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Deno JS-Runtime: yt-dlp braucht seit Version 2025.11.12 eine externe
# JavaScript-Runtime, um YouTubes Signatur-Challenges zu lösen. Ohne sie
# liefert YouTube stark eingeschränkte/keine Formate ("Requested format
# is not available"). Deno wird hier systemweit nach /usr/local/bin installiert.
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y \
    && mv /root/.deno/bin/deno /usr/local/bin/deno \
    && deno --version

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY cookies.txt .

ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "bot.py"]
