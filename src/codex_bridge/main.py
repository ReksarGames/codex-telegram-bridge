import argparse
import asyncio
import json
import os
import signal
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from datetime import datetime

from . import ipc
from .config import CONFIG, DATA, STATE, load_controller, load_gateway, runtime_dir
from .controller import Controller
from .isolation import gateway_command
from .security import Rejected, lock, private_dir, read_private, safe_text
from .setup import add_project, doctor, wizard
from .shell import configure_shell
from .storage import Store
from .rpc import CodexRPC
from .telegram import Gateway, TelegramError


async def send_local_task(project: str, text: str):
    import tomllib
    uid = tomllib.loads(read_private(CONFIG / "controller.toml").decode())["user_id"]
    number = -int(time.time_ns() // 1000)
    message = {"from": {"id": uid}, "chat": {"id": uid, "type": "private"}, "text": text}
    socket = runtime_dir() / "gateway/gateway.sock"
    await ipc.call(socket, "update", {"update": {"update_id": number-1, "callback_query": {"from": message["from"], "message": message, "data": f"p:{project}"}}})
    await ipc.call(socket, "update", {"update": {"update_id": number, "message": message}})


async def select_saved_thread(info: dict) -> str | None:
    Controller.check_project(info)
    async def ignore_event(event):
        pass
    rpc = CodexRPC(Path(info["endpoint"]), ignore_event)
    try:
        await rpc.connect()
        # shortcut: latest 20 unarchived chats; use migrate UUID for older/archived history.
        response = await rpc.call("thread/list", {"cwd": info["path"], "limit": 20, "sortKey": "updated_at", "sourceKinds": ["cli", "vscode", "appServer"]})
    finally:
        await rpc.close()
    threads = [t for t in response["data"] if t.get("cwd") == info["path"] and not t.get("parentThreadId") and not t.get("ephemeral")]
    if not threads:
        return None
    print("Сохранённые чаты проекта (последние 20):")
    for number, thread in enumerate(threads, 1):
        date = datetime.fromtimestamp(thread["updatedAt"]).astimezone().strftime("%d.%m.%Y %H:%M")
        title = safe_text(thread.get("name") or thread.get("preview") or "Без названия", 100).replace("\n", " ")
        print(f"{number}. {date} — {title}")
    print("0. Новый чат без прежнего контекста; сохранённую историю не удалять")
    choice = input("Завершите прежний Codex и другие writers выбранного чата. Введите номер (Enter — отмена): ").strip()
    if not choice or not choice.isascii() or not choice.isdigit() or int(choice) > len(threads):
        raise Rejected("Подключение отменено; сессии не изменены")
    return threads[int(choice)-1]["id"] if int(choice) else None


async def run(args):
    if args.command == "auto":
        args.project = None
    if args.command in {"attach", "auto"} and args.project is None:
        cwd = Path.cwd().resolve(strict=True)
        projects = load_controller(CONFIG / "controller.toml").projects
        project = next((p for p in projects if cwd == p.path or p.path in cwd.parents), None)
        if project is None:
            if args.command == "auto":
                os.execvp("codex", ["codex"])
            raise Rejected("Эта папка не зарегистрирована в мосте. Используйте codex-tg PROJECT_ID либо добавьте проект локально")
        args.project = project.id
        if args.command == "auto":
            args.command, args.direct = "attach", True
    local_socket = runtime_dir() / "local.sock"
    if args.command == "setup":
        await wizard()
    elif args.command == "shell-setup":
        load_controller(CONFIG / "controller.toml")
        configure_shell(args.enabled)
    elif args.command == "doctor":
        if not doctor():
            raise Rejected("Проверка зависимостей не пройдена")
    elif args.command == "add-project":
        add_project(CONFIG / "controller.toml", args.id, args.name, Path(args.path).expanduser())
        print("Проект добавлен локально. После завершения задач перезапустите codex-telegram-controller для загрузки списка.")
    elif args.command == "controller":
        import tomllib
        config = load_controller(CONFIG / "controller.toml")
        user_id = tomllib.loads(read_private(CONFIG / "controller.toml").decode())["user_id"]
        if type(user_id) is not int or user_id <= 0:
            raise Rejected("Некорректный User ID")
        for path in (config.state, config.outbox, config.runtime, config.runtime / "gateway"):
            private_dir(path)
        fd = lock(config.runtime / "controller.lock")
        try:
            print("Codex Controller: запуск и проверка сохранённых сессий", flush=True)
            controller = Controller(config, user_id)
            for signum in (signal.SIGINT, signal.SIGTERM):
                asyncio.get_running_loop().add_signal_handler(signum, controller.stop.set)
            await controller.run()
        finally:
            os.close(fd)
            print("Codex Controller: остановлен", flush=True)
    elif args.command == "gateway":
        config = load_gateway(CONFIG / "gateway.toml")
        for path in (STATE / "gateway", runtime_dir() / "gateway"):
            private_dir(path)
        fd = lock(STATE / "gateway/gateway.lock")
        command = gateway_command(config)
        try:
            print("Telegram Gateway: запуск в файловой изоляции", flush=True)
            process = await asyncio.create_subprocess_exec(*command)
            for signum in (signal.SIGINT, signal.SIGTERM):
                asyncio.get_running_loop().add_signal_handler(signum, lambda: process.terminate() if process.returncode is None else None)
            code = await process.wait()
            if code:
                raise Rejected("Изолированный Gateway остановился")
        finally:
            os.close(fd)
            print("Telegram Gateway: остановлен", flush=True)
    elif args.command == "gateway-worker":
        config = load_gateway(Path(args.config))
        gateway = Gateway(config, Path(args.socket), Path(args.outbox), Path(args.state))
        for signum in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(signum, gateway.stop.set)
        await gateway.run()
    elif args.command in {"attach", "status", "reconcile", "migrate", "smoke"}:
        attach_terminal = args.command == "attach" and not getattr(args, "direct", False)
        if args.command == "migrate":
            if not args.old_process_stopped:
                raise Rejected("Завершите старый Codex. Затем укажите --old-process-stopped: мост не перехватывает чужой терминал")
            print(json.dumps(await ipc.call(local_socket, "migrate", {"project": args.project, "thread": args.session, "old_process_stopped": True, "unarchive": args.unarchive}), ensure_ascii=False, indent=2))
        elif args.command == "reconcile":
            print(json.dumps(await ipc.call(local_socket, "reconcile", {"project": args.project}), ensure_ascii=False, indent=2))
        else:
            current = args.command == "attach" and getattr(args, "current", False)
            if args.command == "attach" and getattr(args, "force", False) and not current:
                raise Rejected("Для принудительной передачи укажите --current")
            if current:
                try:
                    thread_id = str(uuid.UUID(os.environ.get("CODEX_THREAD_ID", "")))
                except (ValueError, TypeError, AttributeError):
                    raise Rejected("CODEX_THREAD_ID не задан или не является UUID. Запускайте команду из нужного чата Codex либо явно сохраните его ID в этой переменной") from None
                await ipc.call(local_socket, "migrate", {"project": args.project, "thread": thread_id, "force": getattr(args, "force", False), "old_process_stopped": getattr(args, "old_process_stopped", False)}, timeout=120)
            info = await ipc.call(local_socket, "local_info", {"project": args.project, "attach": attach_terminal})
            if current and info["thread"] != thread_id:
                raise Rejected("Привязка не совпадает с CODEX_THREAD_ID; терминал не подключён")
            if args.command == "status":
                print(json.dumps(info, ensure_ascii=False, indent=2))
            elif args.command == "smoke":
                # A harmless real turn through the same controller queue, never codex exec.
                await send_local_task(args.project, "Reply with exactly BRIDGE_OK. Do not run commands or change files.")
                print("Безопасная проверочная задача поставлена в общую очередь. Проверяйте status и локальный CLI.")
            else:
                if not info["thread"]:
                    if info.get("paused"):
                        raise Rejected("Очередь приостановлена; сначала выполните /resume для проекта")
                    saved = await select_saved_thread(info)
                    if saved:
                        await ipc.call(local_socket, "migrate", {"project": args.project, "thread": saved, "old_process_stopped": True})
                        info = await ipc.call(local_socket, "local_info", {"project": args.project, "attach": attach_terminal})
                if not info["thread"]:
                    text = input("Новый проект. Первый запрос Codex (затем откроется штатный TUI): ")
                    if not text.strip() or text.startswith("/"):
                        raise Rejected("Для создания истории нужен первый обычный запрос, без управляющей /команды")
                    await send_local_task(args.project, text)
                    try:
                        async with asyncio.timeout(30):
                            while True:
                                info = await ipc.call(local_socket, "local_info", {"project": args.project})
                                if info["state"] == "unknown" or info.get("paused"):
                                    raise Rejected("Отправка не завершена или очередь приостановлена. Запрос не повторяйте; проверьте status/reconcile и /resume")
                                if info["thread"] and (info["turn"] or info["state"] == "idle"):
                                    break
                                await asyncio.sleep(.2)
                    except TimeoutError:
                        raise Rejected("Первый запрос уже поставлен в очередь. Создание сессии пока не подтверждено; проверьте status, запрос не повторяйте") from None
                    info = await ipc.call(local_socket, "local_info", {"project": args.project, "attach": attach_terminal})
                import shutil
                if current and not (sys.stdin.isatty() and sys.stdout.isatty()):
                    print(f"Сессия подключена: {info['thread']}. На ПК откройте терминал: codex-bridge attach {args.project}")
                    return
                if attach_terminal and shutil.which("tmux"):
                    os.execvp("tmux", ["tmux", "-S", str(runtime_dir()/"tmux.sock"), "attach-session", "-t", "bridge-"+args.project])
                os.execvp("codex", ["codex", "resume", "--remote", f"unix://{info['endpoint']}", "-C", info["path"], info["thread"]])
    elif args.command == "publish":
        data = {"project": args.project, "path": args.path}
        if args.session:
            data["session"] = args.session
        token = (await ipc.call(local_socket, "reference_begin", data))["token"]
        deadline = asyncio.get_running_loop().time() + 86400
        while asyncio.get_running_loop().time() < deadline:
            result = await ipc.call(local_socket, "reference_poll", {"project": args.project, "token": token})
            if result["decision"]:
                print(json.dumps(result, ensure_ascii=False))
                if result["decision"] != "accept":
                    raise SystemExit(3)
                return
            await asyncio.sleep(1)
        raise Rejected("Истёк срок ожидания; зависимый шаг запрещён")
    elif args.command in {"backup", "prune", "resolve-task"}:
        # Offline maintenance requires the same lock as the running controller.
        private_dir(runtime_dir())
        fd = lock(runtime_dir() / "controller.lock")
        try:
            store = Store(STATE / "metadata.db")
            if args.command == "backup":
                target = Path(args.output).expanduser()
                out_fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(out_fd)
                store.backup(target)
                print("Резервная копия метаданных создана; она может содержать ожидающие запросы")
            elif args.command == "prune":
                store.prune()
            else:
                task = store.db.execute("SELECT * FROM tasks WHERE id=? AND state='unknown'", (args.task,)).fetchone()
                if not task or not args.reviewed_locally:
                    raise Rejected("Проверьте реальную историю и укажите --reviewed-locally")
                store.task_state(args.task, args.state)
                store.state(task["project"], "unknown")
                print("Результат зарегистрирован. После запуска выполните reconcile; неизвестные задачи не переотправляются.")
            store.db.close()
        finally:
            os.close(fd)


def parser():
    p = argparse.ArgumentParser(description="Codex Telegram Bridge — локальное управление")
    sub = p.add_subparsers(dest="command", required=True)
    for command in ("setup", "doctor", "controller", "gateway", "prune"):
        sub.add_parser(command)
    sub.add_parser("auto", help="Shell launcher: bridge in registered directories, native Codex elsewhere")
    shell = sub.add_parser("shell-setup", help="Configure the codex command for your login shell")
    choice = shell.add_mutually_exclusive_group()
    choice.add_argument("--enable", dest="enabled", action="store_const", const=True)
    choice.add_argument("--disable", dest="enabled", action="store_const", const=False)
    worker = sub.add_parser("gateway-worker", help=argparse.SUPPRESS)
    for option in ("config", "socket", "outbox", "state"):
        worker.add_argument("--"+option, required=True)
    for command in ("attach", "status", "reconcile", "smoke"):
        command_parser = sub.add_parser(command)
        if command == "attach":
            command_parser.add_argument("--direct", action="store_true", help="Открыть штатный Codex напрямую в текущем терминале, без tmux")
            command_parser.add_argument("project", nargs="?", help="ID проекта; по умолчанию определяется по текущей папке")
        else:
            command_parser.add_argument("project")
        if command == "attach":
            command_parser.add_argument("--current", action="store_true", help="Подключить CODEX_THREAD_ID, проверив сессию через App Server")
            command_parser.add_argument("--force", action="store_true", help="Прервать прежний управляемый turn и закрыть только его терминал")
            command_parser.add_argument("--old-process-stopped", action="store_true", help="Подтверждаю завершение целевого чата во внешнем Codex/App Server")
    project = sub.add_parser("add-project")
    for option in ("id", "name", "path"):
        project.add_argument(option)
    migration = sub.add_parser("migrate")
    migration.add_argument("project")
    migration.add_argument("session")
    migration.add_argument("--old-process-stopped", action="store_true")
    migration.add_argument("--unarchive", action="store_true", help="Вернуть историю после сбоя между archive/unarchive")
    reference = sub.add_parser("publish")
    reference.add_argument("project")
    reference.add_argument("path", help="Путь изображения относительно проекта")
    reference.add_argument("--session")
    sub.add_parser("backup").add_argument("output")
    resolve = sub.add_parser("resolve-task")
    resolve.add_argument("task")
    resolve.add_argument("state", choices=["completed", "failed", "interrupted", "cancelled"])
    resolve.add_argument("--reviewed-locally", action="store_true")
    return p


def launch():
    main(["attach", "--direct", *sys.argv[1:]])


def main(argv: list[str] | None = None):
    os.umask(0o077)
    try:
        asyncio.run(run(parser().parse_args(argv)))
    except (Rejected, TelegramError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError, KeyError, sqlite3.Error, TimeoutError):
        print("Операция не завершена. Проверьте зависимости, конфигурацию и локальный статус; секреты в диагностику не выводятся.", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        pass
