import os
import glob
import random
import subprocess
import sys
import time
import yaml
from datetime import datetime, timedelta
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

# Ensure emoji / unicode log output does not crash on Windows consoles that
# default to cp1252 encoding.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

from utils.apply import apply_for_jobs
from utils.humanize import hard_refresh, navigate_page
from utils.login import (
    is_logged_in,
    perform_login,
    save_cookies,
    wait_for_page_full_load,
)
from utils.search import job_key, mark_job_seen, search_easy_apply_jobs
from utils.monitoring import reporter

USER_DATA_DIR = "./user_data"


@dataclass(frozen=True)
class CycleResult:
    confirmed: int
    processed: int
    errors: int
    pages_scanned: int
    next_offset: int
    target_reached: bool
    reason: str


def cycle_delay(result, failure_count, idle_count, settings):
    pause_min = max(1, int(settings.get("cycle_pause_min_seconds", 600)))
    pause_max = max(pause_min, int(settings.get("cycle_pause_max_seconds", 900)))
    if failure_count:
        base = max(1, int(settings.get("cycle_error_backoff_seconds", 30)))
        streak = failure_count
        cap = max(base, int(settings.get("cycle_max_backoff_seconds", 300)))
        backoff = min(cap, base * 2 ** min(max(0, streak - 1), 10))
        return max(pause_min, backoff)
    if result is not None and result.confirmed == 0:
        base = max(1, int(settings.get("cycle_idle_backoff_seconds", 60)))
        streak = idle_count
        cap = max(base, int(settings.get("cycle_max_backoff_seconds", 300)))
        backoff = min(cap, base * 2 ** min(max(0, streak - 1), 10))
        return max(pause_min, backoff)
    return random.randint(pause_min, pause_max)


