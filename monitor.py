import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from utils.monitoring import ROOT, STATUSES, public_job


STALE_AFTER_SECONDS = 20


def parse_time(value):
    return datetime.fromisoformat(value).astimezone()


def summarize(rows, counts=None, confirmed=None):
    counts = Counter(row["status"] for row in rows) if counts is None else counts
    if confirmed is None:
        confirmed = len({row["job_id"] or f"unknown-{index}" for index, row in enumerate(rows)
                         if row["status"] == "CONFIRMED"})
    return {
        "processed": sum(counts.values()), "confirmed": confirmed,
        "confirmed_records": counts.get("CONFIRMED", 0),
        "unconfirmed": counts.get("SUBMIT_UNCONFIRMED", 0),
        "failed": sum(counts.get(status, 0) for status in ("ERROR", "INCOMPLETE", "NO_MODAL")),
        "skipped": sum(counts.get(status, 0) for status in ("NO_EASY_APPLY", "ALREADY_APPLIED", "CLOSED")),
        "statuses": dict(counts), "recent": list(reversed(rows[-50:])),
    }


def build_snapshot(root=ROOT, now=None):
    root = Path(root)
    now = now or datetime.now().astimezone()
    warnings, rows, state = [], [], {}
    health = "not_started"
    try:
        state = json.loads((root / "bot_status.json").read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            raise ValueError("Unsupported status schema")
        age = max(0, (now - parse_time(state["heartbeat_at"])).total_seconds())
        if state.get("phase") == "stopped":
            health = "stopped"
        elif age > STALE_AFTER_SECONDS:
            health = "stale"
        elif state.get("phase") == "error" or state.get("consecutive_failures", 0):
            health = "error"
        else:
            health = "reporting"
        state["heartbeat_age_seconds"] = round(age, 1)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, KeyError, TypeError):
        health, state = "unavailable", {}
        warnings.append("Live status could not be read. The bot is not assumed to be running.")

    try:
        with (root / "application_log.csv").open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            if not {"timestamp", "status", "title", "company", "job_id"}.issubset(reader.fieldnames or []):
                raise ValueError("Unrecognized history format")
            invalid = 0
            for row in reader:
                try:
                    if row.get("status") not in STATUSES:
                        raise ValueError("Incomplete record")
                    when = parse_time(row["timestamp"])
                    rows.append({**public_job(row), "timestamp": when.isoformat(), "status": row["status"]})
                except (ValueError, TypeError, KeyError):
                    invalid += 1
            if invalid:
                warnings.append(f"{invalid} incomplete or invalid history record(s) excluded; totals may be incomplete.")
    except FileNotFoundError:
        pass
    except (OSError, ValueError, csv.Error):
        warnings.append("Application history could not be fully read; displayed totals may be incomplete.")

    rows.sort(key=lambda row: row["timestamp"])
    today = [row for row in rows if parse_time(row["timestamp"]).date() == now.date()]
    state["health"] = health
    return {
        "generated_at": now.isoformat(), "bot": state, "warnings": warnings,
        "history_latest_at": rows[-1]["timestamp"] if rows else None,
        "scopes": {
            "today": summarize(today), "all": summarize(rows),
            "run": summarize(state.get("recent_results", []), state.get("counts", {}), state.get("confirmed_unique", 0)),
        },
        "events": list(reversed(state.get("events", []))),
    }


def create_server(root=ROOT, port=8765):
    root = Path(root)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send_body(self, status, body, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            origin = self.headers.get("Origin")
            if (self.headers.get("Host", "").lower() not in allowed
                    or (origin and origin not in {"http://" + host for host in allowed})
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"):
                self.send_body(403, b'{"error":"Local access only"}')
                return
            path = urlsplit(self.path).path
            if path == "/api/status":
                body = json.dumps(build_snapshot(root), ensure_ascii=False).encode("utf-8")
                self.send_body(200, body)
            elif path == "/":
                self.send_body(200, Path(__file__).with_name("monitor.html").read_bytes(), "text/html; charset=utf-8")
            else:
                self.send_body(404, b'{"error":"Not found"}')

        def do_POST(self):
            self.send_body(405, b'{"error":"Read-only monitor"}')

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description="Read-only local application monitor; never starts the bot.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with create_server(port=args.port) as server:
        print(f"Application monitor: http://127.0.0.1:{server.server_port}", flush=True)
        print("Read-only. Closing this server does not stop the bot.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
