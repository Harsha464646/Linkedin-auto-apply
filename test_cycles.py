import threading
import unittest
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

import main
from utils import apply, humanize


def jobs(start, count):
    return [{"job_id": str(i), "title": "Test job", "company": "Test"} for i in range(start, start + count)]


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.page = Mock()
        self.page.title.return_value = "Jobs | LinkedIn"
        self.page.url = "https://www.linkedin.com/jobs/search/"
        self.stack.enter_context(patch.object(main, "_check_logged_in", return_value=True))
        self.reporter = self.stack.enter_context(patch.object(main, "reporter", Mock(state={})))
        self.seen = self.stack.enter_context(patch.object(main, "mark_job_seen", create=True))
        self.stack.enter_context(patch.object(apply, "save_questions_data"))
        self.stack.enter_context(patch.object(apply, "log_application", return_value=True))
        knowledge = self.stack.enter_context(patch.object(apply, "KB"))
        knowledge.stats.return_value = (0, 0, 0)

    def apply_results(self, statuses):
        def process(page, jobs, limit, target_confirmed, on_processed):
            confirmed = 0
            for job in jobs[:limit]:
                status = statuses.get(job["job_id"], "CONFIRMED")
                on_processed(job, status)
                confirmed += status == "CONFIRMED"
                if confirmed >= target_confirmed:
                    break
            return confirmed
        return process

    def test_targets_ten_confirmations_across_pages_and_preserves_leftovers(self):
        statuses = {str(i): "NO_EASY_APPLY" for i in range(7, 11)}
        with patch.object(main, "search_easy_apply_jobs", side_effect=[jobs(1, 10), jobs(11, 10)]) as search, \
                patch.object(main, "apply_for_jobs", side_effect=self.apply_results(statuses)) as applying:
            result = main.run_apply_cycle(self.page, Mock(), 1)
        self.assertEqual(result.confirmed, 10)
        self.assertEqual(result.processed, 14)
        self.assertEqual(result.pages_scanned, 2)
        self.assertTrue(result.target_reached)
        self.assertEqual(result.next_offset, 25)
        self.assertEqual([c.kwargs["target_confirmed"] for c in applying.call_args_list], [10, 4])
        self.assertEqual([c.kwargs["page_offset"] for c in search.call_args_list], [0, 25])
        self.assertTrue(all(c.kwargs["mark_seen"] is False for c in search.call_args_list))
        self.assertEqual(self.seen.call_count, 14)

    def test_candidate_budget_stops_unproductive_cycle(self):
        with patch.object(main, "search_easy_apply_jobs", return_value=jobs(1, 10)), \
                patch.object(main, "apply_for_jobs", side_effect=self.apply_results({str(i): "INCOMPLETE" for i in range(1, 11)})):
            result = main.run_apply_cycle(self.page, Mock(), 1, settings={"max_candidates_per_cycle": 3})
        self.assertEqual(result.processed, 3)
        self.assertEqual(result.confirmed, 0)
        self.assertEqual(result.reason, "candidate_limit")
        self.assertFalse(result.target_reached)

    def test_empty_pages_are_bounded(self):
        with patch.object(main, "search_easy_apply_jobs", return_value=[]) as search, patch.object(main, "apply_for_jobs") as applying:
            result = main.run_apply_cycle(self.page, Mock(), 1, settings={"max_pages_per_cycle": 2})
        self.assertEqual(search.call_count, 2)
        applying.assert_not_called()
        self.assertEqual(result.next_offset, 50)
        self.assertEqual(result.reason, "page_limit")

    def test_end_of_search_resets_offset_without_looping_within_cycle(self):
        with patch.object(main, "search_easy_apply_jobs", return_value=[]) as search:
            result = main.run_apply_cycle(self.page, Mock(), 1, page_offset=300)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(result.next_offset, 0)
        self.assertEqual(result.reason, "search_end")

    def test_duplicate_candidates_do_not_inflate_target(self):
        with patch.object(main, "search_easy_apply_jobs", side_effect=[jobs(1, 2), jobs(1, 2)]), \
                patch.object(main, "apply_for_jobs", side_effect=self.apply_results({})):
            result = main.run_apply_cycle(self.page, Mock(), 1, settings={"max_pages_per_cycle": 2})
        self.assertEqual(result.confirmed, 2)
        self.assertEqual(result.processed, 2)

    def test_related_title_filter_keeps_warehouse_and_excludes_unrelated_roles(self):
        candidates = jobs(1, 4)
        for job, title in zip(candidates, ["Senior Data Engineer", "Data Warehouse Engineer", "Staff Firmware Engineer", "Analytics Engineer"]):
            job["title"] = title
        settings = {"max_pages_per_cycle": 1, "eligible_title_terms": ["data engineer", "data warehouse", "analytics engineer"]}
        with patch.object(main, "search_easy_apply_jobs", return_value=candidates), \
                patch.object(main, "apply_for_jobs", side_effect=self.apply_results({})) as applying:
            result = main.run_apply_cycle(self.page, Mock(), 1, settings=settings)
        self.assertEqual(result.confirmed, 3)
        self.assertEqual([job["job_id"] for job in applying.call_args.kwargs["jobs"]], ["1", "2", "4"])
        self.assertTrue(any(c.args[0] == "filtered" for c in self.reporter.event.call_args_list))

    def test_transient_errors_remain_eligible_for_a_later_cycle(self):
        with patch.object(main, "search_easy_apply_jobs", return_value=jobs(1, 2)), \
                patch.object(main, "apply_for_jobs", side_effect=self.apply_results({"1": "ERROR"})):
            result = main.run_apply_cycle(self.page, Mock(), 1, settings={"max_pages_per_cycle": 1})
        self.assertEqual(result.processed, 2)
        self.assertEqual(result.confirmed, 1)
        self.seen.assert_called_once_with(jobs(2, 1)[0])

    def test_errors_are_skipped_and_cycle_continues(self):
        with patch.object(main, "search_easy_apply_jobs", return_value=jobs(1, 10)), \
                patch.object(main, "apply_for_jobs", side_effect=self.apply_results({str(i): "ERROR" for i in range(1, 11)})):
            result = main.run_apply_cycle(self.page, Mock(), 1, settings={"max_pages_per_cycle": 1})
        self.assertEqual(result.processed, 10)
        self.assertEqual(result.confirmed, 0)
        self.assertEqual(result.errors, 10)
        self.seen.assert_not_called()

    def test_error_count_tracked_in_cycle_result(self):
        with patch.object(main, "search_easy_apply_jobs", return_value=jobs(1, 5)), \
                patch.object(main, "apply_for_jobs", side_effect=self.apply_results(
                    {"1": "ERROR", "3": "NO_MODAL", "5": "ERROR"})):
            result = main.run_apply_cycle(self.page, Mock(), 1, settings={"max_pages_per_cycle": 1})
        self.assertEqual(result.processed, 5)
        self.assertEqual(result.confirmed, 2)
        self.assertEqual(result.errors, 3)

    def test_apply_stops_at_target_and_never_counts_unconfirmed(self):
        outcomes = ["NO_EASY_APPLY", "CONFIRMED", "SUBMIT_UNCONFIRMED", "CONFIRMED", "CONFIRMED"]
        processed = Mock()
        with patch.object(apply, "apply_easy_apply_job", side_effect=outcomes) as action, \
                patch.object(apply, "KB") as knowledge, patch.object(apply, "log_application", return_value=True), \
                patch.object(apply, "save_questions_data"), patch.object(apply, "random_sleep"):
            knowledge.stats.return_value = (0, 0, 0)
            count = apply.apply_for_jobs(self.page, jobs(1, 5), target_confirmed=2, on_processed=processed)
        self.assertEqual(count, 2)
        self.assertEqual(action.call_count, 4)
        self.assertEqual(processed.call_count, 4)