def cleanup_stale_playwright_session():
    """Remove stale lock files from user_data so Playwright can launch
    without needing to kill the user's personal Google Chrome."""
    data_dir = Path(USER_DATA_DIR)
    if not data_dir.exists():
        return

    # Remove Chromium lock files that prevent relaunch after a crash
    lock_files = ["SingletonLock", "SingletonCookie", "SingletonSocket"]
    for lock in lock_files:
        lock_path = data_dir / lock
        if lock_path.exists():
            try:
                lock_path.unlink()
                print(f"  Removed stale lock: {lock}")
            except Exception:
                pass

    # On Windows, kill only orphaned Playwright Chromium processes
    # (NOT the user's Google Chrome) by matching the user_data_dir argument
    if os.name == "nt":
        try:
            abs_data_dir = str(data_dir.resolve()).replace("\\", "\\\\")
            result = subprocess.run(
                ["powershell", "-Command",
                 f"Get-WmiObject Win32_Process -Filter \"Name='chrome.exe' OR Name='chromium.exe'\" | "
                 f"Where-Object {{ $_.CommandLine -match [regex]::Escape('{abs_data_dir}') }} | "
                 f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"],
                capture_output=True, text=True, timeout=10
            )
        except Exception:
            pass
    print("  Playwright session cleanup done.")


def _check_logged_in(page):
    """More robust login check — looks for Feed title OR linkedin.com/feed in URL."""
    try:
        title = page.title().lower()
        url = urlsplit(page.url.lower())
        host, path = url.hostname or "", url.path
        if host != "linkedin.com" and not host.endswith(".linkedin.com"):
            return False
        if path.startswith(("/login", "/checkpoint", "/uas", "/authwall")):
            return False
        # Logged in if we see the feed page or any authenticated LinkedIn page
        feed_page = path == "/feed" or path.startswith("/feed/")
        # Also check for messaging, notifications, etc. — means logged in
        known_page = any(path == prefix or path.startswith(prefix + "/")
                         for prefix in ["/messaging", "/notifications", "/mynetwork", "/jobs"])
        # Check if login form is present — means NOT logged in
        login_input = page.query_selector("#username")
        if login_input:
            return False
        # Recognize authenticated paths without trusting redirect query strings.
        return feed_page or known_page or "feed | linkedin" in title
    except Exception:
        return False


def run_apply_cycle(page, browser, cycle_num, page_offset=0, settings=None, max_offset=300):
    """Seek a confirmation target within bounded candidate and search-page budgets."""
    settings = settings or {}
    target = int(settings.get("target_confirmed_per_cycle", 10))
    max_candidates = int(settings.get("max_candidates_per_cycle", 50))
    max_pages = int(settings.get("max_pages_per_cycle", 5))
    if min(target, max_candidates, max_pages) <= 0:
        raise ValueError("Cycle target and candidate and page limits must be positive")
    reporter.update(phase="checking_session", activity="Checking LinkedIn session", cycle=cycle_num,
                    page_offset=page_offset, current_job=None, next_cycle_at=None, results_found=0,
                    cycle_confirmed=0, cycle_processed=0, cycle_target=target, cycle_pages=0,
                    cycle_candidate_limit=max_candidates, cycle_page_limit=max_pages)
    reporter.event("cycle", f"Cycle {cycle_num} started (offset {page_offset})")
    print(f"\n{'='*60}")
    print(f"🔄 Cycle {cycle_num} started at {datetime.now().strftime('%I:%M:%S %p')} (page offset: {page_offset})")
    print(f"{'='*60}")

    # Re-check login before each cycle
    if not _check_logged_in(page):
        reporter.update(phase="logging_in", activity="Restoring LinkedIn session")
        print("🔐 Session expired. Logging in again...")
        perform_login(page)
        wait_for_page_full_load(page)
        save_cookies(browser)
    else:
        print("✅ Session active, proceeding...")

    # Pause for manual security verification if LinkedIn triggers a CAPTCHA
    if "security" in page.title().lower() or "checkpoint" in page.url.lower():
        reporter.update(phase="awaiting_input", activity="Waiting for manual security verification")
        print("🛡️ Security verification detected! Please complete it in the browser window.")
        input("Press Enter here after completing the verification...")
        wait_for_page_full_load(page)

    confirmed = processed = pages = found = errors = 0
    visited = set()
    reason = "page_limit"

    def on_processed(job, status):
        nonlocal confirmed, processed, errors
        key = job_key(job)
        if key in visited:
            return
        visited.add(key)
        transient = status in {"ERROR", "NO_MODAL"}
        if not transient:
            mark_job_seen(job)
        if transient:
            errors += 1
        processed += 1
        confirmed += status == "CONFIRMED"
        reporter.update(cycle_confirmed=confirmed, cycle_processed=processed)

    for pages in range(1, max_pages + 1):
        remaining = max_candidates - processed
        reporter.update(phase="searching", activity="Searching for Easy Apply jobs",
                        page_offset=page_offset, cycle_pages=pages, current_job=None)
        jobs = search_easy_apply_jobs(page, page_offset=page_offset, limit=min(25, remaining), mark_seen=False)
        jobs = list({job_key(job): job for job in jobs if job_key(job) and job_key(job) not in visited}.values())[:remaining]
        terms = settings.get("eligible_title_terms", [])
        if terms:
            relevant = []
            for job in jobs:
                title = " ".join((job.get("title") or "").lower().replace("-", " ").split())
                if any(term.lower() in title for term in terms):
                    relevant.append(job)
                else:
                    visited.add(job_key(job))
                    mark_job_seen(job)
                    reporter.event("filtered", f"Outside configured data-engineering titles: {job.get('title', 'Unknown')}")
            jobs = relevant
        found += len(jobs)
        reporter.update(results_found=found)
        reporter.event("search", f"Collected {len(jobs)} candidates at offset {page_offset}")
        if jobs:
            print("Applying for jobs")
            apply_for_jobs(page=page, jobs=jobs, limit=len(jobs),
                           target_confirmed=target - confirmed, on_processed=on_processed)
        if confirmed >= target:
            reason = "target_reached"
            break
        if processed >= max_candidates:
            reason = "candidate_limit"
            break
        page_offset += 25
        if page_offset > max_offset:
            page_offset = 0
            reason = "search_end"
            break

    summary = f"{confirmed}/{target} bot-confirmed; {processed} jobs processed ({errors} errors) across {pages} search pages ({reason})"
    reporter.update(last_cycle_result=summary, consecutive_failures=0, current_job=None)
    reporter.event("cycle_complete", summary)
    print(summary)
    print(f"\n✅ Cycle {cycle_num} complete at {datetime.now().strftime('%I:%M:%S %p')}")
    # Return cycle totals and a resume offset so unprocessed candidates are not skipped.
    return CycleResult(confirmed, processed, errors, pages, page_offset, confirmed >= target, reason)


def _run_bot():
    with open("config.yaml") as f:
        settings = yaml.safe_load(f)
    config = settings.get("automation", {})
    cycle_settings = settings.get("job_search", {})

    # Clean up stale Playwright locks — does NOT touch the user's Google Chrome
    cleanup_stale_playwright_session()

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=config.get("headless", False),
            args=[
                "--start-maximized",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--remote-debugging-port=9223",
            ],
        )

        browser.set_default_timeout(config.get("action_timeout_ms", 3000))
        browser.set_default_navigation_timeout(config.get("navigation_timeout_ms", 30000))
        print("✅ Using existing session.")
        page = browser.new_page()
        navigate_page(page, "https://www.linkedin.com/feed/", selector="main", label="feed navigation")

        # Initial login check
        if not is_logged_in(page):
            reporter.update(phase="logging_in", activity="Restoring LinkedIn session")
            print("🔐 Session expired. Logging in again...")
            perform_login(page)
            wait_for_page_full_load(page)
            save_cookies(browser)
        else:
            print("🎉 Logged in successfully using existing session.")

        print(f"🌐 Current Page Title: {page.title()}")

        # Pause for manual security verification if LinkedIn triggers a CAPTCHA
        if "security" in page.title().lower() or "checkpoint" in page.url.lower():
            reporter.update(phase="awaiting_input", activity="Waiting for manual security verification")
            print("🛡️ Security verification detected! Please complete it in the browser window.")
            input("Press Enter here after completing the verification...")
            wait_for_page_full_load(page)
            print(f"🌐 Page Title after verification: {page.title()}")

        cycle_num = 0
        page_offset = 0
        # LinkedIn search only returns a few hundred results; past this the
        # pages come back empty. When we reach the end (or an empty page) we
        # loop back to offset 0 to re-scan for freshly posted jobs.
        MAX_OFFSET = 300
        failure_count = idle_count = 0
        # Track consecutive zero-confirmation cycles for daily limit detection
        zero_confirm_streak = 0
        daily_limit_threshold = max(1, int(config.get("daily_limit_zero_cycles", 2)))

        while True:
            cycle_num += 1
            result = None
            failed = False
            try:
                result = run_apply_cycle(page, browser, cycle_num, page_offset=page_offset,
                                         settings=cycle_settings, max_offset=MAX_OFFSET)
            except Exception as e:
                reporter.error(e, reporter.state.get("phase", "cycle"), cycle_failed=True)
                print(f"⚠️ Cycle {cycle_num} error: {e}")
                failed = True

            # Resume unprocessed candidates on the same page when a target is met.
            # Search pagination advances inside each bounded cycle and resets at
            # the end, so previously unprocessed jobs are not silently skipped.
            page_offset = result.next_offset if result else reporter.state.get("page_offset", page_offset)

            # Daily limit detection: if several consecutive cycles process
            # a meaningful number of jobs but confirm none (mostly errors),
            # LinkedIn has likely hit its daily Easy Apply cap.
            if result and result.confirmed == 0 and result.processed >= 5 and result.errors >= result.processed // 2:
                zero_confirm_streak += 1
            else:
                zero_confirm_streak = 0

            if zero_confirm_streak >= daily_limit_threshold:
                reporter.update(phase="daily_limit", activity="Daily Easy Apply limit likely reached",
                                current_job=None, next_cycle_at=None)
                reporter.event("daily_limit",
                               f"Stopping: {zero_confirm_streak} consecutive cycles with 0 confirmations and high error rates")
                print(f"\n{'='*60}")
                print(f"Daily Easy Apply limit likely reached ({zero_confirm_streak} consecutive zero-confirmation cycles).")
                print(f"Stopping bot. Try again tomorrow.")
                print(f"{'='*60}")
                break

            try:
                reporter.update(phase="refreshing", activity="Hard-refreshing after cycle", current_job=None)
                hard_refresh(page)
                reporter.event("refresh", "Hard refresh completed (browser cache bypassed)")
            except Exception as e:
                reporter.error(e, "hard refresh", cycle_failed=not failed)
                print(f"Hard refresh failed: {type(e).__name__}")
                failed = True

            failure_count = failure_count + 1 if failed else 0
            idle_count = idle_count + 1 if not failed and result and result.confirmed == 0 else 0
            delay = cycle_delay(result, failure_count, idle_count, config)

            next_cycle = datetime.now().astimezone() + timedelta(seconds=delay)
            if failed:
                activity = "Error backoff"
            elif result and result.confirmed == 0:
                activity = "No confirmations this cycle; pausing before retry"
            else:
                activity = f"Cycle complete ({result.confirmed if result else 0} confirmed); pausing before next cycle"
            reporter.update(phase="pausing", next_cycle_at=next_cycle.isoformat(), activity=activity)
            reporter.event("pausing", f"{activity}: {delay}s; next cycle at {next_cycle.strftime('%H:%M:%S')}")
            print(f"\n{activity}")
            print(f"Next cycle in {delay // 60}m {delay % 60}s (at {next_cycle.strftime('%I:%M:%S %p')}). Press Ctrl+C to stop.")

            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                print("\n🛑 Stopped by user. Closing browser...")
                break

        browser.close()
        print("👋 Done. Browser closed.")


def main():
    reporter.start()
    try:
        _run_bot()
    except KeyboardInterrupt:
        print("Bot stopped.")
    except Exception as error:
        reporter.error(error, reporter.state.get("phase", "startup"))
        raise
    finally:
        reporter.stop()


if __name__ == "__main__":
    main()
