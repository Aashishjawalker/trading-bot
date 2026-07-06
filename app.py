#!/usr/bin/env python3
"""
Binance Futures Trading Bot — Hugging Face Spaces entry point.
Serves landing page, web terminal (running cli.py), and dashboard UI.
"""

import json
import mimetypes
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot.client import BinanceFuturesClient
from bot.portfolio import get_summary, get_order_history

KEY = os.environ.get("BINANCE_TESTNET_API_KEY", "")
SECRET = os.environ.get("BINANCE_TESTNET_API_SECRET", "")
client = BinanceFuturesClient(api_key=KEY, api_secret=SECRET) if KEY and SECRET else None

BASE = Path(__file__).resolve().parent
UI_DIR = BASE / "ui"
PORT = int(os.environ.get("PORT", 7860))
IS_WIN = sys.platform.startswith("win")


# ── Web Terminal (PTY on Linux, pipe-subprocess on Windows) ────────

class TerminalSession:
    def __init__(self):
        self.proc = None
        self.running = False
        self._buf = b""
        self._log = b""  # full output log (never cleared)
        self._read_thread = None
        self._lock = threading.Lock()

    def start(self):
        if IS_WIN:
            self._start_win()
        else:
            self._start_nix()

    def _start_nix(self):
        import fcntl
        import pty
        import termios
        import struct
        pid, fd = pty.fork()
        if pid == 0:
            os.execvp(sys.executable, [sys.executable, "-u", str(BASE / "cli.py")])
            os._exit(1)
        self.child_pid = pid
        self.fd = fd
        self.running = True
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        time.sleep(0.8)
        self._buf = self._read_nix()

    def _start_win(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(BASE / "cli.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        self.running = True
        # Background reader thread — Windows select.select doesn't work with pipes
        def _reader():
            while self.running:
                try:
                    chunk = self.proc.stdout.read(4096)
                    if not chunk:
                        break
                    with self._lock:
                        self._buf += chunk
                except Exception:
                    break
        self._read_thread = threading.Thread(target=_reader, daemon=True)
        self._read_thread.start()
        time.sleep(0.8)

    def _read_nix(self, max_bytes=65536):
        data = b""
        try:
            while len(data) < max_bytes:
                import os as _os
                chunk = _os.read(self.fd, 4096)
                if not chunk:
                    break
                data += chunk
        except (BlockingIOError, OSError):
            pass
        return data

    def _read_win(self, max_bytes=65536):
        with self._lock:
            chunk = self._buf[:max_bytes]
            self._buf = self._buf[max_bytes:]
            return chunk

    def _read(self):
        return self._read_nix() if not IS_WIN else self._read_win()

    def read_output(self):
        out = self._read()
        if self.running:
            if IS_WIN:
                ret = self.proc.poll()
                if ret is not None:
                    with self._lock:
                        if self._buf:
                            out += self._read()
                    self.running = False
            else:
                try:
                    import os as _os
                    wpid, _ = _os.waitpid(self.child_pid, _os.WNOHANG)
                    if wpid:
                        self.running = False
                except OSError:
                    pass
        result = self._buf + out
        self._buf = b""
        self._log += result
        return result.decode("utf-8", errors="replace"), self.running

    def get_history(self):
        return self._log.decode("utf-8", errors="replace")

    def write(self, data: bytes):
        if not self.running:
            return
        if IS_WIN and self.proc and self.proc.stdin:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except OSError:
                pass
        elif not IS_WIN:
            try:
                import os as _os
                _os.write(self.fd, data)
            except OSError:
                pass

    def resize(self, rows: int, cols: int):
        if not IS_WIN and self.running:
            try:
                import fcntl, termios, struct as _struct
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, _struct.pack("HH", rows, cols))
            except (ImportError, OSError):
                pass

    def stop(self):
        self.running = False
        if IS_WIN:
            if self.proc:
                try:
                    self.proc.terminate()
                    self.proc.wait(3)
                except Exception:
                    pass
            if self._read_thread and self._read_thread.is_alive():
                self._read_thread.join(timeout=2)
        else:
            try:
                import os as _os
                _os.kill(self.child_pid, signal.SIGTERM)
                _os.waitpid(self.child_pid, 0)
            except (OSError, ProcessLookupError):
                pass
            if self.fd is not None:
                try:
                    import os as _os
                    _os.close(self.fd)
                except OSError:
                    pass
                self.fd = None


_term_lock = threading.Lock()
_term: TerminalSession | None = None


def _get_term():
    global _term
    with _term_lock:
        if _term is None or not _term.running:
            _term = TerminalSession()
            _term.start()
        return _term


def _kill_term():
    global _term
    with _term_lock:
        if _term:
            _term.stop()
            _term = None


# ── Helpers ────────────────────────────────────────────────────────

def _get_open_orders(symbol=None):
    raw = client._signed_request("GET", "/fapi/v1/openOrders", {})
    return [{
        "orderId": o["orderId"], "symbol": o["symbol"], "side": o["side"],
        "type": o["type"], "price": o["price"], "origQty": o["origQty"],
        "executedQty": o["executedQty"], "time": o["time"],
    } for o in raw]


# ── HTTP Handler ───────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        try:
            if path == "/":
                self._serve_static("/landing.html")
            elif path == "/terminal":
                self._serve_static("/terminal.html")
            elif path == "/api/summary":
                self._json(get_summary(client) if client else {"error": "No credentials"})
            elif path == "/api/orders":
                symbol = qs.get("symbol", [None])[0]
                self._json({"orders": get_order_history(client, symbol) if client else []})
            elif path == "/api/open_orders":
                self._json({"orders": _get_open_orders() if client else []})
            elif path == "/api/terminal/output":
                session = _get_term()
                text, running = session.read_output()
                self._json({"output": text, "running": running})
            elif path == "/api/terminal/history":
                session = _get_term()
                self._json({"output": session.get_history(), "running": session.running})
            elif path == "/api/terminal/status":
                with _term_lock:
                    running = _term is not None and _term.running
                self._json({"running": running})
            else:
                self._serve_static(path)

        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        try:
            if self.path == "/api/place_order":
                self._json(self._place(body))
            elif self.path == "/api/cancel_order":
                self._json(self._cancel(body))
            elif self.path == "/api/terminal/input":
                session = _get_term()
                data = body.get("data", "")
                session.write(data.encode("utf-8") if isinstance(data, str) else data)
                self._json({"status": "ok"})
            elif self.path == "/api/terminal/resize":
                session = _get_term()
                session.resize(body.get("rows", 24), body.get("cols", 80))
                self._json({"status": "ok"})
            elif self.path == "/api/terminal/restart":
                _kill_term()
                self._json({"status": "restarted"})
            else:
                self.send_error(404)
        except Exception as e:
            self._json({"error": str(e)}, 400)

    def _place(self, body):
        params = {
            "symbol": body["symbol"], "side": body["side"],
            "type": body["type"], "quantity": float(body["quantity"]),
        }
        if body.get("price"):
            params["price"] = float(body["price"])
        if body.get("stopPrice"):
            params["stopPrice"] = float(body["stopPrice"])
        if body["type"] == "LIMIT":
            params["timeInForce"] = body.get("timeInForce", "GTC")
        r = client.place_order(**params)
        return {"orderId": r["orderId"], "status": r["status"], "symbol": r["symbol"],
                "message": f"Order {r['orderId']} {r['status']}"}

    def _cancel(self, body):
        r = client.cancel_order(symbol=body["symbol"], order_id=int(body["orderId"]))
        return {"message": f"Cancelled {r['orderId']}", "result": r}

    def _serve_static(self, path):
        if path in ("/", ""):
            path = "/landing.html"
        # Strip /ui prefix — files are already in UI_DIR
        if path.startswith("/ui"):
            path = path[3:] or "/"
            if path == "/":
                path = "/index.html"
        filepath = (UI_DIR / path.lstrip("/")).resolve()
        if not str(filepath).startswith(str(UI_DIR.resolve())):
            self.send_error(403)
            return
        if not filepath.is_file():
            self.send_error(404)
            return
        mime, _ = mimetypes.guess_type(str(filepath))
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.end_headers()
        with open(filepath, "rb") as f:
            self.wfile.write(f.read())

    def _json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def log_message(self, fmt, *args):
        pass  # silence


# ── Entry ──────────────────────────────────────────────────────────

def main():
    if not client:
        print("⚠ No Binance API credentials — dashboard will show errors")
    else:
        if not client.ping():
            print("⚠ Cannot reach Binance testnet API")
    print(f"→ http://localhost:{PORT}")
    HTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
