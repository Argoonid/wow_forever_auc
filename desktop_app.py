import os
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

# Фиксируем рабочую папку рядом с .exe ДО импорта config/database
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
os.chdir(APP_DIR)

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(APP_DIR / "terminal_error.log", "a", encoding="utf-8")

import pystray
import uvicorn
from PIL import Image, ImageDraw
import web  # noqa: E402

HOST = "127.0.0.1"
PORT = 8000
URL = f"http://{HOST}:{PORT}"

_shutdown_lock = threading.Lock()
_shutdown_timer: threading.Timer | None = None
_tray_icon: pystray.Icon | None = None
_uvicorn_server: uvicorn.Server | None = None


def _build_tray_icon() -> Image.Image:
    """Золотая монета на тёмном фоне с лаконичной зелёной стрелкой вверх."""
    size = 128
    img = Image.new("RGBA", (size, size), (18, 20, 26, 255))
    draw = ImageDraw.Draw(img)

    # Основа золотой монеты с тонким бликом по краю
    draw.ellipse((12, 12, 116, 116), fill=(234, 179, 8, 255), outline=(254, 240, 138, 255), width=4)
    draw.ellipse((20, 20, 108, 108), fill=(217, 158, 6, 255), outline=(161, 112, 4, 255), width=2)

    # Зелёная стрелка вверх по центру монеты
    arrow_color = (34, 197, 94, 255)
    outline_color = (20, 83, 45, 255)

    # Ножка стрелки
    draw.rectangle((56, 56, 72, 92), fill=arrow_color, outline=outline_color, width=2)
    # Наконечник стрелки
    head_pts = [(64, 34), (36, 62), (92, 62)]
    draw.polygon(head_pts, fill=arrow_color, outline=outline_color)

    return img.resize((64, 64), Image.Resampling.LANCZOS)


def _trigger_exit() -> None:
    if _uvicorn_server is not None:
        _uvicorn_server.should_exit = True
    if _tray_icon is not None:
        try:
            _tray_icon.stop()
        except Exception:
            pass


def schedule_browser_close_exit(delay_sec: float = 4.0) -> None:
    """Запускает отложенный выход, если вкладка/окно браузера закрыты и не открылись снова."""
    global _shutdown_timer
    with _shutdown_lock:
        if _shutdown_timer is not None:
            _shutdown_timer.cancel()
        _shutdown_timer = threading.Timer(delay_sec, _trigger_exit)
        _shutdown_timer.daemon = True
        _shutdown_timer.start()


def cancel_browser_close_exit() -> None:
    """Отменяет таймер выхода при активной вкладке или перезагрузке."""
    global _shutdown_timer
    with _shutdown_lock:
        if _shutdown_timer is not None:
            _shutdown_timer.cancel()
            _shutdown_timer = None


def _wait_and_open_browser() -> None:
    for _ in range(40):
        try:
            with urllib.request.urlopen(URL, timeout=0.5):
                break
        except Exception:
            time.sleep(0.25)
    webbrowser.open(URL)


def main() -> None:
    global _tray_icon, _uvicorn_server

    web.register_lifecycle_hooks(
        on_ping=cancel_browser_close_exit,
        on_closing=lambda: schedule_browser_close_exit(delay_sec=4.0),
    )

    config = uvicorn.Config(
        app=web.app,
        host=HOST,
        port=PORT,
        log_config=None,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    _uvicorn_server = server

    threading.Thread(target=server.run, daemon=True).start()
    threading.Thread(target=_wait_and_open_browser, daemon=True).start()

    def on_open(icon, item):
        cancel_browser_close_exit()
        webbrowser.open(URL)

    def on_exit(icon, item):
        cancel_browser_close_exit()
        _trigger_exit()

    tray = pystray.Icon(
        name="ForeverTerminal",
        icon=_build_tray_icon(),
        title="WoW: Forever AH Terminal",
        menu=pystray.Menu(
            pystray.MenuItem("Открыть терминал", on_open, default=True),
            pystray.MenuItem("Выход", on_exit),
        ),
    )
    _tray_icon = tray
    tray.run()


if __name__ == "__main__":
    main()