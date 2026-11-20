"""Bulk email agent.

Reads recipients from a CSV, fills a template per recipient, optionally has
Claude personalize each message, and sends via SMTP with rate limiting.

Safe by default: without --send it only writes previews to ./outbox/.
For the web interface, run `python server.py` instead.

Usage:
    python bulkmail.py recipients.csv template.txt              # dry run (previews)
    python bulkmail.py recipients.csv template.txt --personalize # dry run + Claude
    python bulkmail.py recipients.csv template.txt --send --limit 3 --test-to me@example.com
    python bulkmail.py recipients.csv template.txt --send        # real send
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os
import re
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

HERE = Path(__file__).parent
SENT_LOG = HERE / "sent_log.csv"
SUPPRESSION_FILE = HERE / "unsubscribed.txt"
OUTBOX = HERE / "outbox"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


# ---------------------------------------------------------------- config

def load_dotenv(path: Path = HERE / ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines). Real env vars take precedence."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        # Remove one matching pair of surrounding quotes only, so a password that
        # merely starts or ends with a quote character survives intact.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"Missing required setting {name} (set it in .env)")
    return value or ""


# Conservative default: many hosts throttle or block accounts that send faster.
DEFAULT_RATE = 20.0


def rate_setting() -> float:
    """SEND_RATE_PER_MIN as a number, falling back to DEFAULT_RATE if unset or invalid."""
    try:
        rate = float(env("SEND_RATE_PER_MIN") or DEFAULT_RATE)
    except ValueError:
        return DEFAULT_RATE
    return rate if rate > 0 else DEFAULT_RATE


# ---------------------------------------------------------------- inputs

def parse_recipients(text: str) -> list[dict[str, str]]:
    """Parse CSV text. Raises ValueError if there is no 'email' column."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or "email" not in [h.strip().lower() for h in reader.fieldnames]:
        raise ValueError("The CSV needs a column named 'email'.")
    return [{k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            for row in reader]


def load_recipients(path: Path) -> list[dict[str, str]]:
    try:
        return parse_recipients(path.read_text(encoding="utf-8-sig"))
    except ValueError as e:
        sys.exit(f"{path}: {e}")


def classify(rows: list[dict[str, str]], campaign: str) -> list[tuple[dict[str, str], str]]:
    """Tag each row: ready, invalid, duplicate, unsubscribed, or already sent."""
    suppressed = read_lines(SUPPRESSION_FILE)
    done = already_sent(campaign)
    seen: set[str] = set()
    out = []
    for row in rows:
        email = row.get("email", "").lower()
        if not EMAIL_RE.match(email):
            status = "invalid"
        elif email in seen:
            status = "duplicate"
        elif email in suppressed:
            status = "unsubscribed"
        elif email in done:
            status = "already sent"
        else:
            seen.add(email)
            status = "ready"
        out.append((row, status))
    return out


def load_template(path: Path) -> tuple[str, str]:
    """Template file: first line 'Subject: ...', blank line, then the body."""
    text = path.read_text(encoding="utf-8")
    first, _, body = text.partition("\n")
    if not first.lower().startswith("subject:"):
        sys.exit(f"{path}: first line must be 'Subject: ...'")
    return first.split(":", 1)[1].strip(), body.lstrip("\n")


def render(template: str, row: dict[str, str]) -> str:
    """Replace {{field}} with the row's value. Unknown fields become ''."""
    return PLACEHOLDER_RE.sub(lambda m: row.get(m.group(1).lower(), ""), template)


def read_lines(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {l.strip().lower() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()}


def already_sent(campaign: str) -> set[str]:
    if not SENT_LOG.exists():
        return set()
    with SENT_LOG.open(newline="", encoding="utf-8") as f:
        return {r["email"].lower() for r in csv.DictReader(f)
                if r.get("campaign") == campaign and r.get("status") == "sent"}


def log_result(campaign: str, email: str, status: str, detail: str = "") -> None:
    new = not SENT_LOG.exists()
    with SENT_LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "campaign", "email", "status", "detail"])
        w.writerow([dt.datetime.now().isoformat(timespec="seconds"), campaign, email, status, detail])


