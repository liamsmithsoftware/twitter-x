#!/usr/bin/env python3
"""
اجرای سامانه جمع‌آوری کانال‌های ایکس.

استفاده:
    python run.py                 # اجرا روی 0.0.0.0:8000
    python run.py --port 9000     # تغییر پورت
    python run.py --reload        # حالت توسعه (بارگذاری خودکار)
"""
from __future__ import annotations

import argparse
import http.client
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _try_bind_socket(host: str, port: int) -> socket.socket | None:
    """Bind and keep a listening socket, avoiding a probe/use race."""
    server_socket: socket.socket | None = None
    try:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        server_socket = socket.socket(family, socket.SOCK_STREAM)
        # Do not set SO_REUSEADDR here: on Windows it can allow binding to a
        # port that another process already owns.
        server_socket.bind((host, port))
        server_socket.listen(socket.SOMAXCONN)
        server_socket.setblocking(False)
        return server_socket
    except OSError:
        if server_socket is not None:
            server_socket.close()
        return None


def bind_server_socket(host: str, preferred_port: int, span: int = 20) -> tuple[socket.socket, int]:
    """Bind the preferred port, then a nearby free port, then an OS port.

    The socket stays bound until Uvicorn takes ownership, so another process
    cannot grab the selected port in the meantime.
    """
    server_socket = _try_bind_socket(host, preferred_port)
    if server_socket is not None:
        return server_socket, int(server_socket.getsockname()[1])

    if preferred_port == 0:
        raise OSError("The operating system could not allocate an available port.")

    for candidate in range(max(1, preferred_port + 1), min(preferred_port + span + 1, 65536)):
        server_socket = _try_bind_socket(host, candidate)
        if server_socket is not None:
            return server_socket, int(server_socket.getsockname()[1])

    # If the nearby range is busy, ask the OS to allocate an ephemeral port
    # instead of returning an occupied port and crashing at server startup.
    server_socket = _try_bind_socket(host, 0)
    if server_socket is None:
        raise OSError("No available TCP port could be bound.")
    return server_socket, int(server_socket.getsockname()[1])


def is_our_app(host: str, port: int) -> bool:
    """Check whether the service already on that port is this very program."""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    try:
        with urllib.request.urlopen(f"http://{probe_host}:{port}/healthz", timeout=2) as response:
            body = response.read(200).decode("utf-8", "replace")
        return '"status"' in body and "ok" in body
    except (http.client.HTTPException, urllib.error.URLError, OSError, ValueError):
        # A different protocol (for example a SOCKS service) may answer with
        # non-HTTP bytes such as 0x05. That means the port is occupied, but not
        # by this app, so continue with automatic port selection.
        return False


def open_browser(host: str, port: int) -> None:
    """Open the URL that corresponds to the port the server actually bound."""
    browser_host = "localhost" if host in ("0.0.0.0", "", "::") else host
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    url = f"http://{browser_host}:{port}"

    def _open() -> None:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    timer = threading.Timer(3.5, _open)
    timer.daemon = True
    timer.start()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="سامانه هوشمند جمع‌آوری و آرشیو کانال‌های ایکس",
    )
    parser.add_argument("--host", default=os.getenv("X_COLLECTOR_HOST", "0.0.0.0"), help="آدرس اتصال")
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("X_COLLECTOR_PORT", "8000")), help="پورت سرور"
    )
    parser.add_argument("--reload", action="store_true", help="بارگذاری خودکار هنگام تغییر کد")
    parser.add_argument(
        "--strict-port",
        action="store_true",
        help="اگر پورت اشغال بود، به‌جای انتخاب پورت آزاد، برنامه متوقف شود",
    )
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        print("خطا: کتابخانه uvicorn نصب نشده است.\nابتدا دستور زیر را اجرا کنید:\n  pip install -r requirements.txt")
        raise SystemExit(1)

    # کتابخانه‌های ضروری را قبل از بالا آمدن سرور بررسی می‌کنیم تا پیام خطای
    # واضح فارسی ببینید (نه traceback انگلیسی).
    missing: list[str] = []
    for module_name, pip_name in (
        ("fastapi", "fastapi"),
        ("httpx", "httpx[socks]"),
        ("bs4", "beautifulsoup4"),
        ("apscheduler", "apscheduler"),
        ("openpyxl", "openpyxl"),
        ("jdatetime", "jdatetime"),
    ):
        try:
            __import__(module_name)
        except ImportError:
            missing.append(pip_name)
    if missing:
        print("خطا: این بسته‌ها نصب نشده‌اند: " + ", ".join(missing))
        print("دستور نصب:  pip install -r requirements.txt")
        raise SystemExit(1)

    from app.config import ensure_directories

    ensure_directories()

    requested_port = args.port
    if args.strict_port:
        server_socket = _try_bind_socket(args.host, requested_port)
        if server_socket is None:
            print(f"خطا: پورت {requested_port} اشغال است. یک پورت دیگر با --port انتخاب کنید.")
            raise SystemExit(1)
        args.port = int(server_socket.getsockname()[1])
    else:
        server_socket = _try_bind_socket(args.host, requested_port)
        if server_socket is None:
            if requested_port and is_our_app(args.host, requested_port):
                print(f"برنامه از قبل روی پورت {requested_port} در حال اجراست.")
                print(f"پنل مدیریت: http://localhost:{requested_port}")
                open_browser(args.host, requested_port)
                return

            print(f"پورت {requested_port} اشغال است؛ در حال انتخاب پورت آزاد دیگری ...")
            try:
                server_socket, args.port = bind_server_socket(args.host, requested_port + 1)
            except OSError as exc:
                print(f"خطا: پورت آزاد دیگری پیدا نشد: {exc}")
                raise SystemExit(1) from exc
            print(f"برنامه روی پورت آزاد {args.port} اجرا می‌شود.")
        else:
            args.port = int(server_socket.getsockname()[1])

    print("=" * 68)
    print("  سامانه هوشمند جمع‌آوری و آرشیو کانال‌های ایکس")
    print(f"  آدرس پنل مدیریت:  http://localhost:{args.port}")
    print(f"  آدرس شبکه محلی:   http://{args.host}:{args.port}")
    print("  مرورگر تا چند ثانیه دیگر باز می‌شود.")
    print("  برای خروج کلیدهای Ctrl + C را فشار دهید")
    print("=" * 68)
    open_browser(args.host, args.port)

    config = uvicorn.Config(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(ROOT / "app")] if args.reload else None,
        log_level="info",
        forwarded_allow_ips="*",
        proxy_headers=True,
    )
    server = uvicorn.Server(config)
    try:
        if args.reload:
            # Use Uvicorn's reload supervisor with the already-bound socket so
            # reload mode has the same port-conflict protection as normal mode.
            from uvicorn.supervisors import ChangeReload

            ChangeReload(config, target=server.run, sockets=[server_socket]).run()
        else:
            server.run(sockets=[server_socket])
    finally:
        server_socket.close()


if __name__ == "__main__":
    main()
