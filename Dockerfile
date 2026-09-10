
FROM python:3.12-slim

# FFmpeg installieren
RUN apt-get update \
    && apt-get install -y ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Arbeitsordner
WORKDIR /app

# Python-Abhängigkeiten zuerst installieren
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Bot-Code kopieren
COPY bot.py .

# Bot starten
CMD ["python", "bot.py"]