# ---------------------------------------------------------- personalization

PERSONALIZE_SYSTEM = """You personalize outbound emails. You receive a base email and \
facts about one recipient. Rewrite the email so it reads as written for that person: \
adjust the greeting and opening, and reference their details where it is natural.

Rules:
- Keep the sender's intent, offer, facts, links, and call to action exactly; never invent \
claims, prices, dates, or details about the recipient that are not in the data.
- Keep roughly the same length and tone. Plain text, no markdown.
- Output only the email body: no subject line, no commentary."""


class Personalizer:
    def __init__(self, model: str, effort: str):
        try:
            import anthropic
        except ImportError:
            sys.exit("--personalize needs the Anthropic SDK: pip install -r requirements.txt")
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort

    def __call__(self, base_body: str, row: dict[str, str]) -> str:
        facts = "\n".join(f"{k}: {v}" for k, v in sorted(row.items()) if v)
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": self.effort},
            # Stable system prompt is cached across all recipients.
            system=[{"type": "text", "text": PERSONALIZE_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{
                "role": "user",
                "content": f"<base_email>\n{base_body}\n</base_email>\n\n"
                           f"<recipient>\n{facts}\n</recipient>",
            }],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined to personalize this message")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("personalized output was truncated")
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise RuntimeError("empty personalization response")
        return text


# ---------------------------------------------------------------- sending

def build_message(sender_name: str, sender_email: str, to: str, subject: str,
                  body: str, reply_to: str, unsubscribe: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender_email)) if sender_name else sender_email
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender_email.split("@")[-1])
    if reply_to:
        msg["Reply-To"] = reply_to
    if unsubscribe:
        target = unsubscribe if unsubscribe.startswith(("http", "mailto:")) else f"mailto:{unsubscribe}"
        msg["List-Unsubscribe"] = f"<{target}>"
        if target.startswith("http"):
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        if target.startswith("mailto:"):
            address = target.removeprefix("mailto:").split("?")[0]
            how = f'email {address} with the subject "unsubscribe"'
        else:
            how = f"visit {target}"
        body = f"{body.rstrip()}\n\n--\nTo stop receiving these emails, {how}."
    msg.set_content(body)
    return msg


class Mailer:
    def __init__(self):
        self.host = env("SMTP_HOST", required=True)
        self.port = int(env("SMTP_PORT", "587"))
        self.user = env("SMTP_USER", required=True)
        self.password = env("SMTP_PASSWORD", required=True)
        self.use_ssl = self.port == 465
        self.conn: smtplib.SMTP | None = None

    def connect(self) -> None:
        ctx = ssl.create_default_context()
        if self.use_ssl:
            self.conn = smtplib.SMTP_SSL(self.host, self.port, context=ctx, timeout=30)
        else:
            self.conn = smtplib.SMTP(self.host, self.port, timeout=30)
            self.conn.starttls(context=ctx)
        self.conn.login(self.user, self.password)

    def send(self, msg: EmailMessage) -> None:
        for attempt in range(2):
            try:
                if self.conn is None:
                    self.connect()
                self.conn.send_message(msg)
                return
            except (smtplib.SMTPServerDisconnected, ConnectionError, TimeoutError):
                self.conn = None  # reconnect once, then give up
                if attempt:
                    raise

    def close(self) -> None:
        if self.conn:
            try:
                self.conn.quit()
            except smtplib.SMTPException:
                pass


# ---------------------------------------------------------------- main

