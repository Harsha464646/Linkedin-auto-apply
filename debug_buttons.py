"""Debug: find the apply button on a LinkedIn job detail page."""
import yaml
from playwright.sync_api import sync_playwright
from utils.humanize import random_sleep
from utils.login import is_logged_in, perform_login, save_cookies, wait_for_page_full_load
from utils.search import search_easy_apply_jobs

def main():
    with open("config.yaml") as f:
        config = yaml.safe_load(f).get("automation", {})

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir="./user_data",
            headless=False,
            args=[
                "--start-maximized",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-gpu",
                "--disable-dev-shm-usage",
            ],
        )
        page = browser.new_page()
        page.goto("https://www.linkedin.com/feed/")
        wait_for_page_full_load(page)
        random_sleep()

        if not is_logged_in(page):
            print("Logging in...")
            perform_login(page)
            wait_for_page_full_load(page)
            save_cookies(browser)

        if "security" in page.title().lower() or "checkpoint" in page.url.lower():
            input("Complete security verification, then press Enter...")
            wait_for_page_full_load(page)

        print(f"Page title: {page.title()}")

        jobs = search_easy_apply_jobs(page)
        if not jobs:
            print("No jobs found!")
            browser.close()
            return

        job = jobs[0]
        print(f"\nGoing to: {job['title']} at {job['company']}")
        page.goto(job["link"])
        wait_for_page_full_load(page)
        random_sleep()

        # Dump all buttons, links, and clickable elements
        results = page.evaluate("""
            () => {
                const out = [];
                // All buttons
                document.querySelectorAll('button').forEach((el, i) => {
                    out.push({
                        type: 'button',
                        index: i,
                        text: el.innerText.trim().substring(0, 100),
                        id: el.id || '',
                        ariaLabel: el.getAttribute('aria-label') || '',
                        classes: (el.className || '').substring(0, 120),
                        visible: el.offsetParent !== null,
                        html: el.outerHTML.substring(0, 250),
                    });
                });
                // Also look for divs/spans with role=button
                document.querySelectorAll('[role="button"]').forEach((el, i) => {
                    out.push({
                        type: 'role-button',
                        index: i,
                        text: el.innerText.trim().substring(0, 100),
                        id: el.id || '',
                        ariaLabel: el.getAttribute('aria-label') || '',
                        tag: el.tagName,
                        classes: (el.className || '').substring(0, 120),
                        visible: el.offsetParent !== null,
                        html: el.outerHTML.substring(0, 250),
                    });
                });
                return out;
            }
        """)

        print("\n=== ALL BUTTONS & ROLE=BUTTON ELEMENTS ===")
        for item in results:
            text = item.get('text', '')
            # Only print items that have text or mention apply
            if text or 'apply' in str(item).lower():
                print(f"  [{item['type']} {item['index']}] text='{text}' id='{item.get('id','')}' aria='{item.get('ariaLabel','')}' visible={item.get('visible')}")
                print(f"    html: {item['html'][:200]}")
                print()

        input("Press Enter to close...")
        browser.close()

if __name__ == "__main__":
    main()
