#!/usr/bin/env python3
"""Serve a local dashboard and run an allowlisted set of trader tools."""

import argparse
import json
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = Path(__file__).resolve().parent
TRADER_DIR = ROOT / "trader"
MAX_BODY = 8192
MAX_STATE_BYTES = 512 * 1024
MAX_OUTPUT_CHARS = 12000
MAX_JOBS = 100
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
STOP_RE = re.compile(r"^(or-mid|or-low|(price|pct|atr):[0-9]+(\.[0-9]+)?)$")
TARGET_RE = re.compile(r"^([0-9]+(\.[0-9]+)?R|price:[0-9]+(\.[0-9]+)?)$")
PRACTICE_RE = re.compile(r"^20[0-9]{2}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01]) ([01][0-9]|2[0-3]):[0-5][0-9]$")
IPV4_RE = re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$")
# Tasks whose tools take --now: a practice time replays them as of that New York time.
PRACTICE_TASKS = ("scanner-orb", "entry", "exit-plan", "timemachine", "budget", "risk", "tickets", "alerts-check")
TOKEN_COOKIE = "dt_token"
MAX_SYMBOLS = 100
JOBS = {}
JOBS_LOCK = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=2)

STATE_FILES = (
    "config.json",
    "watchlist.json",
    "entry.json",
    "plan.json",
    "timemachine.json",
    "orders.json",
    "portfolio.json",
    "prices.json",
    "metrics.json",
    "universe.txt",
    "alerts.json",
    "state/news.json",
    "state/risk.json",
    "state/broker.json",
    "state/alerts.json",
    "state/pelosi.json",
)


class DashboardError(ValueError):
    """Raised when a dashboard task request is not supported."""


def _ticker(value):
    if not isinstance(value, str):
        raise DashboardError("Enter a ticker symbol.")
    symbol = value.strip().upper()
    if not TICKER_RE.fullmatch(symbol):
        raise DashboardError("Ticker must contain 1 to 10 letters, numbers, dots, or hyphens.")
    return symbol


def _symbols(value):
    """Optional comma-separated tickers for a scan; empty means the whole universe."""
    if value in (None, ""):
        return []
    if not isinstance(value, str):
        raise DashboardError("Symbols must be a comma-separated list.")
    symbols = [_ticker(part) for part in value.replace(" ", ",").split(",") if part.strip()]
    if len(symbols) > MAX_SYMBOLS:
        raise DashboardError("Scan at most %d symbols at once, or leave the list empty for the universe." % MAX_SYMBOLS)
    return list(dict.fromkeys(symbols))


def _option(payload, key, pattern, message):
    value = payload.get(key)
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value.strip()):
        raise DashboardError(message)
    return value.strip()


def _seed(value):
    if value in (None, ""):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10 ** 9:
        raise DashboardError("Seed must be a whole number from 0 to 1,000,000,000.")
    return value


def build_command(payload):
    """Translate a small task vocabulary into fixed local Python commands."""
    if not isinstance(payload, dict):
        raise DashboardError("Task request must be a JSON object.")

    task = payload.get("task")
    if not isinstance(task, str):
        raise DashboardError("Choose a supported dashboard task.")
    commands = {
        "universe": ["trader-scanner/scan.py", "universe"],
        "budget": ["trader-budget/size.py", "--write"],
        "risk": ["trader-risk/check.py"],
        "tickets": ["trader-broker/ticket.py", "show"],
        "alerts-check": ["trader-alerts/alerts.py", "check"],
        "alerts-clock": ["trader-alerts/alerts.py", "clock"],
        "alerts-list": ["trader-alerts/alerts.py", "list"],
        "alerts-plan": ["trader-alerts/alerts.py", "add", "--from-plan"],
        "pelosi": ["trader-pelosi/ptr.py", "index", "--member", "pelosi"],
        "pelosi-new": ["trader-pelosi/ptr.py", "new", "--year", str(datetime.now().year)],
    }

    if task in ("news", "entry", "analyst", "quote"):
        symbol = _ticker(payload.get("symbol"))
        if task == "news":
            args = ["trader-news/news.py", "--tickers", symbol]
        elif task == "entry":
            args = ["trader-entry/entry.py", "check", symbol, "--write"]
        elif task == "quote":
            args = ["trader-alerts/alerts.py", "quote", symbol, "--write"]
        else:
            args = ["trader-analyst/facts.py", symbol]
    elif task in ("scanner-daily", "scanner-orb"):
        args = ["trader-scanner/scan.py", "daily" if task == "scanner-daily" else "orb"]
        symbols = _symbols(payload.get("symbols"))
        if symbols:
            args += ["--symbols", ",".join(symbols)]
    elif task == "exit-plan":
        args = ["trader-exit/exit.py", "plan", "--write"]
        stop = _option(payload, "stop", STOP_RE, "Stop is or-mid, or-low, price:X, pct:X or atr:K.")
        target = _option(payload, "target", TARGET_RE, "Target is an R multiple like 2R, or price:X.")
        if stop:
            args += ["--stop", stop]
        if target:
            args += ["--target", target]
    elif task == "timemachine":
        args = ["trader-timemachine/timemachine.py", "run", "--write"]
        seed = _seed(payload.get("seed"))
        if seed is not None:
            args += ["--seed", str(seed)]
    elif task in commands:
        args = commands[task]
    else:
        raise DashboardError("That dashboard task is not available.")

    practice = _option(payload, "practice", PRACTICE_RE, "Practice time is a New York date and time, like 2026-11-09 10:15.")
    if practice and task in PRACTICE_TASKS:
        args += ["--now", practice]

    script, *arguments = args
    return [sys.executable, str(ROOT / script), *arguments]


