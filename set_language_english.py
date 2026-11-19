"""One-purpose helper: switch the LinkedIn account display language to English.

Uses the existing logged-in persistent session in ./user_data. It navigates to
LinkedIn's language settings and selects "English" (which is always written in
Latin letters, so it is findable even while the page is currently in Arabic).

Run:  python set_language_english.py
"""

import time

from playwright.sync_api import sync_playwright

USER_DATA_DIR = "./user_data"

LAUNCH_ARGS = [
    "--start-maximized",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--remote-debugging-port=9224",
]


def screenshot(page, name):
    try:
        page.screenshot(path=name, full_page=True)
        print(f"📸 saved {name}")
    except Exception as e:
        print(f"⚠️ screenshot failed: {e}")


def try_click_english(page) -> bool:
    """Select English in the language control (native <select> preferred)."""
    # Strategy 1: native <select> — enumerate options, find the English one.
    try:
        selects = page.query_selector_all("select")
        print(f"🔎 found {len(selects)} <select> element(s)")
        for s in selects:
            options = s.query_selector_all("option")
            opts = []
            english_value = None
            for o in options:
                val = o.get_attribute("value") or ""
                txt = (o.inner_text() or "").strip()
                opts.append(f"{val!r}={txt!r}")
                low_val = val.lower()
                if (
                    "english" in txt.lower()
                    or low_val in ("en_us", "en-us", "en")
                    or low_val.startswith("en_")
                ):
                    english_value = val
            print(f"   options: {opts}")
            if english_value is not None:
                s.select_option(value=english_value)
                # Fire change/input events so LinkedIn's JS reacts + saves.
                s.evaluate(
                    "el => { el.dispatchEvent(new Event('input', {bubbles:true}));"
                    " el.dispatchEvent(new Event('change', {bubbles:true})); }"
                )
                print(f"✅ selected English in <select> value={english_value!r}")
                return True
    except Exception as e:
        print(f"⚠️ <select> strategy failed: {e}")

    # Strategy 2: custom dropdown — open it, then click the English row.
    try:
        combo = (
            page.query_selector("[role='combobox']")
            or page.query_selector("button[aria-haspopup='listbox']")
            or page.query_selector(".artdeco-dropdown__trigger")
        )
        if combo:
            combo.click()
            time.sleep(1.5)
            for sel in [
                "[role='option']:has-text('English')",
                "li:has-text('English')",
                "div:has-text('English')",
            ]:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    print(f"✅ selected English via custom dropdown: {sel}")
                    return True
    except Exception as e:
        print(f"⚠️ custom dropdown strategy failed: {e}")

    return False


def main():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            locale="en-US",
            args=LAUNCH_ARGS,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        print("🌐 Opening language settings...")
        try:
            page.goto(
                "https://www.linkedin.com/mypreferences/d/language",
                wait_until="domcontentloaded",
                timeout=45000,
            )
        except Exception as e:
            print(f"⚠️ navigation issue: {e}")
        time.sleep(6)
        screenshot(page, "lang_before.png")

        clicked = try_click_english(page)
        time.sleep(3)

        # Some LinkedIn language pickers auto-save; others show a Save/primary button.
        if clicked:
            for sel in [
                "button[type='submit']",
                "button.artdeco-button--primary",
                "button:has-text('Save')",
            ]:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        print(f"💾 clicked save via: {sel}")
                        break
                except Exception:
                    continue

        time.sleep(5)
        screenshot(page, "lang_after.png")

        # Verify by reloading the feed and checking <html lang>
        try:
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=45000)
            time.sleep(5)
            lang = page.get_attribute("html", "lang") or ""
            print(f"🔎 <html lang> = '{lang}'")
            screenshot(page, "feed_after.png")
            if lang.lower().startswith("en"):
                print("🎉 SUCCESS: LinkedIn UI is now English.")
            else:
                print("⚠️ Still not English — check lang_before.png / lang_after.png.")
        except Exception as e:
            print(f"⚠️ verification failed: {e}")

        print("⏳ Keeping browser open 20s so you can see the result...")
        time.sleep(20)
        ctx.close()


if __name__ == "__main__":
    main()
