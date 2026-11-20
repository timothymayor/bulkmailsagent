"""Local web UI for the bulk email agent.

    python server.py            # then open http://127.0.0.1:8025

Listens on localhost only. Uses the same .env, sent_log.csv and
unsubscribed.txt as the command-line tool.
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import bulkmail as bm

PORT = int(os.environ.get("BULKMAIL_PORT", "8025"))
STATIC = bm.HERE / "web"
ENV_FILE = bm.HERE / ".env"
SETTING_KEYS = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SENDER_NAME",
                "SENDER_EMAIL", "REPLY_TO", "UNSUBSCRIBE", "SEND_RATE_PER_MIN",
                "ANTHROPIC_API_KEY", "CLAUDE_MODEL"]
SECRET_KEYS = {"SMTP_PASSWORD", "ANTHROPIC_API_KEY"}


# ---------------------------------------------------------------- state

class Job:
    """One send (or preview) run, executed on a background thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.state: dict = {"status": "idle"}

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.state))

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def update(self, **kw) -> None:
        with self.lock:
            self.state.update(kw)

    def add_item(self, item: dict) -> None:
        with self.lock:
            self.state["items"].append(item)
            key = "failed" if item["status"] == "failed" else "done"
            self.state[key] += 1


JOB = Job()
RECIPIENTS: list[dict[str, str]] = []


def run_job(opts: dict) -> None:
    campaign = opts["campaign"]
    live = opts["send"]
    test_to = opts.get("test_to") or ""
    queue = [r for r, s in bm.classify(RECIPIENTS, campaign) if s == "ready"]
    if opts.get("limit"):
        queue = queue[: int(opts["limit"])]
    JOB.update(status="running", campaign=campaign, live=live, test_to=test_to,
               total=len(queue), done=0, failed=0, items=[], error="",
               started=time.time())
    mailer = None
    try:
        personalize = bm.Personalizer(bm.env("CLAUDE_MODEL", "claude-opus-5-5"),
                                      opts.get("effort", "low")) if opts.get("personalize") else None
        sender = bm.env("SENDER_EMAIL") or bm.env("SMTP_USER") or "you@example.com"
        if live:
            mailer = bm.Mailer()
            mailer.connect()  # fail fast on bad credentials
        rate = float(opts.get("rate") or bm.env("SEND_RATE_PER_MIN", "20"))
        interval = 60.0 / rate if rate > 0 else 0
        for i, row in enumerate(queue, 1):
            if JOB.stop.is_set():
                JOB.update(status="stopped")
                return
            started = time.monotonic()
            email = row["email"]
            to = test_to or email
            try:
                body = bm.render(opts["body"], row)
                if personalize:
                    body = personalize(body, row)
                msg = bm.build_message(bm.env("SENDER_NAME"), sender, to,
                                       bm.render(opts["subject"], row), body,
                                       bm.env("REPLY_TO"), bm.env("UNSUBSCRIBE"))
                if mailer:
                    mailer.send(msg)
                    if not test_to:
                        bm.log_result(campaign, email, "sent")
                JOB.add_item({"email": email, "to": to, "status": "sent" if live else "previewed",
                              "subject": msg["Subject"], "body": body})
            except Exception as e:
                if mailer and not test_to:
                    bm.log_result(campaign, email, "failed", str(e)[:200])
                JOB.add_item({"email": email, "to": to, "status": "failed", "detail": str(e)})
            if mailer and i < len(queue):
                remaining = interval - (time.monotonic() - started)
                if JOB.stop.wait(max(0.0, remaining)):
                    JOB.update(status="stopped")
                    return
        JOB.update(status="finished")
    except (Exception, SystemExit) as e:
        JOB.update(status="error", error=str(e) or e.__class__.__name__)
    finally:
        if mailer:
            mailer.close()


# ---------------------------------------------------------------- settings

def read_settings() -> dict:
    values = {k: os.environ.get(k, "") for k in SETTING_KEYS}
    for k in SECRET_KEYS:
        values[k] = "••••••••" if values[k] else ""
    return values


def write_settings(new: dict) -> None:
    current = {k: os.environ.get(k, "") for k in SETTING_KEYS}
    for k in SETTING_KEYS:
        if k not in new:
            continue
        v = str(new[k]).strip()
        if k in SECRET_KEYS and (v == "" or set(v) == {"•"}):
            continue  # blank or masked = keep existing secret
        current[k] = v
    lines = [f"{k}={current[k]}" for k in SETTING_KEYS]
    ENV_FILE.write_text("# Written by the bulk email web UI\n" + "\n".join(lines) + "\n",
                        encoding="utf-8")
    os.environ.update(current)


def status() -> dict:
    smtp_ready = all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"))
    return {
        "smtp_ready": smtp_ready,
        "claude_ready": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
        "sender": bm.env("SENDER_EMAIL") or bm.env("SMTP_USER"),
        "sender_name": bm.env("SENDER_NAME"),
        "unsubscribe": bm.env("UNSUBSCRIBE"),
        "rate": float(bm.env("SEND_RATE_PER_MIN", "20") or 20),
    }


