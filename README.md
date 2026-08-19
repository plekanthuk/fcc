# FCC EAS Scanner (GitHub Actions)

Тягне дані з офіційної FCC Equipment Authorization Search (обходячи блокування
Akamai через `curl_cffi`) і пушить у WordPress-плагін **FCC Import Monitor**
через `POST /wp-json/fcc/v1/import`.

## Налаштування (один раз)

1. Створіть **приватний** GitHub-репозиторій, запуште туди вміст цієї папки
   (`fetch_fcc.py`, `requirements.txt`, `.github/workflows/scan.yml`).

2. У репозиторії: **Settings → Secrets and variables → Actions → New repository secret**,
   додайте три секрети:
   - `WP_URL` — напр. `https://ваш-сайт.com`
   - `WP_USER` — `adminlmteam`
   - `WP_APP_PASSWORD` — Application Password з пробілами (створюється в
     WordPress: Users → Profile → Application Passwords)

3. У WP-адмінці плагіна (`FCC Import → Scan → GitHub налаштування`) впишіть:
   - GitHub owner (ваш логін/організація)
   - GitHub repo (назва щойно створеного репозиторію)
   - GitHub PAT — Personal Access Token з правами `repo` + `workflow`
     (GitHub → Settings → Developer settings → Personal access tokens →
     Fine-grained, scope: цей репозиторій, permissions: Contents=Read,
     Actions=Read and write)

Після цього кнопка "Scan" в адмінці зможе тригерити workflow миттєво
(`repository_dispatch`), а щоденний прогін (учора) запускається сам о 06:00 UTC.

## Ручний запуск локально (для тестування)

```bash
pip install -r requirements.txt

# подивитись, скільки записів знайде, без пушу в WP
python fetch_fcc.py --date-from 2026-08-18 --date-to 2026-08-18 --dry-run

# реальний пуш (потрібні env WP_URL / WP_USER / WP_APP_PASSWORD)
export WP_URL=https://ваш-сайт.com
export WP_USER=adminlmteam
export WP_APP_PASSWORD="xxxx xxxx xxxx xxxx xxxx xxxx"
python fetch_fcc.py --date-from 2026-08-18 --date-to 2026-08-18 --scan-type manual --enrich
```

## Історичний імпорт

```bash
python fetch_fcc.py --date-from 2021-01-01 --date-to 2026-08-19 --scan-type historical
```

Скрипт сам розбиває діапазон на місяці (кожен місяць — окремий запит +
окремий запис у `wp_fcc_scan_log`). `--enrich` для історичного імпорту
не рекомендується вмикати одразу (у кожного запису +1-2 запити) — краще
спершу занести самі записи, а збагачення (Equipment Class / документи)
донакотити окремим прогоном пізніше, вибірково.

## Чому не просто Python на десктопі / не PHP на сервері

- **PHP-скрипт на будь-якому сервері** (включно з вашим Plesk) не може
  напряму піти в apps.fcc.gov — Akamai блокує за TLS-відбитком з'єднання,
  а не за IP чи заголовками, тож звичайний `curl`/`file_get_contents` завжди
  отримає 403, хоч би де він виконувався.
- `curl_cffi` (тут) імітує TLS-відбиток справжнього Chrome — цим і проходить.
- GitHub Actions обраний як "завжди-увімкнений" виконавець, що не залежить
  від вашого домашнього/робочого ПК — безкоштовний scheduled workflow.
