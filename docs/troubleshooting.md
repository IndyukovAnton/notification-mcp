# Решение проблем

Для stdio сначала перезапустите Codex и проверьте `/mcp`. Для фонового HTTP-режима выполните
`notification-mcp status` и `notification-mcp logs`.

| Симптом | Что сделать |
|---|---|
| Команда не найдена | `uv tool update-shell`, затем открыть новый терминал |
| Configuration not found | `notification-mcp setup`; для старого файла — `--config` |
| `stopped` | `notification-mcp start` |
| `unreachable` | Посмотрите `logs`. `stop` очистит старое состояние, если база свободна; затем `start` |
| Порт занят | Остановите прежний `serve`/другую копию или измените `server.port`, перезапустите и обновите URL клиента |
| Another delivery worker… | Та же база уже открыта сервером: остановите прежний HTTP/stdio-процесс |
| Telegram-чат не подключён | При работающем сервере отправьте боту `/start` в личном чате |
| Telegram 401 | Проверьте токен BotFather и выполните `restart` |
| Telegram 403 | Разблокируйте бота, отправьте `/start`, проверьте право писать в группу |
| Telegram 409 | Другой `getUpdates` или webhook. Используйте отдельного бота либо отключите прежний обработчик |
| Telegram connection | Проверьте интернет и доступ к Telegram API; временные ошибки повторяются |
| Codex содержит другую запись | Выполните `codex mcp remove notifications` и повторите `connect codex` |
| `notifications` отсутствует в `/mcp` | Выполните `notification-mcp connect codex`, затем перезапустите Codex |
| stdio завершается сразу | Проверьте конфиг через `notification-mcp check-config`; остановите HTTP-сервер с той же базой |
| `test` не подтвердил доставку | Проверьте `status`, `logs` и `/start`. После таймаута сообщение ещё может быть в очереди |

`status` показывает фоновый сервер, запущенный через `start`. Он не управляет
`serve` в другом терминале или процессом stdio MCP-клиента.
`stop` не завершает неизвестный процесс по номеру PID.

`check-config` проверяет файл и наличие токена локально. Команда `test` проверяет MCP
и отправляет настоящее сообщение через фоновый HTTP-сервер. В stdio-режиме попросите агента
вызвать `notify`, затем `notification_status`.

## Проверка без Telegram

В отдельном пустом каталоге:

```shell
notification-mcp --config check.toml setup --local
notification-mcp --config check.toml start
notification-mcp --config check.toml test
notification-mcp --config check.toml stop
```

Предварительно остановите основной сервер, если он использует стандартный порт 8765.
Уведомление появится в `var/notifications.jsonl` рядом с `check.toml`.
Проверяется весь локальный путь MCP → очередь → доставка без внешних отправок.
