# АРХИВ — Telegram Business бот + юзербот (Telethon)
# Образ работает и для bot.py, и для userbot.py — команда задаётся в docker-compose.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py userbot.py spam_user.py database.py ./

# Каталоги для базы и медиа (можно переопределить через DB_PATH / MEDIA_PATH)
RUN mkdir -p /data/media
ENV DB_PATH=/data/data.db \
    MEDIA_PATH=/data/media

CMD ["python", "bot.py"]
