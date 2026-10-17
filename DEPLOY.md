# Деплой «АРХИВ» на хостинг

Бот работает через **long polling** (исходящие соединения наружу), поэтому ему не
нужен домен, SSL и webhook — ему нужен только постоянно работающий процесс.
Юзербот — второй процесс, клиентской библиотеке Telethon нужен интерактивный
первый вход (телефон/код), что надо учитывать при выборе способа.

## Вариант A — VPS (рекомендую: самый дешёвый и надёжный)

Подойдёт любой VPS за ~150–300 ₽/мес (1 ГБ RAM хватает с запасом):
Timeweb, Beget, aeza, Hetzner, DigitalOcean… ОС: Ubuntu 22.04/24.04.

### 1. Подготовка сервера (один раз)

```bash
sudo apt update && sudo apt install -y python3-venv git
sudo adduser --disabled-password --gecos "" archive
sudo mkdir -p /opt/autotg && sudo chown archive:archive /opt/autotg
```

### 2. Код: с GitHub или scp

```bash
# вариант git:
sudo -u archive git clone https://github.com/ТВОЙ_ЛОГИН/autotg.git /opt/autotg
# или залили файлы по scp:
scp -r C:/Users/user/Desktop/autotg/hosting/* root@IP_СЕРВЕРА:/opt/autotg/
```

### 3. Зависимости и конфиг

```bash
cd /opt/autotg
sudo -u archive python3 -m venv venv
sudo -u archive venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env   # впиши BOT_TOKEN (и API_ID/API_HASH, если юзербот нужен)
```

### 4. Первый вход юзербота (если используешь)

Interaktivный вход Telethon требует терминал:

```bash
sudo -u archive env TERM=xterm-256color sh -c 'cd /opt/autotg && ./venv/bin/python userbot.py'
# введи телефон -> код из Telegram -> 2FA (если есть); Ctrl+C после "успешного входа"
```

Сессия сохранится в `/opt/autotg/save_session.session` — дальше сервис зайдёт сам.

### 5. Автозапуск через systemd

Установи юниты из этой папки (поправь в них `User=` и пути, если отличаются):

```bash
sudo cp archive-bot.service archive-userbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now archive-bot
# юзербот, если он используется (иначе пропусти):
sudo systemctl enable --now archive-userbot
```

Проверка и логи:

```bash
systemctl status archive-bot
journalctl -u archive-bot -f        # живые логи
journalctl -u archive-userbot -f
```

⚠️ Юзербот подключён к твоему аккаунту: правила Telegram запрещают массовый
спам, аккаунт могут ограничить/забанить. Держи задержку ≥0.34 с и малые объёмы.

## Вариант B — Docker на VPS

Нужен установленный Docker + compose plugin (одной командой: официальный
`get.docker.com`). Дальше в папке проекта на сервере:

```bash
cp .env.example .env && nano .env          # BOT_TOKEN, API_ID/API_HASH
docker compose up -d bot                   # только бот
# или оба контейнера:
docker compose up -d
```

Первый вход юзербота в Docker (телефон/код — интерактивно):

```bash
docker compose run --rm userbot            # зайдёшь по коду, сессия уйдёт в том
# после успешного входа: docker compose up -d userbot
```

База и медиа живут в named-volume `archive-data` (`/data` внутрь контейнера) —
переживают пересоздание контейнеров.

Полезное:

```bash
docker compose logs -f bot
docker compose restart userbot
docker compose down && docker compose up -d   # обновление=None, код прилетает при rebuild
docker compose up -d --build                  # пересобрать после изменений в коде
```

## Вариант C — PaaS (Railway / Render / Fly.io)

Бот — как **worker-процесс** (это не веб-сервис: домен не нужен, трафик Bot API
не приходит, он сам наружу ходит). Юзербот лучше туда не пихать — интерактивный
вход там сделать сложно.

- Railway: New Project → Deploy from repo → в Settings поставь
  "Start Command" = `python bot.py`. Переменные: `BOT_TOKEN`, `DB_PATH=/data/data.db`,
  `MEDIA_PATH=/data/media`, и подключи Volume на `/data` (без сохраняемого тома
  вся история будет теряться при деплое).
- Render: New → Background Worker → команда `python bot.py`, там же переменные.
- Fly.io: `fly launch --no-deploy`, добавь volume, `fly deploy`.
  Procfile в корне репо этим платформам удобно подхватывается (`web:` = бот).

База данных тут — просто файл SQLite в volume; отдельная БД (Postgres) не нужна,
код на неё не рассчитан.

## Чек-лист после запуска

1. В Telegram: `/start` боту → приходит приветствие «АРХИВ».
2. Настройки → Telegram Business → Чат-боты → выбрать бота, разрешить чтение
   сообщений (для `/spam` ещё «Отвечать на сообщения» в «Управление сообщениями»).
3. Напиши себе/тестовому аккаунту сообщение → в личке с ботом появляется копия.
4. `/stats`, `/history 5`, `/spam 3 test` — работают.
5. Если юзербот запущен: `SELECT * FROM meta WHERE k='userbot_heartbeat';` в
   базе живой, а `/spam` уходит от твоего имени.

## Бэкап (важно!)

Вся ценность — в базе и медиа:

```bash
# VPS-вариант:
sqlite3 /opt/autotg/data.db ".backup '/backup/autotg-$(date +%F).db'"
tar czf /backup/autotg-media-$(date +%F).tgz /opt/autotg/media
```

Docker-вариант: скопируй том перед заменой (`docker run --rm -v archive-data:/d -v
/backup:/b alpine tar czf /b/data.tgz -C /d .`).