def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(description="Send personalized bulk email.")
    p.add_argument("recipients", type=Path, help="CSV with an 'email' column plus any fields")
    p.add_argument("template", type=Path, help="Template: 'Subject: ...' line, blank line, body with {{field}}")
    p.add_argument("--send", action="store_true", help="Actually send (default is a dry run)")
    p.add_argument("--personalize", action="store_true", help="Have Claude tailor each email")
    p.add_argument("--campaign", help="Campaign name for resume tracking (default: template filename)")
    p.add_argument("--rate", type=float, default=rate_setting(),
                   help=f"Max emails per minute (default: SEND_RATE_PER_MIN, else {DEFAULT_RATE:g})")
    p.add_argument("--limit", type=int, help="Only process the first N eligible recipients")
    p.add_argument("--test-to", help="Send every message to this address instead (for testing)")
    p.add_argument("--model", default=env("CLAUDE_MODEL", "claude-opus-5-5"))
    p.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    args = p.parse_args()

    campaign = args.campaign or args.template.stem
    subject_tpl, body_tpl = load_template(args.template)
    rows = load_recipients(args.recipients)

    queue, skipped = [], {"invalid": 0, "duplicate": 0, "unsubscribed": 0, "already sent": 0}
    for row, status in classify(rows, campaign):
        if status == "ready":
            queue.append(row)
        else:
            skipped[status] += 1
    if args.limit:
        queue = queue[: args.limit]

    mode = "SEND" if args.send else "DRY RUN"
    print(f"[{mode}] campaign={campaign!r}: {len(queue)} to process, "
          + (", ".join(f"{v} skipped ({k})" for k, v in skipped.items() if v) or "none skipped"))
    if args.test_to:
        print(f"  All messages redirected to {args.test_to}")
    if not queue:
        return

    if args.send and not args.yes:
        if input(f"Send {len(queue)} emails now? [y/N] ").strip().lower() != "y":
            sys.exit("Aborted.")

    sender_email = env("SENDER_EMAIL") or env("SMTP_USER", required=args.send)
    sender_name = env("SENDER_NAME")
    reply_to = env("REPLY_TO")
    unsubscribe = env("UNSUBSCRIBE")
    personalize = Personalizer(args.model, args.effort) if args.personalize else None
    mailer = Mailer() if args.send else None
    if not args.send:
        OUTBOX.mkdir(exist_ok=True)

    interval = 60.0 / args.rate if args.rate > 0 else 0
    sent = failed = 0
    try:
        for i, row in enumerate(queue, 1):
            started = time.monotonic()
            email = row["email"]
            try:
                subject = render(subject_tpl, row)
                body = render(body_tpl, row)
                if personalize:
                    body = personalize(body, row)
                to = args.test_to or email
                msg = build_message(sender_name, sender_email or "you@example.com", to,
                                    subject, body, reply_to, unsubscribe)
                if mailer:
                    mailer.send(msg)
                    # Test sends don't mark real recipients as done.
                    if not args.test_to:
                        log_result(campaign, email, "sent")
                else:
                    safe = re.sub(r"[^\w.@-]", "_", email)
                    (OUTBOX / f"{i:04d}_{safe}.eml").write_bytes(bytes(msg))
                sent += 1
                print(f"  [{i}/{len(queue)}] {'sent' if mailer else 'preview'} -> {to}")
            except KeyboardInterrupt:
                raise
            except Exception as e:  # keep going; one bad row shouldn't kill the run
                failed += 1
                if mailer and not args.test_to:
                    log_result(campaign, email, "failed", str(e)[:200])
                print(f"  [{i}/{len(queue)}] FAILED {email}: {e}")
            if mailer and i < len(queue):
                time.sleep(max(0.0, interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("\nInterrupted - rerun the same command to resume where it stopped.")
    finally:
        if mailer:
            mailer.close()

    print(f"Done: {sent} {'sent' if mailer else 'previewed'}, {failed} failed.")
    if not args.send:
        print(f"Previews in {OUTBOX}. Add --send to deliver.")


if __name__ == "__main__":
    main()
