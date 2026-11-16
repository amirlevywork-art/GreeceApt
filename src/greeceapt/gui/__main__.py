"""``./gui`` — start the local GUI server and open it in its own Chrome window.

    ./gui                 open the window (127.0.0.1, port 8765 or the next free one)
    ./gui --no-browser    only start the server and print its address
"""

from __future__ import annotations

import argparse
import logging
import secrets
import socket
import subprocess
import sys
import threading
import webbrowser

DEFAULT_PORT = 8765


def free_port(start: int = DEFAULT_PORT, tries: int = 20) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as uvicorn binds: a closing port is free
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port in {start}–{start + tries - 1}")


def open_window(url: str) -> None:
    """A Chrome app window (no tabs or address bar); the default browser if Chrome is missing."""
    if sys.platform == "darwin":
        result = subprocess.run(["open", "-na", "Google Chrome", "--args", f"--app={url}",
                                 "--window-size=1500,950"], capture_output=True)
        if result.returncode == 0:
            return
    webbrowser.open(url)


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    from greeceapt.gui.server import create_app

    parser = argparse.ArgumentParser(prog="./gui", description="GreeceApt local GUI")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    port = free_port(args.port)
    token = secrets.token_urlsafe(24)
    server: uvicorn.Server | None = None

    def quit_server() -> None:
        if server is not None:
            server.should_exit = True

    app = create_app(token=token, on_quit=quit_server)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    url = f"http://127.0.0.1:{port}/"
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(f"GreeceApt GUI: {url}  (Ctrl-C to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(1.0, open_window, args=(url,)).start()
    server.run()
    running = [j.action.label for j in app.state.jobs.running()]
    if running:
        print("Still running in the background (their processes go on): " + ", ".join(running))


if __name__ == "__main__":
    main()
