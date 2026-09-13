# Разработка и подготовка GitHub

## Локальная разработка

```shell
uv sync --locked
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
```

Тесты используют временные настройки и HTTP-заглушки Telegram. Они не отправляют реальные
сообщения и не меняют пользовательскую конфигурацию Codex.

Проверка исходников с отдельным конфигом:

```shell
uv run --frozen notification-mcp --config config.local.toml setup --local
uv run --frozen notification-mcp --config config.local.toml start
uv run --frozen notification-mcp --config config.local.toml test
uv run --frozen notification-mcp --config config.local.toml stop
```

Обычный `uv tool install .` копирует программу в отдельное окружение.
Изменение исходников требует переустановки. Для разработки доступен
`uv tool install --editable .`, но тогда папку исходников удалять нельзя.

## Пакет и GitHub

```shell
uv build
```

В `dist/` появятся wheel и архив исходников. Имя дистрибутива — `notification-mcp-relay`,
а установленная команда остаётся `notification-mcp`. Wheel устанавливается командой
`uv tool install путь/к/пакету.whl`.

GitHub Actions выполняет полный Ruff/pytest/build один раз на Linux и короткий smoke-check запуска
на Windows и macOS. После push дождитесь зелёного workflow **Check**. Workflow **Release** не
повторяет полный набор: проверяет версию тега, собирает и устанавливает wheel в чистое окружение,
затем создаёт GitHub Release с wheel и sdist.

Подготовка версии `0.4.0`:

```shell
git add .
git commit -m "Release notification-mcp 0.4.0"
git push origin main
git tag -a v0.4.0 -m "notification-mcp 0.4.0"
git push origin v0.4.0
```

Перед тегом версия в `pyproject.toml` и `src/notification_mcp/__init__.py` должна совпадать.
Публикация в PyPI не требуется: релиз устанавливается из GitHub по тегу или из wheel.

В репозиторий входят исходники, тесты, docs, примеры конфигурации, `install.ps1`, `LICENSE`,
`pyproject.toml`, `uv.lock` и `.github`. Личные `config.toml`, `var`, `.venv`, журналы
и runtime-файлы исключены из Git; sdist собирается по явному списку включаемых путей.

## Структура

| Модуль | Ответственность |
|---|---|
| `models.py`, `config.py` | Общие события, настройки и маршруты |
| `channels.py`, `telegram.py` | Доставка и приём `/start` |
| `store.py`, `worker_lock.py` | SQLite и единственный обработчик |
| `service.py`, `server.py` | Очередь, повторы, MCP и HTTP/stdio |
| `installation.py` | Личные настройки и импорт |
| `runtime.py` | Фоновый запуск и корректная остановка |
| `integrations.py` | Настройки клиентов и подключение Codex |
| `diagnostics.py`, `cli.py` | Проверка MCP и команды пользователя |

Для нового канала добавьте конфигурацию/назначение в `config.Channel` и `config.Destination`,
адаптер `validate`/`send` и регистрацию в `ADAPTER_TYPES` и `NotificationService.running`.
Публичные инструменты `notify`/`notification_status` остаются общими.
Ошибки доставки используют `DeliveryError` с безопасным текстом и признаком повторяемости.
