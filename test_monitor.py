import csv
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

from playwright.sync_api import expect, sync_playwright

from monitor import build_snapshot, create_server
from utils.monitoring import RunStatus


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reporter = RunStatus(self.root / "bot_status.json", heartbeat_interval=0.03)
        self.addCleanup(self.reporter.stop)

    def history(self, rows):
        with (self.root / "application_log.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["timestamp", "status", "title", "company", "job_id", "reason", "link"])
            writer.writeheader()
            writer.writerows(rows)

    def test_missing_state_is_not_reported_as_running(self):
        data = build_snapshot(self.root)
        self.assertEqual(data["bot"]["health"], "not_started")
        self.assertEqual(data["scopes"]["today"]["processed"], 0)

    def test_search_failure_is_visible_with_zero_applications(self):
        self.reporter.start()
        self.reporter.update(phase="searching", activity="Search readiness", cycle=1)
        self.reporter.error(TimeoutError("private credential value"), "search", cycle_failed=True)
        self.reporter.update(phase="waiting", activity="Waiting after failed cycle")
        data = build_snapshot(self.root)
        self.assertEqual(data["bot"]["health"], "error")
        self.assertEqual(data["bot"]["consecutive_failures"], 1)
        self.assertEqual(data["scopes"]["run"]["processed"], 0)
        self.assertEqual(data["bot"]["last_error"]["stage"], "search")
        self.assertNotIn("private credential", json.dumps(data))
        self.assertTrue(data["events"])

    def test_run_counts_distinguish_confirmed_unconfirmed_and_duplicates(self):
        self.reporter.start()
        job = {"job_id": "123", "title": "Engineer", "company": "Test", "link": "javascript:alert(1)"}
        self.reporter.record_application(job, "CONFIRMED")
        self.reporter.record_application(job, "CONFIRMED")
        self.reporter.record_application({**job, "job_id": "124"}, "SUBMIT_UNCONFIRMED")
        data = build_snapshot(self.root)
        self.assertEqual(data["scopes"]["run"]["processed"], 3)
        self.assertEqual(data["scopes"]["run"]["confirmed"], 1)
        self.assertEqual(data["scopes"]["run"]["unconfirmed"], 1)
        self.assertNotIn("javascript:", json.dumps(data))

    def test_history_scopes_and_safe_links(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self.history([
            {"timestamp": f"{today} 10:00:00", "status": "CONFIRMED", "title": "A", "company": "Test", "job_id": "123", "reason": "secret", "link": "javascript:alert(1)"},
            {"timestamp": f"{today} 10:01:00", "status": "CONFIRMED", "title": "A", "company": "Test", "job_id": "123"},
            {"timestamp": f"{today} 10:02:00", "status": "SUBMIT_UNCONFIRMED", "title": "B", "company": "Test", "job_id": "124"},
            {"timestamp": "2020-01-01 10:00:00", "status": "ERROR", "job_id": "125"},
        ])
        data = build_snapshot(self.root)
        self.assertEqual(data["scopes"]["today"]["processed"], 3)
        self.assertEqual(data["scopes"]["today"]["confirmed"], 1)
        self.assertEqual(data["scopes"]["all"]["processed"], 4)
        self.assertNotIn("javascript:", json.dumps(data))
        self.assertNotIn("secret", json.dumps(data))

    def test_stale_heartbeat_and_stopped_state_are_distinct(self):
        self.reporter.start()
        future = datetime.now().astimezone() + timedelta(minutes=2)
        self.assertEqual(build_snapshot(self.root, now=future)["bot"]["health"], "stale")
        self.reporter.stop()
        self.assertEqual(build_snapshot(self.root)["bot"]["health"], "stopped")

    def test_heartbeat_updates_without_new_application(self):
        self.reporter.start()
        initial = json.loads(self.reporter.path.read_text())["heartbeat_at"]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if json.loads(self.reporter.path.read_text())["heartbeat_at"] != initial:
                break
            time.sleep(0.02)
        self.assertNotEqual(json.loads(self.reporter.path.read_text())["heartbeat_at"], initial)

    def test_corrupt_state_and_partial_csv_are_reported(self):
        self.reporter.path.write_text("{", encoding="utf-8")
        (self.root / "application_log.csv").write_text("timestamp,status,title,company,job_id\n2026-01-01", encoding="utf-8")
        data = build_snapshot(self.root)
        self.assertEqual(data["bot"]["health"], "unavailable")
        self.assertTrue(data["warnings"])
        self.assertEqual(data["scopes"]["all"]["processed"], 0)

    def test_log_write_failure_does_not_hide_run_outcome(self):
        self.reporter.start()
        self.reporter.record_application({"job_id": "1"}, "CONFIRMED", persisted=False)
        data = build_snapshot(self.root)
        self.assertEqual(data["scopes"]["run"]["confirmed"], 1)
        self.assertTrue(data["bot"]["history_write_failed"])

    def test_main_reports_search_failure_without_applying(self):
        import main
        page = Mock()
        page.title.return_value = "Feed | LinkedIn"
        page.url = "https://www.linkedin.com/feed/"
        playwright = Mock()
        playwright.chromium.launch_persistent_context.return_value.new_page.return_value = page
        context = Mock()
        context.__enter__ = Mock(return_value=playwright)
        context.__exit__ = Mock(return_value=False)
        with patch.object(main, "reporter", self.reporter), patch.object(main, "sync_playwright", return_value=context), \
                patch.object(main, "cleanup_stale_playwright_session"), patch.object(main, "navigate_page"), patch.object(main, "hard_refresh"), \
                patch.object(main, "is_logged_in", return_value=True), patch.object(main, "_check_logged_in", return_value=True), \
                patch.object(main, "search_easy_apply_jobs", side_effect=TimeoutError("search timeout")), \
                patch.object(main, "apply_for_jobs") as applications, patch.object(main, "time", Mock(sleep=Mock(side_effect=KeyboardInterrupt))):
            main.main()
        data = build_snapshot(self.root)
        applications.assert_not_called()
        self.assertEqual(data["bot"]["health"], "stopped")
        self.assertEqual(data["bot"]["last_error"]["stage"], "searching")
        self.assertEqual(data["bot"]["consecutive_failures"], 1)
        self.assertEqual(data["scopes"]["run"]["processed"], 0)

    def test_application_results_reach_monitor_without_real_actions(self):
        from utils import apply
        self.reporter.start()
        with patch.object(apply, "reporter", self.reporter), patch.object(apply, "KB"), \
                patch.object(apply, "apply_easy_apply_job", side_effect=["CONFIRMED", "SUBMIT_UNCONFIRMED"]), \
                patch.object(apply, "log_application", return_value=True), patch.object(apply, "save_questions_data"), \
                patch.object(apply, "random_sleep"):
            apply.KB.stats.return_value = (0, 0, 0)
            self.assertEqual(apply.apply_for_jobs(Mock(), [{"job_id": "1"}, {"job_id": "2"}]), 1)
        data = build_snapshot(self.root)
        self.assertEqual(data["scopes"]["run"]["confirmed"], 1)
        self.assertEqual(data["scopes"]["run"]["unconfirmed"], 1)

    def test_browser_renders_live_counts_errors_and_untrusted_titles_as_text(self):
        server = create_server(self.root, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.reporter.start()
        self.reporter.record_application({"job_id": "123", "title": "<img src=x onerror=window.injected=true>"}, "CONFIRMED")
        self.reporter.record_application({"job_id": "124"}, "SUBMIT_UNCONFIRMED")
        self.reporter.update(cycle_target=10, cycle_confirmed=1, cycle_processed=2,
                             cycle_candidate_limit=50, cycle_pages=1, cycle_page_limit=5)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}")
                expect(page.locator("#health")).to_have_text("Bot reporting", timeout=5000)
                page.locator("#scope").select_option("run")
                self.assertEqual(page.locator("#confirmed").inner_text(), "1")
                self.assertEqual(page.locator("#unconfirmed").inner_text(), "1")
                self.assertEqual(page.locator("#cycle-target").inner_text(), "1 / 10 bot-confirmed submissions")
                self.assertIn("2 / 50 jobs processed", page.locator("#cycle-limits").inner_text())
                self.assertEqual(page.locator("#results img").count(), 0)
                self.assertIn("<img src=x", page.locator("#results").inner_text())
                self.reporter.error(TimeoutError("private value"), "search", cycle_failed=True)
                expect(page.locator("#health")).to_have_text("Bot reporting errors", timeout=5000)
                self.assertIn("Timed out", page.locator("#last-error").inner_text())
                self.reporter.stop()
                expect(page.locator("#health")).to_have_text("Bot stopped", timeout=5000)
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_http_is_read_only_loopback_and_no_file_serving(self):
        server = create_server(self.root, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(base + "/api/status") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        for path, headers, expected in [
            ("/.env", {}, 404),
            ("/api/status", {"Host": "attacker.example"}, 403),
            ("/api/status", {"Origin": "https://attacker.example"}, 403),
        ]:
            with self.subTest(path=path, headers=headers), self.assertRaises(HTTPError) as error:
                urlopen(Request(base + path, headers=headers))
            self.assertEqual(error.exception.code, expected)
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(base + "/api/status", data=b"{}", method="POST"))
        self.assertEqual(error.exception.code, 405)


if __name__ == "__main__":
    unittest.main()