def read_state():
    files = {}
    for relative in STATE_FILES:
        path = TRADER_DIR / relative
        item = {"exists": path.is_file()}
        if path.is_file():
            try:
                stat = path.stat()
                item["modified"] = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")
                if stat.st_size > MAX_STATE_BYTES:
                    item["error"] = "File exceeds the dashboard read limit."
                else:
                    text = path.read_text(encoding="utf-8")
                    if path.suffix == ".json":
                        item["data"] = json.loads(text)
                    else:
                        item["data"] = {"lines": sum(1 for line in text.splitlines() if line.strip())}
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                item["error"] = str(exc)
        files[relative] = item
    return {"files": files, "paper": _paper_mode(files)}


def _paper_mode(files):
    config = files.get("config.json", {}).get("data")
    if not isinstance(config, dict):
        return "unknown"
    return "paper" if config.get("paper") is True else "live" if config.get("paper") is False else "unknown"


def _run_job(job_id, command):
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
        JOBS[job_id]["started"] = time.time()
    try:
        result = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            shell=False,
        )
        output = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
        status = "completed" if result.returncode == 0 else "finished"
        with JOBS_LOCK:
            JOBS[job_id].update(
                status=status,
                exit_code=result.returncode,
                output=output[-MAX_OUTPUT_CHARS:],
                finished=time.time(),
            )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        with JOBS_LOCK:
            JOBS[job_id].update(
                status="failed",
                error="Task exceeded the 10 minute limit.",
                output=output[-MAX_OUTPUT_CHARS:],
                finished=time.time(),
            )
    except OSError as exc:
        with JOBS_LOCK:
            JOBS[job_id].update(
                status="failed",
                error="Could not start the tool: %s" % exc,
                output="",
                finished=time.time(),
            )