class RefreshTests(unittest.TestCase):
    def test_real_chromium_hard_reload_fetches_fresh_cacheable_document(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path != "/":
                    self.send_error(404)
                    return
                requests.append(self.path)
                body = f"<main><h1>Document {len(requests)}</h1></main>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                self.assertEqual(page.locator("h1").inner_text(), "Document 1")
                humanize.hard_refresh(page)
                self.assertEqual(page.locator("h1").inner_text(), "Document 2")
                self.assertEqual(len(requests), 2)
            finally:
                browser.close()

    def test_hard_refresh_bypasses_cache_and_waits_for_new_navigation(self):
        page = Mock()
        page.evaluate.return_value = False
        navigation = Mock()
        navigation.__enter__ = Mock()
        navigation.__exit__ = Mock(return_value=False)
        page.expect_navigation.return_value = navigation
        session = page.context.new_cdp_session.return_value
        humanize.hard_refresh(page)
        page.expect_navigation.assert_called_once_with(wait_until="domcontentloaded", timeout=30000)
        session.send.assert_called_once_with("Page.reload", {"ignoreCache": True})
        session.detach.assert_called_once()
        page.keyboard.press.assert_not_called()

    def test_refresh_does_not_interrupt_open_application_form(self):
        page = Mock()
        page.evaluate.return_value = True
        with self.assertRaises(RuntimeError):
            humanize.hard_refresh(page)
        page.context.new_cdp_session.assert_not_called()

    def test_refresh_detaches_even_if_navigation_fails(self):
        page = Mock()
        page.evaluate.return_value = False
        navigation = Mock()
        navigation.__enter__ = Mock()
        navigation.__exit__ = Mock(side_effect=TimeoutError("navigation timeout"))
        page.expect_navigation.return_value = navigation
        session = page.context.new_cdp_session.return_value
        with self.assertRaises(TimeoutError):
            humanize.hard_refresh(page)
        session.detach.assert_called_once()

    def test_success_pauses_and_errors_back_off_with_floor(self):
        success = main.CycleResult(10, 10, 0, 1, 0, True, "target_reached")
        empty = main.CycleResult(0, 0, 0, 5, 125, False, "page_limit")
        # Successful cycle returns a random pause between min and max
        delay = main.cycle_delay(success, 0, 0, {})
        self.assertGreaterEqual(delay, 600)
        self.assertLessEqual(delay, 900)
        # Custom pause range
        delay = main.cycle_delay(success, 0, 0, {"cycle_pause_min_seconds": 60, "cycle_pause_max_seconds": 120})
        self.assertGreaterEqual(delay, 60)
        self.assertLessEqual(delay, 120)
        # Error backoff is floored at pause_min (default 600)
        self.assertGreaterEqual(main.cycle_delay(None, 1, 0, {}), 600)
        self.assertGreaterEqual(main.cycle_delay(None, 20, 0, {}), 600)
        # Idle backoff is also floored at pause_min
        self.assertGreaterEqual(main.cycle_delay(empty, 0, 1, {}), 600)

    def test_next_cycle_pauses_after_refresh(self):
        sequence = []
        success = main.CycleResult(10, 10, 0, 1, 0, True, "target_reached")
        def cycle(*args, **kwargs):
            sequence.append("cycle")
            if len(sequence) > 2:
                raise KeyboardInterrupt
            return success
        page = Mock()
        page.title.return_value = "Feed | LinkedIn"
        page.url = "https://www.linkedin.com/feed/"
        playwright = Mock()
        playwright.chromium.launch_persistent_context.return_value.new_page.return_value = page
        context = Mock(__enter__=Mock(return_value=playwright), __exit__=Mock(return_value=False))
        with patch.object(main, "sync_playwright", return_value=context), patch.object(main, "cleanup_stale_playwright_session"), \
                patch.object(main, "navigate_page"), patch.object(main, "is_logged_in", return_value=True), \
                patch.object(main, "run_apply_cycle", side_effect=cycle), patch.object(main, "reporter", Mock(state={})), \
                patch.object(main, "hard_refresh", side_effect=lambda page: sequence.append("refresh"), create=True), \
                patch.object(main, "time", Mock()) as clock:
            with self.assertRaises(KeyboardInterrupt):
                main._run_bot()
        # First cycle completes -> refresh -> sleep (pause) -> second cycle raises KeyboardInterrupt
        self.assertEqual(sequence, ["cycle", "refresh", "cycle"])
        # The mandatory pause sleep was called once between the two cycles
        self.assertEqual(clock.sleep.call_count, 1)
        delay = clock.sleep.call_args.args[0]
        self.assertGreaterEqual(delay, 600)
        self.assertLessEqual(delay, 900)


    def test_daily_limit_stops_bot_after_consecutive_zero_confirmation_cycles(self):
        call_count = [0]
        def cycle(*args, **kwargs):
            call_count[0] += 1
            # Simulate a cycle with many jobs processed, 0 confirmed, all errors
            return main.CycleResult(confirmed=0, processed=10, errors=10,
                                    pages_scanned=1, next_offset=0,
                                    target_reached=False, reason="page_limit")
        page = Mock()
        page.title.return_value = "Feed | LinkedIn"
        page.url = "https://www.linkedin.com/feed/"
        playwright = Mock()
        playwright.chromium.launch_persistent_context.return_value.new_page.return_value = page
        context = Mock(__enter__=Mock(return_value=playwright), __exit__=Mock(return_value=False))
        with patch.object(main, "sync_playwright", return_value=context), patch.object(main, "cleanup_stale_playwright_session"), \
                patch.object(main, "navigate_page"), patch.object(main, "is_logged_in", return_value=True), \
                patch.object(main, "run_apply_cycle", side_effect=cycle), \
                patch.object(main, "reporter", Mock(state={})) as mock_reporter, \
                patch.object(main, "hard_refresh"), \
                patch.object(main, "time", Mock()):
            main._run_bot()
        # Default daily_limit_zero_cycles is 2, so the bot should run exactly 2 cycles then stop
        self.assertEqual(call_count[0], 2)
        # Verify the reporter was told about the daily limit
        daily_limit_calls = [c for c in mock_reporter.update.call_args_list if c.kwargs.get("phase") == "daily_limit"]
        self.assertTrue(len(daily_limit_calls) > 0)

    def test_daily_limit_resets_on_successful_cycle(self):
        call_count = [0]
        def cycle(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First cycle: zero confirmations, all errors
                return main.CycleResult(0, 10, 10, 1, 0, False, "page_limit")
            elif call_count[0] == 2:
                # Second cycle: success — resets the streak
                return main.CycleResult(5, 10, 2, 1, 0, False, "page_limit")
            elif call_count[0] == 3:
                # Third cycle: zero confirmations again
                return main.CycleResult(0, 10, 10, 1, 0, False, "page_limit")
            else:
                raise KeyboardInterrupt
        page = Mock()
        page.title.return_value = "Feed | LinkedIn"
        page.url = "https://www.linkedin.com/feed/"
        playwright = Mock()
        playwright.chromium.launch_persistent_context.return_value.new_page.return_value = page
        context = Mock(__enter__=Mock(return_value=playwright), __exit__=Mock(return_value=False))
        with patch.object(main, "sync_playwright", return_value=context), patch.object(main, "cleanup_stale_playwright_session"), \
                patch.object(main, "navigate_page"), patch.object(main, "is_logged_in", return_value=True), \
                patch.object(main, "run_apply_cycle", side_effect=cycle), \
                patch.object(main, "reporter", Mock(state={})), \
                patch.object(main, "hard_refresh"), \
                patch.object(main, "time", Mock()):
            with self.assertRaises(KeyboardInterrupt):
                main._run_bot()
        # Streak was reset by the successful cycle, so bot didn't stop at daily limit
        self.assertEqual(call_count[0], 4)



if __name__ == "__main__":
    unittest.main()
