`/hosting` — всё, что нужно, чтобы выложить проект «АРХИВ» на GitHub и
запустить его на хостинге (VPS, Docker-хостинг или PaaS).

## Что здесь лежит

| Файл | Назначение |
|---|---|
| `bot.py` | Основной бот (Bot API, Telegram Business). |
| `userbot.py` | Юзербот (Telethon): спам от твоего имени + скачивание скрытой медиа. |
| `spam_user.py` | CLI: вручную поставить задачу спама в очередь. |
| `database.py` | Слой SQLite (общая база бота и юзербота). |
| `requirements.txt` | Зависимости (`python-telegram-bot`, `python-dotenv`, `telethon`). |
| `.env.example` | Шаблон секретов. На хостинге переменные задаются в панели или в `.env` на сервере. |
| `.gitignore` | Скрывает секреты, базу, медиа, сессии — в репо не попадут. |
| `Dockerfile` | Один образ для бота и юзербота. |
| `docker-compose.yml` | Два контейнера (bot + userbot) с общим томом `/data`. |
| `Procfile` | Для Heroku/Railway/Render/Fly.io: `web` = бот, `worker` = юзербот (отдельное приложение). |
| `archive-bot.service` / `archive-userbot.service` | systemd-юниты для VPS (Ubuntu/Debian). |
| `run_bot.bat` / `run_userbot.bat` | Запуск на Windows (для локального теста). |
| `DEPLOY.md` | Пошаговая инструкция по деплою. |
| `GITHUB.md` | Как залить проект на GitHub (что коммитить, что скрывать). |

## ВАЖНО: не выкладывай на GitHub

- `.env` — там токен бота и API-ключи. Выложил = скомпрометировал (перевыпускай через BotFather).
- `data.db`, `data.db-*` — вся переписка и личные данные внутри.
- `media/` — скачанные медиа из переписок.
- `*.session` — сессия юзербота = полный доступ к аккаунту.

Всё это уже перечислено в `.gitignore` в этой папке.