def recipients_summary(campaign: str) -> dict:
    rows = [{"row": r, "status": s} for r, s in bm.classify(RECIPIENTS, campaign)]
    columns = list(RECIPIENTS[0].keys()) if RECIPIENTS else []
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"columns": columns, "rows": rows, "counts": counts}


def history() -> list[dict]:
    if not bm.SENT_LOG.exists():
        return []
    with bm.SENT_LOG.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))[::-1][:500]


# ---------------------------------------------------------------- HTTP

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STATIC), **kw)

    def log_message(self, fmt, *args):  # quieter console
        if "/api/job" not in (args[0] if args else ""):
            super().log_message(fmt, *args)

    def send_json(self, data, code=HTTPStatus.OK):
        payload = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def error(self, message, code=HTTPStatus.BAD_REQUEST):
        self.send_json({"error": message}, code)

    # Only accept API calls from this app's own page, so other websites
    # open in the browser can't trigger sends against localhost.
    def same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin:
            return urlparse(origin).hostname in ("127.0.0.1", "localhost")
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return super().do_GET()
        campaign = parse_qs(urlparse(self.path).query).get("campaign", [""])[0] or "campaign"
        routes = {
            "/api/status": lambda: status(),
            "/api/settings": lambda: read_settings(),
            "/api/recipients": lambda: recipients_summary(campaign),
            "/api/job": lambda: JOB.snapshot(),
            "/api/history": lambda: history(),
            "/api/suppression": lambda: sorted(bm.read_lines(bm.SUPPRESSION_FILE)),
        }
        if path not in routes:
            return self.error("Not found", HTTPStatus.NOT_FOUND)
        self.send_json(routes[path]())

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.same_origin() or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.error("Requests must come from the app itself.", HTTPStatus.FORBIDDEN)
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self.error("Body must be JSON.")
        try:
            handler = getattr(self, "post_" + path.removeprefix("/api/").replace("/", "_"), None)
            if not handler:
                return self.error("Not found", HTTPStatus.NOT_FOUND)
            handler(data)
        except ValueError as e:
            self.error(str(e))
        except Exception as e:
            self.error(f"{e.__class__.__name__}: {e}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def post_recipients(self, data):
        global RECIPIENTS
        text = data.get("csv", "")
        if not isinstance(text, str):
            raise ValueError("'csv' must be the CSV file's text.")
        RECIPIENTS = bm.parse_recipients(text)
        self.send_json(recipients_summary(data.get("campaign") or "campaign"))

    def post_preview(self, data):
        if not RECIPIENTS:
            raise ValueError("Add recipients first.")
        row = RECIPIENTS[max(0, min(int(data.get("index", 0)), len(RECIPIENTS) - 1))]
        body = bm.render(data.get("body", ""), row)
        if data.get("personalize"):
            try:
                body = bm.Personalizer(bm.env("CLAUDE_MODEL", "claude-opus-5-5"),
                                       data.get("effort", "low"))(body, row)
            except SystemExit as e:
                raise ValueError(str(e))
        sender = bm.env("SENDER_EMAIL") or bm.env("SMTP_USER") or "you@example.com"
        msg = bm.build_message(bm.env("SENDER_NAME"), sender, row.get("email", ""),
                               bm.render(data.get("subject", ""), row), body,
                               bm.env("REPLY_TO"), bm.env("UNSUBSCRIBE"))
        self.send_json({"from": msg["From"], "to": msg["To"], "subject": msg["Subject"],
                        "body": msg.get_content(), "row": row})

    def post_send(self, data):
        if JOB.running():
            raise ValueError("A run is already in progress.")
        if not RECIPIENTS:
            raise ValueError("Add recipients first.")
        if not data.get("subject", "").strip() or not data.get("body", "").strip():
            raise ValueError("Write a subject and a message first.")
        if data.get("send") and not status()["smtp_ready"]:
            raise ValueError("Add your email server details in Settings before sending.")
        JOB.stop.clear()
        data["campaign"] = (data.get("campaign") or "campaign").strip()
        JOB.thread = threading.Thread(target=run_job, args=(data,), daemon=True)
        JOB.thread.start()
        time.sleep(0.05)
        self.send_json(JOB.snapshot())

    def post_job_stop(self, data):
        JOB.stop.set()
        self.send_json({"ok": True})

    def post_settings(self, data):
        write_settings(data)
        self.send_json({"settings": read_settings(), "status": status()})

    def post_suppression(self, data):
        emails = sorted({e.strip().lower() for e in data.get("emails", []) if e.strip()})
        bm.SUPPRESSION_FILE.write_text("\n".join(emails) + ("\n" if emails else ""), encoding="utf-8")
        self.send_json(emails)


def main():
    bm.load_dotenv()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Bulk email UI running at http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
