"""Windows-friendly launcher: start the server and open the browser with one double-click.

    RelayEmployeeHub.exe                       start (or open the running copy) and open the browser
    RelayEmployeeHub.exe --background          start without opening a browser (Windows logon mode)
    RelayEmployeeHub.exe --create-desktop-shortcut
    RelayEmployeeHub.exe --install-startup     start in background mode when this Windows user signs in
    RelayEmployeeHub.exe --remove-startup
    RelayEmployeeHub.exe --set-data-dir D:\\RelayHubData
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import APP_ID, APP_NAME, __version__
from .config import ServerConfig, looks_like_sync_folder, resolve_data_dir, write_pointer


def _message(text: str, error: bool = False) -> None:
    """Show a message even when running without a console window."""
    if sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)():
        print(text, file=sys.stderr if error else sys.stdout)
        return
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10 if error else 0x40)
    else:
        print(text, file=sys.stderr if error else sys.stdout)


def _health(port: int, scheme: str = "http") -> dict | None:
    try:
        with urllib.request.urlopen(f"{scheme}://127.0.0.1:{port}/api/health", timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data if data.get("app") == APP_ID else None
    except Exception:  # noqa: BLE001 - not running
        return None


def _launch_target() -> tuple[str, str]:
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else exe), "-m relay_hub"


def _shortcut(folder_kind: str, name: str, extra_args: str) -> Path:
    if sys.platform != "win32":
        raise SystemExit("Shortcuts can only be created on Windows.")
    target, args = _launch_target()
    args = (args + " " + extra_args).strip()
    workdir = str(Path(target).parent)
    script = (
        "$ErrorActionPreference='Stop';"
        f"$dir=[Environment]::GetFolderPath('{folder_kind}');"
        f"$path=Join-Path $dir '{name}.lnk';"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($path);"
        f"$s.TargetPath='{target}';$s.Arguments='{args}';$s.WorkingDirectory='{workdir}';"
        f"$s.IconLocation='{target},0';$s.Description='{APP_NAME}';$s.Save();Write-Output $path")
    out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                         capture_output=True, text=True, check=True, creationflags=0x08000000)
    return Path(out.stdout.strip())


def _remove_startup() -> bool:
    if sys.platform != "win32":
        return False
    script = ("$p=Join-Path ([Environment]::GetFolderPath('Startup')) 'Relay Employee Hub (background).lnk';"
              "if(Test-Path $p){Remove-Item $p;Write-Output 'removed'}")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                         creationflags=0x08000000)
    return "removed" in out.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="RelayEmployeeHub", description=f"{APP_NAME} {__version__}")
    parser.add_argument("--data-dir", help="data folder to use for this run")
    parser.add_argument("--port", type=int, help="override the configured port")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--background", action="store_true", help="logon/background mode (implies --no-browser)")
    parser.add_argument("--set-data-dir", help="remember a data folder for future runs, then exit")
    parser.add_argument("--create-desktop-shortcut", action="store_true")
    parser.add_argument("--install-startup", action="store_true",
                        help="start in background mode when this Windows user signs in")
    parser.add_argument("--remove-startup", action="store_true")
    parser.add_argument("--allow-sync-folder", action="store_true",
                        help="allow a data folder inside OneDrive/SharePoint/Dropbox (not recommended)")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    if getattr(sys, "frozen", False) and sys.stdout is None:
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115 - windowed build has no console
        sys.stderr = open(os.devnull, "w")  # noqa: SIM115

    if args.create_desktop_shortcut:
        _message(f"Desktop shortcut created: {_shortcut('Desktop', APP_NAME, '')}")
        return 0
    if args.install_startup:
        path = _shortcut("Startup", f"{APP_NAME} (background)", "--background")
        _message(f"{APP_NAME} will start in the background when you sign in to Windows.\n{path}\n\n"
                 "Scheduled tasks only run while this computer is on and you are signed in.")
        return 0
    if args.remove_startup:
        _message("Startup entry removed." if _remove_startup() else "No startup entry was found.")
        return 0
    if args.set_data_dir:
        target = Path(args.set_data_dir).expanduser().resolve()
        reason = looks_like_sync_folder(target)
        if reason and not args.allow_sync_folder:
            _message(f"Refusing to use {target}: {reason}. Keep the live database outside OneDrive/SharePoint.",
                     error=True)
            return 2
        target.mkdir(parents=True, exist_ok=True)
        write_pointer(target)
        _message(f"Data folder set to {target}. Start the application normally.")
        return 0

    data_dir = resolve_data_dir(args.data_dir)
    reason = looks_like_sync_folder(data_dir)
    if reason and not args.allow_sync_folder:
        _message(f"The data folder {data_dir} is {reason}.\n\nThe live database must not be stored in a synchronised "
                 "folder. Choose another folder with --set-data-dir.", error=True)
        return 2
    data_dir.mkdir(parents=True, exist_ok=True)
    config = ServerConfig.load(data_dir)
    if args.port:
        config.port = args.port
    scheme = "https" if config.tls_certfile else "http"
    url = f"{scheme}://127.0.0.1:{config.port}/"
    open_browser = not (args.no_browser or args.background)

    if _health(config.port, scheme):
        if open_browser:
            webbrowser.open(url)
        return 0

    import uvicorn

    from .app import create_app
    from .context import AppContext, InstanceLockError

    ctx = AppContext(data_dir, config)
    try:
        ctx.startup()
    except InstanceLockError as exc:
        if exc.same_host and exc.port and _health(exc.port, scheme):
            if open_browser:
                webbrowser.open(f"{scheme}://127.0.0.1:{exc.port}/")
            return 0
        _message(str(exc), error=True)
        return 3
    except Exception as exc:  # noqa: BLE001
        _message(f"{APP_NAME} could not start:\n\n{exc}", error=True)
        return 1

    app = create_app(ctx)
    server = uvicorn.Server(uvicorn.Config(
        app, host=config.bind_host(), port=config.port, log_config=None, access_log=False,
        ssl_certfile=config.tls_certfile, ssl_keyfile=config.tls_keyfile, timeout_keep_alive=30))

    def stop():
        server.should_exit = True

    ctx.shutdown_callback = stop

    if open_browser:
        def opener():
            for _ in range(60):
                if server.started:
                    webbrowser.open(url)
                    return
                time.sleep(0.25)
        threading.Thread(target=opener, daemon=True).start()
    try:
        server.run()
    except OSError as exc:
        _message(f"Port {config.port} is not available ({exc}). Another program may be using it.", error=True)
        return 4
    finally:
        ctx.shutdown()
    if not server.started:
        _message(f"The server could not listen on port {config.port}. Another program may be using it; change "
                 "the port in config.json in the data folder.", error=True)
        return 4
    return 0
