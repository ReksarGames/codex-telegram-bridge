# Feasibility: проверяемая установленная версия

Исследована CachyOS/Arch, Hyprland/Wayland, Codex CLI **0.160.1**, уже авторизованный через ChatGPT. Из CLI получены --help, login status и `app-server generate-json-schema --experimental`. Авторизационные файлы и токены не читались и не менялись. Python приложение использует asyncio, aiohttp, SQLite и Pillow.

## Выбор транспорта

| Вопрос | A: tmux / PTY вокруг самостоятельного CLI | B: официальный App Server + native remote TUI |
|---|---|---|
| Та же живая сессия | Да, если процесс изначально запущен под управлением | Да: TUI и bridge подключаются к одному серверу/thread |
| Несколько подключений | Несколько tmux clients к одному pane | Несколько JSON-RPC clients, remote `codex resume` |
| Approvals и request ID | Терминальный текст не является надёжным API | Структурированные server requests и JSON-RPC response |
| Turn start/completion | JSONL/hook могут помочь, но не дают полного duplex-протокола | Структурированные notifications |
| Безопасный ввод | tmux buffers/PTY без shell interpolation, но зависит от текущего меню | Typed text input и native queue без steering текущего turn |
| Локальная история | В исходном pane | В штатном Codex TUI того же App Server |
| Привязка к уже произвольному CLI | Нет безопасного захвата | Сначала закончить старый процесс, resume сохранённого UUID |

Выбран **B**. tmux используется для удобного локального launcher, не для определения approvals или передачи произвольного Telegram-текста. Нет двух самостоятельных Codex writers в одном rollout. Один сервер может держать root threads разных зарегистрированных каталогов; каждому проекту соответствует один актуальный root thread.

## Официальные источники

- [App Server](https://learn.chatgpt.com/docs/app-server): JSON-RPC, private Unix WebSocket, remote TUI, thread/turn lifecycle, approvals, вопросы, dynamic tools, queue.
- [Штатные команды CLI](https://learn.chatgpt.com/docs/developer-commands?surface=cli): `/clear`, `/new`, `/status` и различие с очисткой терминала.
- [Hooks](https://learn.chatgpt.com/docs/hooks): lifecycle callbacks. Они не заменяют атомарный workflow gate или duplex approvals.
- [Source v0.160.1: slash command](https://github.com/openai/codex/blob/rust-v0.160.1/codex-rs/tui/src/slash_command.rs).
- [Source v0.160.1: new-session config](https://github.com/openai/codex/blob/rust-v0.160.1/codex-rs/tui/src/app/new_session.rs): remote session defaults могут зависеть от server cwd; `-C` задаёт override.
- [Официальный whisper.cpp](https://github.com/ggml-org/whisper.cpp) и [multilingual models](https://huggingface.co/ggerganov/whisper.cpp).

Интернет-документация может описывать более новую версию. Реализация ориентируется на schema и реальные эксперименты установленной **0.160.1**, а не обещание совместимости с будущими версиями. Experimental capability включена явно, runtime version guard предотвращает незаметное использование изменившегося протокола.

## Проверенные особенности

1. `thread/start` возвращает ID до сохранения rollout; saved session появляется после первого turn. Поэтому первый prompt нужен до resume нового чата.
2. `thread/resume` для уже loaded thread присоединяет клиент к живому объекту сервера. Два клиента видят завершение одного turn; штатный remote TUI подключается к тому же ID.
3. `thread/queue/add` сам запускает native FIFO pump при idle. Дополнительный queue/start создавал гонку и **удалён**. Bridge связывает task/turn по `userMessage.clientId`, а не по первому случайному `turn/started` — он мог принадлежать локальному пользователю.
4. Завершение определяется только `turn/completed` или проверенным turn status из истории. Тишина терминала не считается завершением.
5. `thread/unsubscribe` сохраняет thread и writer в памяти до **30 минут** без активности; loaded/list не является списком клиентов. Эксперимент обнаружил ошибку первоначальной проверки. `/new` теперь закрывает managed TUI только без подключённого человека, проверяет отсутствие inProgress turn, вызывает `thread/archive`, проверяет отсутствие старого ID в loaded/list и возвращает историю на диск через `thread/unarchive` без resume. Только после этого создаётся новый thread. Сбой блокирует очередь; история не удаляется. Неуправляемые клиенты тоже теряют старый runtime.
6. Approvals: commandExecution/fileChange/permissions server requests. Decisions ограничены одним разрешением; permissions scope turn. Secret и составные questions требуют ответа локально.
7. Dynamic tool `bridge_publish_reference` оставляет item/tool/call неотвеченным. Живой эксперимент подтвердил, что очередь не начинает следующую задачу до решения. Импортированный thread может не иметь этого tool — для него helper или новый чат.
8. JSONL session events не используются для управления: это saved history, не надёжный канал ответов. Hooks не используются для автоматических approvals или имитации паузы.

## Async questions в 0.160.1

Установленная JSON schema и [официальный handler request_user_input_async](https://github.com/openai/codex/blob/rust-v0.160.1/codex-rs/core/src/tools/handlers/request_user_input_async.rs) подтверждают `agentMessage.delivery=async`, `questions=[{title,options?:[string]}]`, ID item равен call ID. Это не JSON-RPC server request и не blocking gate. Мост показывает каждый вопрос отдельно независимо от mirror_results; ответы отправляет как обычный UserInput через `turn/steer` с обязательным expectedTurnId. Формат envelope и questionItemId соответствует [native TUI](https://github.com/openai/codex/blob/rust-v0.160.1/codex-rs/tui/src/async_question_reply.rs) и [его тестам](https://github.com/openai/codex/blob/rust-v0.160.1/codex-rs/tui/src/chatwidget/tests/questions_tests.rs). Локальный именованный ответ закрывает только свой вопрос; обычный follow-up закрывает все предыдущие вопросы. Completion/interrupt делает старые кнопки недействительными. Мост никогда не превращает такой ответ в approval или независимую новую задачу.

## Почему `/clear` отключена

Эксперимент создавал безвредную историю, подключал штатный TUI к тому же серверу в свежем managed tmux, вводил **буквальную `/clear`**, затем ожидал структурированно проверяемый новый root thread. Ни shell `clear`, ни ANSI очистка, ни `/new` не подставлялись.

Найдены проблемы: paste-burst ввод требует паузы перед Enter; без explicit `-C` новый remote chat мог получить cwd сервера вместо проекта; с explicit cwd эксперимент также не дал стабильного подтверждения нового ID в установленной версии. Неопределённый результат блокировал очередь. Парсить похожий терминальный текст и объявлять успех было бы небезопасно.

Нестабильная реализация удалена. Telegram `/clear` теперь честно сообщает об ограничении, не меняет сессию и не посылает клавиши. `/new` создаёт новый разговор через документированный thread/start после подтверждения и сохранения старого UUID. Настоящая `/status` остаётся в локальном CLI; мост не выдаёт свою сводку за её вывод.

Для полной приёмки нужен проверенный официальный UI-command endpoint или стабильная версия remote TUI с подтверждаемым новым thread/cwd, после чего отдельный live тест `/clear` и строгая проверка сохранения старой истории. Не обновляйте Codex автоматически в надежде обойти version guard.
