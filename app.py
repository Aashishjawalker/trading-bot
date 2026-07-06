#!/usr/bin/env python3
"""
Binance Futures Trading Bot — Hugging Face Spaces entry point.
Serves:
  /              → Landing page (choose UI or Terminal)
  /ui            → Existing dashboard UI
  /terminal      → xterm.js web terminal
  /api/*         → Dashboard API (proxied from dashboard.py)
"""

import hashlib
import hmac
import json
import mimetypes
import os
import select
import signal
import struct
import sys
import threading
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ── Import bot modules ──────────────────────────────────────────────────
from bot.client import BinanceFuturesClient
from bot.portfolio import get_summary, get_order_history

KEY = os.environ.get("BINANCE_TESTNET_API_KEY", "")
SECRET = os.environ.get("BINANCE_TESTNET_API_SECRET", "")
client = BinanceFuturesClient(api_key=KEY, api_secret=SECRET) if KEY and SECRET else None

BASE = Path(__file__).resolve().parent
UI_DIR = BASE / "ui"
PORT = int(os.environ.get("PORT", 7860))

# ── Terminal session (PTY-based) ────────────────────────────────────────

_TERMINAL_SESSION = None
_TERMINAL_LOCK = threading.Lock()


class TerminalSession:
    """Runs cli.py in a PTY so interactive I/O works via the web."""

    def __init__(self):
        self.fd = None
        self.child_pid = None
        self.running = False
        self.output_buffer = b""

    def start(self):
        import subprocess
        import pty
        import fcntl

        master_fd, slave_fd = pty.openpty()

        proc = subprocess.Popen(
            [sys.executable, str(BASE / "cli.py")],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            preexec_fn=os.setsid,
        )
        os.close(slave_fd)

        self.child_pid = proc.pid
        self.fd = master_fd
        self.running = True

        # Non-blocking reads on master
        fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        # Give cli.py a moment to print the initial menu
        import time
        time.sleep(0.8)
        try:
            while True:
                data = os.read(master_fd, 4096)
                if not data:
                    break
                self.output_buffer += data
        except (BlockingIOError, OSError):
            pass

    def read_output(self):
        """Return (output_bytes, still_running_bool)."""
        if not self.running or self.fd is None:
            return b"", False

        out = b""
        try:
            while True:
                data = os.read(self.fd, 4096)
                if not data:
                    # EOF — child closed its end
                    self.running = False
                    break
                out += data
        except BlockingIOError:
            pass
        except OSError:
            self.running = False

        self.output_buffer += out
        return out, self.running

    def write(self, data: bytes):
        if self.fd is not None and self.running:
            os.write(self.fd, data)

    def resize(self, rows: int, cols: int):
        if self.fd is not None:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            try:
                import fcntl, termios
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
            except (ImportError, OSError):
                pass

    def stop(self):
        self.running = False
        if self.child_pid:
            try:
                os.kill(self.child_pid, signal.SIGTERM)
            except OSError:
                pass
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass


def get_or_create_terminal():
    global _TERMINAL_SESSION
    with _TERMINAL_LOCK:
        if _TERMINAL_SESSION is None or not _TERMINAL_SESSION.running:
            _TERMINAL_SESSION = TerminalSession()
            _TERMINAL_SESSION.start()
        else:
            # Drain new output so it's available for the next poll
            _TERMINAL_SESSION.read_output()
        return _TERMINAL_SESSION


def kill_terminal():
    global _TERMINAL_SESSION
    with _TERMINAL_LOCK:
        if _TERMINAL_SESSION:
            _TERMINAL_SESSION.stop()
            _TERMINAL_SESSION = None


# ── Helpers ─────────────────────────────────────────────────────────────

def get_open_orders(client, symbol=None):
    raw = client._signed_request("GET", "/fapi/v1/openOrders", {})
    return [{
        "orderId": o["orderId"], "symbol": o["symbol"], "side": o["side"],
        "type": o["type"], "price": o["price"], "origQty": o["origQty"],
        "executedQty": o["executedQty"], "time": o["time"],
    } for o in raw]


# ── Landing page HTML (embedded) ────────────────────────────────────────

