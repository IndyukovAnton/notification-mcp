# Настройки и каналы

`setup` создаёт личный `config.toml`; точный путь показывает `notification-mcp status`.
Повторный `setup` не перезаписывает настройки. Для изменения токена отредактируйте файл
и выполните `notification-mcp restart`.

Для отдельного конфига укажите `--config` **перед командой**:

```shell
notification-mcp --config path/to/config.toml start
notification-mcp --config path/to/config.toml stop
```

Приоритет: `--config`, переменная `NOTIFICATION_MCP_CONFIG`, личная конфигурация.
Файл из текущего каталога автоматически не подхватывается.
Относительные пути внутри TOML считаются от расположения этого TOML.

## Telegram

Минимальный конфиг:

```toml
[routing]
default_channel = "personal"

[channels.personal]
type = "telegram"
bot_token = "ТОКЕН_ОТ_BOTFATHER"
```

При отсутствии `chat_id` сервер получает `/start` через long polling:

1. Первый `/start` в личном чате закрепляет владельца и получателя.
2. Этот же аккаунт может выбрать группу, добавив туда бота и отправив `/start@имя_бота`.
3. `/start` в личном чате возвращает уведомления туда.

Чужие, анонимные и пересланные команды игнорируются. Бот должен иметь право писать в группу.
Темы форумов и каналы Telegram через `/start` не поддерживаются.
Для одного бота разрешён один автоматически настраиваемый канал.

Для фиксированного адреса добавьте `chat_id = "123456789"`: обработчик `/start` отключится.
Вместо `bot_token` можно указать `bot_token_env = "TELEGRAM_BOT_TOKEN"`. Если заданы оба,
используется `bot_token`. `.env` автоматически не загружается.

Используйте отдельного бота: другой потребитель `getUpdates` или webhook вызывает конфликт.
Сервер не удаляет чужой webhook. Подробнее — [решение проблем](troubleshooting.md).

## Общий интерфейс и выбор доставки

```toml
[routing]
default_channel = "local"

[routing.events]
action_required = "personal"
review_requested = "personal"
error = "personal"

[channels.local]
type = "file"
path = "var/events.jsonl"

[channels.personal]
type = "telegram"
bot_token = "ТОКЕН_ОТ_BOTFATHER"
```

Клиент вызывает `notify`:

```json
{
  "message": "Реализация готова. Нужно проверить результат.",
  "event": "review_requested",
  "title": "Задача завершена",
  "source": "project / task-42",
  "url": "https://example.com/result",
  "idempotency_key": "project:42:ready:v1"
}
```

Обязателен `message`. События: `info`, `action_required`, `review_requested`, `error`.
Ограничения: сообщение 3000 символов, заголовок и источник по 160, URL 500.
Telegram ограничивает сформированный текст 4096 единицами UTF-16.
Текст отправляется без Markdown/HTML. Файловый канал записывает JSONL, без баннера и звука.

Получатель и идентификатор бота фиксируются в очереди; изменение маршрута влияет на новые записи.
Секрет в очередь не записывается. Ротация токена того же бота поддерживается после перезапуска.
История хранится до обслуживания базы владельцем; автоматической очистки нет.

## Очередь и повторные попытки

```toml
[server]
host = "127.0.0.1"
port = 8765

[storage]
database = "var/notifications.sqlite3"

[delivery]
poll_interval_seconds = 1
request_timeout_seconds = 10
max_attempts = 8
retry_base_seconds = 5
retry_max_seconds = 300
```

Telegram `retry_after` учитывается как минимальная задержка. Ошибки 400/401/403 не повторяются.
После исчерпания попыток запись получает `failed`; для новой отправки после устранения причины
используйте новый ключ. При потере подтверждения уже отправленного сообщения возможен дубль.

Постановка локально, в том числе при остановленном сервере:

```shell
notification-mcp notify --message "Готово к проверке" --event review_requested
notification-mcp status ID_ИЗ_ОТВЕТА
```

До первой привязки `/start` Telegram-уведомления отклоняются. Другие настроенные каналы работают.
SQLite и её lock-файлы должны находиться на локальном диске.
