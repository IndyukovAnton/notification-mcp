# Notification MCP

**Уведомления из MCP-клиента — в выбранный вами канал.** Агент сообщает, что работа готова,
нужно решение или возникла ошибка. Сервер выбирает получателя и доставляет сообщение.

Сейчас доступны Telegram-бот и локальный файл для проверки без интернета. Инструменты
`notify` и `notification_status` общие для всех каналов. Поддерживаются Streamable HTTP и stdio.

## Быстрый старт с Codex

Рекомендуемый режим — stdio. Codex сам запускает локальный сервер при подключении и завершает
его вместе с MCP-соединением. Отдельные `start` и `stop` не нужны.

### 1. Установить

Нужен [uv](https://docs.astral.sh/uv/getting-started/installation/) — он установит подходящий
Python и зависимости автоматически. Если uv ещё нет, выполните один раз:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

После публикации релиза установите зафиксированную версию из GitHub:

```powershell
uv tool install --python 3.11 "git+https://github.com/IndyukovAnton/notification-mcp.git@v0.4.0"
uv tool update-shell
```

Откройте **новый терминал**. Для установки по Git URL требуется Git. Без него скачайте wheel
со страницы GitHub Release и выполните:

```powershell
uv tool install --python 3.11 .\notification_mcp_relay-0.4.0-py3-none-any.whl
```

<details>
<summary>Установка из ZIP с исходниками</summary>

В GitHub нажмите **Code → Download ZIP**, распакуйте архив и откройте терминал в этой папке.
На Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

На Linux и macOS:

```shell
uv tool install --python 3.11 .
uv tool update-shell
```

</details>

### 2. Настроить Telegram и подключить Codex

Создайте бота через [@BotFather](https://t.me/BotFather), затем выполните одну команду:

```shell
notification-mcp connect codex --token-env
```

Команда скрыто запросит токен и сохранит его как пару `TELEGRAM_BOT_TOKEN = значение`
непосредственно в локальной записи MCP-сервера. Системная переменная ОС и отдельный
`config.toml` не создаются.

Перезапустите Codex и проверьте `/mcp`: сервер `notifications` должен быть активен. Затем откройте
личный чат со своим ботом, отправьте `/start` и дождитесь ответа
«Чат подключён. Новые уведомления будут приходить сюда».

Первый пользователь с `/start` становится владельцем привязки — выполните этот шаг сами.
После перезапуска токен и чат вводить заново не нужно.

> Уже пользовались предыдущей версией? Сначала прочитайте
> [как перенести существующие настройки и очередь](docs/migration.md).

### Универсальная ручная настройка

Любой локальный MCP-клиент должен запускать один и тот же executable без аргументов и передавать
токен в собственном объекте `env`. Для JSON-конфигураций:

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

Эквивалент для Codex:

```toml
[mcp_servers.notifications]
command = "notification-mcp"

[mcp_servers.notifications.env]
TELEGRAM_BOT_TOKEN = "ТОКЕН_ОТ_BOTFATHER"
```

Если приложение не видит `notification-mcp` через `PATH`, укажите полный путь к установленному
launcher. Сервер принимает те же поля `command`, `args` и `env`, что и обычные stdio MCP-серверы.
Готовый JSON с безопасным placeholder выдаёт `notification-mcp client-config json --token-env`.
Для нескольких каналов и собственной маршрутизации остаётся расширенный режим с `config.toml`.

### 3. Проверить и использовать

Напишите агенту в Codex:

> Вызови notify сервера notifications: сообщение «Проверка подключения», событие info.
> Затем проверь доставку через notification_status.

Ожидаются уведомление в Telegram и состояние `sent`. В дальнейших задачах достаточно поручения:

> По завершении работы отправь через notifications уведомление с событием review_requested.
> Кратко напиши, что сделано и что мне проверить.

MCP-клиент должен сам вызвать инструмент: сервер не следит за ходом работы агента. Постоянное
правило для Codex приведено в [инструкции подключения](docs/clients.md#команды-агенту).

## Фоновый HTTP-режим

Используйте его, если один сервер должен одновременно обслуживать несколько MCP-клиентов:

```shell
notification-mcp setup
notification-mcp start
notification-mcp connect codex --transport http
notification-mcp test
```

В HTTP-режиме терминал можно закрыть, но после перезагрузки компьютера нужно снова выполнить
`notification-mcp start`. Для другого клиента используйте `http://127.0.0.1:8765/mcp` или
готовый JSON из `notification-mcp client-config`.

| Команда | Действие |
|---|---|
| `notification-mcp start` | Запустить фоновый HTTP-сервер |
| `notification-mcp stop` | Завершить текущую попытку отправки и остановить сервер |
| `notification-mcp restart` | Перезапустить и применить изменённые настройки |
| `notification-mcp status` | Показать состояние фонового сервера, адрес и пути файлов |
| `notification-mcp logs` | Показать последние сообщения сервера |
| `notification-mcp test` | Отправить проверочное уведомление и дождаться результата |

`start`/`stop`/`status` не управляют stdio-процессом: его жизненным циклом управляет MCP-клиент.

## Где хранятся настройки

| ОС | Конфигурация по умолчанию |
|---|---|
| Windows | `%LOCALAPPDATA%\notification-mcp\config.toml` |
| Linux | `$XDG_CONFIG_HOME/notification-mcp/config.toml` или `~/.config/notification-mcp/config.toml` |
| macOS | `~/Library/Application Support/notification-mcp/config.toml` |

Рядом сохраняются очередь и привязка Telegram; фоновый HTTP-режим также сохраняет журнал.
В рекомендуемом stdio-режиме токен хранится в локальной настройке самого MCP-клиента в поле
`env`; отдельная системная переменная и сервисный TOML не нужны. Как и у других локальных
MCP-серверов, этот секрет хранится открытым текстом в конфигурации выбранного клиента.

Для обновления закройте Codex, остановите HTTP-сервер, если он используется, и установите новый
тег с `--force`. Например:

```shell
notification-mcp stop
uv tool install --python 3.11 --force "git+https://github.com/IndyukovAnton/notification-mcp.git@v0.4.0"
```

`setup` повторять не нужно. Для удаления закройте Codex и выполните:

```shell
codex mcp remove notifications
notification-mcp stop
uv tool uninstall notification-mcp-relay
```

Личные настройки и история при удалении программы сохраняются.

## Как работает доставка

- `notify` сохраняет сообщение в SQLite и возвращает ID. `queued` означает принятие в очередь.
- Временные ошибки соединения повторяются с увеличением задержки; очередь переживает перезапуск.
- `notification_status` показывает `queued`, `sending`, `sent` или `failed`.
- `sent` означает подтверждение канала доставки, а не прочтение человеком.
- Получатель фиксируется при постановке в очередь. Смена чата влияет на новые уведомления.
- После исчерпания попыток сообщение получает `failed`. При неоднозначном сетевом сбое возможен дубль.

MCP-клиент должен сам вызвать инструмент: сервер не следит за ходом работы агента.
Локальный адрес доступен приложениям на этом компьютере. Для удалённого облачного клиента
потребуется отдельное развёртывание с защищённым доступом; текущий HTTP-сервер слушает только localhost.

## Подробнее

- [Настройки, выбор каналов и команды Telegram](docs/configuration.md)
- [Подключение Codex и других MCP-клиентов](docs/clients.md)
- [Решение проблем](docs/troubleshooting.md)
- [Переход с предыдущей версии](docs/migration.md)
- [Разработка, проверки и подготовка GitHub](docs/development.md)

Используются [официальный Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
и [Telegram Bot API](https://core.telegram.org/bots/api). Подход к MCP взят из того же SDK,
что и у [chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp);
его исходники не копировались, Telethon и пользовательские Telegram-сессии не используются.

## Лицензия

[MIT License](LICENSE). Бесплатное использование, изменение и распространение разрешены при
сохранении copyright-уведомления и текста лицензии. Автор:
[Anton Indyukov](https://github.com/IndyukovAnton).