LANDING_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Trading Bot — Launcher</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
    color: #e2e8f0;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
  }
  .container { text-align: center; max-width: 600px; padding: 40px 20px }
  h1 { font-size: 2rem; margin-bottom: 8px; font-weight: 700 }
  .subtitle { color: #94a3b8; margin-bottom: 40px; font-size: 1rem; line-height: 1.5 }
  .cards { display: flex; gap: 20px; flex-wrap: wrap; justify-content: center }
  .card {
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 16px;
    padding: 32px 28px;
    width: 240px;
    cursor: pointer;
    transition: all .2s;
    text-decoration: none;
    color: #e2e8f0;
  }
  .card:hover { border-color: #3b82f6; transform: translateY(-4px); box-shadow: 0 8px 24px rgba(59,130,246,.15) }
  .icon { font-size: 3rem; margin-bottom: 16px }
  .card h2 { font-size: 1.2rem; margin-bottom: 8px }
  .card p { color: #94a3b8; font-size: .85rem; line-height: 1.5 }
  .footer { margin-top: 40px; color: #475569; font-size: .8rem }
  .footer span { color: #3b82f6 }
</style>
</head>
<body>
<div class="container">
  <h1>📈 Binance Futures Bot</h1>
  <p class="subtitle">Choose how you want to interact with the trading bot</p>
  <div class="cards">
    <a class="card" href="/index.html">
      <div class="icon">🖥️</div>
      <h2>Web UI Dashboard</h2>
      <p>Full trading dashboard with positions, orders, and trade form</p>
    </a>
    <a class="card" href="/terminal">
      <div class="icon">⌨️</div>
      <h2>Terminal Access</h2>
      <p>Interactive CLI with full terminal emulation — place orders, check positions, manage your bot</p>
    </a>
  </div>
  <div class="footer">Hosted on <span>Hugging Face Spaces</span></div>
</div>
</body>
</html>
"""


# ── HTTP Handler ────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    # ── Routing ──────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        try:
            if path == "/":
                self._html(200, LANDING_HTML)
            elif path == "/terminal":
                self._serve_terminal()
            elif path == "/api/summary":
                self._json(get_summary(client) if client else {"error": "No credentials"})
            elif path == "/api/orders":
                symbol = qs.get("symbol", [None])[0]
                self._json({"orders": get_order_history(client, symbol) if client else []})
            elif path == "/api/open_orders":
                self._json({"orders": get_open_orders(client) if client else []})
            elif path == "/api/terminal/output":
                self._handle_terminal_output()
            elif path == "/api/terminal/status":
                self._handle_terminal_status()
            else:
                self._serve_static(path)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        try:
            if path == "/api/place_order":
                self._json(self._place(body))
            elif path == "/api/cancel_order":
                self._json(self._cancel(body))
            elif path == "/api/terminal/input":
                self._handle_terminal_input(body)
            elif path == "/api/terminal/resize":
                self._handle_terminal_resize(body)
            elif path == "/api/terminal/restart":
                kill_terminal()
                self._json({"status": "restarted"})
            else:
                self.send_error(404)
        except Exception as e:
            self._json({"error": str(e)}, 400)

    # ── Trading API ──────────────────────────────────────────────────

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

    # ── Terminal handlers ────────────────────────────────────────────

    def _handle_terminal_output(self):
        session = get_or_create_terminal()
        output, running = session.read_output()

        # Decode with replacement for non-UTF8 control chars
        text = output.decode("utf-8", errors="replace")

        self._json({
            "output": text,
            "running": running,
        })

    def _handle_terminal_input(self, body):
        session = get_or_create_terminal()
        data = body.get("data", "")
        if isinstance(data, str):
            data = data.encode("utf-8")
        session.write(data)
        self._json({"status": "ok"})

    def _handle_terminal_resize(self, body):
        session = get_or_create_terminal()
        rows = body.get("rows", 24)
        cols = body.get("cols", 80)
        session.resize(rows, cols)
        self._json({"status": "ok"})

    def _handle_terminal_status(self):
        global _TERMINAL_SESSION
        with _TERMINAL_LOCK:
            running = _TERMINAL_SESSION is not None and _TERMINAL_SESSION.running
        self._json({"running": running})

    # ── Serve terminal page ──────────────────────────────────────────

    def _serve_terminal(self):
        html = self._get_terminal_html()
        self._html(200, html)

    @staticmethod
    def _get_terminal_html():
        return """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Trading Bot — Terminal</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box }
  body { background: #0f172a; color: #e2e8f0; font-family: -apple-system, sans-serif; }
  #terminal-container { height: 100vh; width: 100vw; padding: 8px; }
  #toolbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 8px 16px; background: #1e293b; border-bottom: 1px solid #334155;
  }
  #toolbar h3 { font-size: 14px; font-weight: 600; color: #94a3b8; }
  #toolbar a { color: #3b82f6; text-decoration: none; font-size: 13px; }
  #toolbar button {
    background: #334155; border: none; color: #e2e8f0;
    padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px;
  }
  #toolbar button:hover { background: #475569; }
  #terminal { height: calc(100vh - 45px); }
  .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .status-dot.alive { background: #22c55e; }
  .status-dot.dead { background: #ef4444; }
</style>
</head>
<body>
<div id="toolbar">
  <div>
    <span class="status-dot alive" id="status-dot"></span>
    <h3 style="display:inline">Terminal — Binance Futures CLI</h3>
  </div>
  <div>
    <a href="/">← Back to Launcher</a>
    <button onclick="restartTerminal()" style="margin-left:12px">Restart</button>
  </div>
</div>
<div id="terminal"></div>

<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
<script>
(function() {
  const term = new Terminal({
    cursorBlink: true,
    cursorStyle: 'block',
    fontSize: 14,
    fontFamily: 'Menlo, Monaco, "Courier New", monospace',
    theme: {
      background: '#0f172a',
      foreground: '#e2e8f0',
      cursor: '#e2e8f0',
      selectionBackground: '#334155',
      black: '#0f172a', red: '#ef4444', green: '#22c55e',
      yellow: '#eab308', blue: '#3b82f6', magenta: '#a855f7',
      cyan: '#06b6d4', white: '#e2e8f0',
      brightBlack: '#475569', brightRed: '#f87171', brightGreen: '#4ade80',
      brightYellow: '#facc15', brightBlue: '#60a5fa', brightMagenta: '#c084fc',
      brightCyan: '#22d3ee', brightWhite: '#f8fafc',
    },
  });

  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);

  const el = document.getElementById('terminal');
  term.open(el);
  fitAddon.fit();

  let buffer = '';

  // Resize observer
  const ro = new ResizeObserver(() => fitAddon.fit());
  ro.observe(el);

  // Send resize to server
  function sendResize() {
    const dims = fitAddon.proposeDimensions();
    if (!dims) return;
    fetch('/api/terminal/resize', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({rows: dims.rows, cols: dims.cols}),
    }).catch(function(){});
  }

  window.addEventListener('resize', function() {
    fitAddon.fit();
    sendResize();
  });

  // Input: capture keystrokes and send raw
  term.onData(function(data) {
    buffer += data;
    fetch('/api/terminal/input', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({data: data}),
    }).catch(function(){});
  });

  // Poll output from server
  let lastOutputLen = 0;
  let pollTimer = null;
  let statusDot = document.getElementById('status-dot');

  function poll() {
    fetch('/api/terminal/output')
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.output && data.output.length > 0) {
          term.write(data.output);
        }
        statusDot.className = 'status-dot ' + (data.running ? 'alive' : 'dead');
        pollTimer = setTimeout(poll, 100);
      })
      .catch(function() {
        statusDot.className = 'status-dot dead';
        pollTimer = setTimeout(poll, 2000);
      });
  }

  poll();
  setTimeout(sendResize, 500);

  // Expose restart globally
  window.restartTerminal = function() {
    fetch('/api/terminal/restart', {method: 'POST'})
      .then(function() {
        term.clear();
        term.write('\\r\\n*** Terminal restarted ***\\r\\n\\r\\n');
      });
  };
})();
</script>
</body>
</html>"""

    # ── Static file serving ──────────────────────────────────────────

    def _serve_static(self, path):
        if path == "/":
            path = "/index.html"
        filepath = UI_DIR / path.lstrip("/")
        try:
            filepath = filepath.resolve()
            if not str(filepath).startswith(str(UI_DIR.resolve())):
                self.send_error(403)
                return
        except OSError:
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

    # ── Response helpers ─────────────────────────────────────────────

    def _html(self, status, html):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def log_message(self, fmt, *args):
        pass  # quieter logs for HF Spaces


# ── Main ────────────────────────────────────────────────────────────────

def main():
    if not client:
        print("Warning: BINANCE_TESTNET_API_KEY / SECRET not set.")
        print("The dashboard API will return errors, but UI and terminal will still load.")

    if not client or not client.ping():
        print("Warning: Cannot reach Binance testnet — check credentials / network.")

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Server: http://0.0.0.0:{PORT}")
    print(f"  Landing:  http://localhost:{PORT}/")
    print(f"  Dashboard: http://localhost:{PORT}/dashboard")
    print(f"  Terminal:  http://localhost:{PORT}/terminal")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        kill_terminal()
        server.shutdown()


if __name__ == "__main__":
    main()
