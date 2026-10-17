# Как выложить «АРХИВ» на GitHub

## Главный принцип

В Git должны попасть **только исходники и конфиги-шаблоны**. Всё, что содержит
твои секреты или чужие переписки, должно остаться на диске и не попасть в
коммит. `.gitignore` (он уже в этой папке) это обеспечивает.

## Что попадёт в репозиторий

`bot.py`, `userbot.py`, `spam_user.py`, `database.py`, `requirements.txt`,
`.env.example`, `.gitignore`, `README.md`, `Dockerfile`, `docker-compose.yml`,
`Procfile`, `*.service`, `run_bot.bat`, `run_userbot.bat`, `DEPLOY.md`.

## Что НЕ попадёт (и не должно)

| Что | Почему |
|---|---|
| `.env` | Токен бота, API_ID/API_HASH. Токен = полный контроль над ботом. |
| `data.db`, `data.db-shm/wal` | Вся переписка и события — личные данные. |
| `media/` | Скачанные фото/видео людей. |
| `*.session`, `*.session-journal` | Сессия Telethon = вход в твой аккаунт. |
| `__pycache__/`, `*.pyc` | Мусор от Python. |
| `assets/` | Личные картинки-исходники (не нужны коду). |
| "Название Архив бот.txt" | Внутреннее ТЗ — решай сам, обычно не выкладывают. |

## Пошагово

### 1. Создай пустой репозиторий на GitHub

New repository → имя, например `autotg` → Public или Private → **без** README/
.gitignore/license (мы сами всё добавим). Для проекта с личной перепиской
рекомендую **Private**.

### 2. В корне проекта (не в `hosting/`) подготовь файлы

Вариантов два — выбери один:

**Вариант А (рекомендую): корень = содержимое `hosting/`**
Скопируй всё из этой папки в корень `C:\Users\user\Desktop\autotg` (README.md
заменит старый — новый общий), а папку `hosting/` не коммить (добавь её в
`.gitignore`, чтобы двойной копии в репо не было).

**Вариант Б: оставить корень как есть, `hosting/` — как подпапку**
Тогда в корневом `.gitignore` добавь строку `hosting/` не надо — наоборот,
`hosting/` коммитим, а игнор-правила уже защищают secrets/db/media. Репо будет
содержать обе инструкции — старый README и `hosting/README.md`.

### 3. Закоммить и запушь

```bash
cd C:\Users\user\Desktop\autotg
git init
git add .
git status            # ПРОВЕРЬ: в списке НЕ должно быть .env, data.db, media/, *.session
git commit -m "ARCHIVE: Telegram Business bot + userbot"
git branch -M main
git remote add origin https://github.com/ТВОЙ_ЛОГИН/autotg.git
git push -u origin main
```

`git status` на этапе проверки — обязательный шаг: если что-то лишнее просочилось,
`git rm --cached файл` и добавь имя в `.gitignore`.

### 4. Проверка репо в браузере

В корне репо должны быть видны `.py`-файлы, README, Dockerfile и т.д., и **не
должно быть** `.env` (GitHub его даже прячет из листинга только если имя
совпадает — положись на `.gitignore`, а не на приметы).

## Секреты на хостинге

Хостинг-платформы (Railway/Render/Fly/Heroku) читают переменные из своей панели
(Variables/Secrets), а не из файлов — просто задай там `BOT_TOKEN`, `API_ID`,
`API_HASH` и т.д. из `.env.example`. На VPS — `.env` рядом с кодом (не в git).

## Если секрет утёк

1. `@BotFather` → `/revoke` (или /mybot → API Token → Revoke) — новый токен в `.env` на сервере.
2. my.telegram.org → API development tools → нет «сброса», но maxfollow-паника не нужна:
   скомпрометирован API_HASH сам по себе малоопасен без кода входа; смени пароль
   2FA и завершить все сеансы в Telegram («Settings → Devices → Terminate all»).
3. История Git: если коммит уже запушен — `git filter-repo` или пересоздай репо
   без истории (частный случай: `rm -rf .git`, init заново, force push).
