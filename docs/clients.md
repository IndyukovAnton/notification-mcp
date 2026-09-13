# Подключение MCP-клиента

## Универсальный stdio-профиль

`notification-mcp` работает как обычный локальный stdio MCP-сервер. Клиент запускает одну
команду без аргументов и задаёт токен только в окружении этого процесса:

```json
{
  "mcpServers": {
    "notifications": {
      "command": "notification-mcp",
      "env": {
        "TELEGRAM_BOT_TOKEN": "ТОКЕН_ОТ_BOTFATHER"
      }
    }
  }
}
```

В форме подключения заполните:

- Name: `notifications`;
- Connection/Transport: `On this computer` или `STDIO`;
- Command: `notification-mcp`;
- Arguments: оставить пустыми;
- Environment: ключ `TELEGRAM_BOT_TOKEN`, значение — токен BotFather;
- Working directory: оставить пустой.

Если GUI не находит короткую команду, укажите абсолютный путь к launcher. Это свойство среды
запуска клиента, а не отдельный режим сервера.

Готовая заготовка с placeholder:

```shell
notification-mcp client-config json --token-env
```

Токен не передаётся аргументом командной строки и не требует системной переменной. Он хранится
локально в конфигурации MCP-хоста, как у других серверов с полем `env`.

## Codex

Автоматическое подключение одной командой:

```shell
notification-mcp connect codex --token-env
```

Команда скрыто запросит токен и добавит:

```toml
[mcp_servers.notifications]
command = "notification-mcp"

[mcp_servers.notifications.env]
TELEGRAM_BOT_TOKEN = "ТОКЕН_ОТ_BOTFATHER"
```

Если `notifications` уже существует в другом виде:

```shell
codex mcp remove notifications
notification-mcp connect codex --token-env
```

После изменения полностью перезапустите клиент. Codex, ChatGPT desktop и IDE extension используют
общий пользовательский конфиг Codex. Формат `command`, `args` и `env` описан в
[официальной документации OpenAI](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

## Жизненный цикл

Клиент запускает `notification-mcp` при подключении и завершает процесс вместе с MCP-сессией.
Команды `start` и `stop` для stdio не нужны. Одновременно запускайте только один процесс с одной
базой; для нескольких параллельных клиентов используйте общий Streamable HTTP-сервер.

## Иконка и отображение пути

Сервер передаёт стандартные MCP-метаданные: название, URL проекта и PNG-иконку. Отображать их или
оставить стандартную иконку решает сам клиент.

Локальный путь рядом с `@notifications` означает только то, что интеграция запускается на этом
компьютере. Это не имя инструмента и не часть промпта. Скрыть происхождение локального процесса
можно только переходом на удалённый HTTP MCP-сервер.

## Команды агенту

Упоминание через `@` не обязательно. Достаточно явно назвать сервер и инструмент:

> Используй MCP-сервер notifications. Вызови notify с сообщением «Проверка подключения» и
> событием info, затем проверь результат через notification_status.

Чтобы агент использовал уведомления постоянно, добавьте в его пользовательские или проектные
инструкции:

> Когда требуется моё решение, вызывай notify сервера notifications с событием action_required.
> Когда работа готова к проверке — review_requested. После вызова проверяй notification_status.

Подключение MCP само по себе не создаёт slash-команду и не обязывает модель вызывать инструмент:
решение принимает MCP-хост и модель на основании доступных tools и текста промпта.

## Фоновый HTTP-режим

Для нескольких клиентов:

```shell
notification-mcp setup
notification-mcp start
notification-mcp client-config json
```

Стандартный адрес: `http://127.0.0.1:8765/mcp`.