class Handler(BaseHTTPRequestHandler):
    server_version = "DayTraderDashboard/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        super().end_headers()

    def do_GET(self):
        if not self._local_host():
            self._json(421, {"error": "Open the dashboard at its 127.0.0.1 address."})
            return
        url = urlparse(self.path)
        path = url.path
        token = self._token()
        if token and not self._has_token(token):
            given = parse_qs(url.query).get("token", [""])[0]
            if path == "/" and secrets.compare_digest(given, token):
                # Trade the link's key for a cookie, then drop it from the address bar.
                self.send_response(303)
                self.send_header("Set-Cookie", "%s=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"
                                 % (TOKEN_COOKIE, token))
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._json(401, {"error": "Open the dashboard with the link the server printed (it carries the key)."})
            return
        if path == "/api/status":
            self._json(200, read_state())
        elif path == "/":
            self._static("index.html", "text/html; charset=utf-8")
        elif path == "/app.js":
            self._static("app.js", "text/javascript; charset=utf-8")
        elif path == "/styles.css":
            self._static("styles.css", "text/css; charset=utf-8")
        elif path.startswith("/api/jobs/"):
            self._job(path[len("/api/jobs/"):])
        else:
            self._json(404, {"error": "Not found."})

    def do_POST(self):
        if urlparse(self.path).path != "/api/run":
            self._json(404, {"error": "Not found."})
            return
        token = self._token()
        if not self._local_host() or not self._same_origin() or (token and not self._has_token(token)):
            self._json(403, {"error": "Dashboard tasks must come from this local dashboard."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 1 or length > MAX_BODY:
            self._json(413, {"error": "Task request is empty or too large."})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            command = build_command(payload)
        except (UnicodeError, json.JSONDecodeError, DashboardError) as exc:
            self._json(400, {"error": str(exc)})
            return

        job_id = secrets.token_urlsafe(9)
        with JOBS_LOCK:
            job_key = tuple(command[1:])
            if any(
                job.get("key") == job_key and job.get("status") in ("queued", "running")
                for job in JOBS.values()
            ):
                self._json(409, {"error": "That task is already running for this input."})
                return
            if len(JOBS) >= MAX_JOBS:
                finished = sorted(
                    (job for job in JOBS.values() if job.get("status") not in ("queued", "running")),
                    key=lambda job: job.get("created", 0),
                )
                for job in finished[:len(JOBS) - MAX_JOBS + 1]:
                    JOBS.pop(job["id"], None)
            if len(JOBS) >= MAX_JOBS:
                self._json(503, {"error": "Task history is full. Restart the dashboard to clear completed tasks."})
                return
            JOBS[job_id] = {
                "id": job_id,
                "task": payload["task"],
                "key": job_key,
                "status": "queued",
                "created": time.time(),
                "output": "",
            }
        POOL.submit(_run_job, job_id, command)
        self._json(202, {"id": job_id, "status": "queued"})

    def _lan(self):
        return bool(getattr(self.server, "token", None))

    def _loopback_client(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _token(self):
        """The key a request must carry: only on a --lan server, and only from another device."""
        return None if self._loopback_client() else getattr(self.server, "token", None)

    def _has_token(self, token):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        morsel = cookie.get(TOKEN_COOKIE)
        return bool(morsel) and secrets.compare_digest(morsel.value, token)

    def _local_host(self):
        """The Host header must name this server by loopback (or, with --lan, by an IP
        address). A page on another site that re-points its own domain here (DNS
        rebinding) still sends its own domain name as the Host."""
        port = self.server.server_address[1]
        host = self.headers.get("Host", "")
        if host in ("127.0.0.1:%d" % port, "localhost:%d" % port):
            return True
        name, _, host_port = host.rpartition(":")
        return self._lan() and host_port == str(port) and bool(IPV4_RE.fullmatch(name))

    def _same_origin(self):
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if not origin or not host:
            return False
        try:
            parsed = urlparse(origin)
            if parsed.scheme != "http" or parsed.netloc != host:
                return False
            if parsed.hostname in ("127.0.0.1", "localhost"):
                return self._loopback_client()
            return self._lan() and bool(IPV4_RE.fullmatch(parsed.hostname or ""))
        except ValueError:
            return False

    def _static(self, filename, content_type):
        try:
            content = (DASHBOARD / filename).read_bytes()
        except OSError:
            self._json(500, {"error": "Dashboard asset is unavailable."})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _job(self, job_id):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job:
                job = dict(job)
        if job is None:
            self._json(404, {"error": "Task result was not found. Restarted dashboards do not retain job history."})
        else:
            self._json(200, job)

    def _json(self, code, payload):
        content = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def _lan_ip():
    """This computer's address on the home network. A UDP connect picks the outgoing
    interface without sending anything."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        return probe.getsockname()[0]
    except OSError:
        return "<this-computer's-IP>"
    finally:
        probe.close()


def main():
    parser = argparse.ArgumentParser(description="Run the local day-trading dashboard.")
    parser.add_argument("--port", type=int, default=8765, help="Local port to use (default: 8765)")
    parser.add_argument("--lan", action="store_true",
                        help="also serve your home network (phone, tablet), behind a secret key in the link")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    server = ThreadingHTTPServer(("0.0.0.0" if args.lan else "127.0.0.1", args.port), Handler)
    server.token = secrets.token_urlsafe(18) if args.lan else None
    print("Dashboard available at http://127.0.0.1:%d" % args.port)
    if args.lan:
        print("On your phone (same Wi-Fi), open:  http://%s:%d/?token=%s" % (_lan_ip(), args.port, server.token))
        print("Anyone with that link on your network can run the analysis tools. A new key is made each start.")
    else:
        print("Local only. Broker orders are not available in this dashboard.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()
        POOL.shutdown(wait=False)


if __name__ == "__main__":
    main()
