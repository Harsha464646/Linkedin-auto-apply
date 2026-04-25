import os
import glob
import subprocess
import time
import yaml
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright

from utils.apply import apply_for_jobs
from utils.humanize import random_sleep
from utils.login import (
    is_logged_in,
    perform_login,
    save_cookies,
    wait_for_page_full_load,
)
from utils.search import search_easy_apply_jobs

CYCLE_INTERVAL_MINUTES = 15
USER_DATA_DIR = "./user_data"


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
        url = page.url.lower()
        # Logged in if we see the feed page or any authenticated LinkedIn page
        if "feed" in title or "/feed" in url:
            return True
        # Also check for messaging, notifications, etc. — means logged in
        if any(x in url for x in ["/messaging", "/notifications", "/mynetwork", "/jobs"]):
            return True
        # Check if login form is present — means NOT logged in
        login_input = page.query_selector("#username")
        if login_input:
            return False
        # If we're on linkedin.com but not on the login page, likely logged in
        if "linkedin.com" in url and "/login" not in url and "/checkpoint" not in url:
            return True
        return False
    except Exception:
        return False


def run_apply_cycle(page, browser, cycle_num, page_offset=0):
    """Run one search + apply cycle."""
    print(f"\n{'='*60}")
    print(f"🔄 Cycle {cycle_num} started at {datetime.now().strftime('%I:%M:%S %p')} (page offset: {page_offset})")
    print(f"{'='*60}")

    # Re-check login before each cycle
    page.goto("https://www.linkedin.com/feed/")
    wait_for_page_full_load(page)
    random_sleep()

    if not _check_logged_in(page):
        print("🔐 Session expired. Logging in again...")
        perform_login(page)
        wait_for_page_full_load(page)
        save_cookies(browser)
    else:
        print("✅ Session active, proceeding...")

    # Pause for manual security verification if LinkedIn triggers a CAPTCHA
    if "security" in page.title().lower() or "checkpoint" in page.url.lower():
        print("🛡️ Security verification detected! Please complete it in the browser window.")
        input("Press Enter here after completing the verification...")
        wait_for_page_full_load(page)

    jobs = search_easy_apply_jobs(page, page_offset=page_offset)
    print(jobs)

    print("Applying for jobs")
    result = apply_for_jobs(page=page, jobs=jobs)

    print(f"\n✅ Cycle {cycle_num} complete at {datetime.now().strftime('%I:%M:%S %p')}")
    return result


def main():
    with open("config.yaml") as f:
        config = yaml.safe_load(f).get("automation", {})

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

        print("✅ Using existing session.")
        page = browser.new_page()
        page.goto("https://www.linkedin.com/feed/")
        wait_for_page_full_load(page)
        random_sleep()

        # Initial login check
        if not is_logged_in(page):
            print("🔐 Session expired. Logging in again...")
            perform_login(page)
            wait_for_page_full_load(page)
            save_cookies(browser)
        else:
            print("🎉 Logged in successfully using existing session.")

        random_sleep()
        print(f"🌐 Current Page Title: {page.title()}")

        # Pause for manual security verification if LinkedIn triggers a CAPTCHA
        if "security" in page.title().lower() or "checkpoint" in page.url.lower():
            print("🛡️ Security verification detected! Please complete it in the browser window.")
            input("Press Enter here after completing the verification...")
            wait_for_page_full_load(page)
            print(f"🌐 Page Title after verification: {page.title()}")

        cycle_num = 0
        total_applied = 0

        while True:
            cycle_num += 1
            # Each cycle searches the next page of results (25 per page on LinkedIn)
            page_offset = (cycle_num - 1) * 25
            try:
                run_apply_cycle(page, browser, cycle_num, page_offset=page_offset)
            except Exception as e:
                print(f"⚠️ Cycle {cycle_num} error: {e}")

            next_time = datetime.now().strftime('%I:%M %p')
            print(f"\n⏳ Waiting {CYCLE_INTERVAL_MINUTES} minutes before next cycle...")
            print(f"   Next cycle at ~{next_time} + {CYCLE_INTERVAL_MINUTES}min. Press Ctrl+C to stop.\n")

            try:
                time.sleep(CYCLE_INTERVAL_MINUTES * 60)
            except KeyboardInterrupt:
                print("\n🛑 Stopped by user. Closing browser...")
                break

        browser.close()
        print("👋 Done. Browser closed.")


if __name__ == "__main__":
    main()
