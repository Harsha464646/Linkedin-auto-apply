import random
import time
from contextlib import contextmanager

import yaml
from playwright.sync_api import Page
from utils.monitoring import reporter

with open("config.yaml") as f:
    config = yaml.safe_load(f)["automation"]


@contextmanager
def timed_action(label):
    reporter.update(activity=label)
    started = time.monotonic()
    outcome = "ok"
    try:
        yield
    except Exception:
        outcome = "error"
        raise
    finally:
        if config.get("timing_logs", True):
            print(f"  [timing] {label}: {time.monotonic() - started:.2f}s ({outcome})")


def navigate_page(page, url, selector=None, label="navigation", timeout=None):
    with timed_action(label):
        page.goto(url, wait_until="domcontentloaded",
                  timeout=timeout or config.get("navigation_timeout_ms", 30000))
        if selector:
            page.wait_for_selector(selector, state="visible",
                                   timeout=config.get("readiness_timeout_ms", 10000))


def hard_refresh(page):
    """Reload the browser tab with the HTTP cache bypassed, like Ctrl+Shift+R."""
    with timed_action("hard refresh"):
        applying = page.evaluate("""() => Array.from(document.querySelectorAll('dialog[open], div[role="dialog"]'))
            .some(dialog => dialog.getClientRects().length > 0 &&
                Array.from(dialog.querySelectorAll('button')).some(button =>
                    button.getClientRects().length > 0 &&
                    /^(next|continue to next step|review|submit|submit application)$/i.test(button.innerText.trim())))""")
        if applying:
            raise RuntimeError("An application form is still open; review or close it before refreshing")
        session = page.context.new_cdp_session(page)
        try:
            with page.expect_navigation(wait_until="domcontentloaded",
                                        timeout=config.get("navigation_timeout_ms", 30000)):
                session.send("Page.reload", {"ignoreCache": True})
        finally:
            session.detach()


def bounded_click(element, label="click", timeout=None, force=False):
    with timed_action(label):
        if force and (not element.is_visible() or not element.is_enabled()):
            raise ValueError("Cannot force-click a hidden or disabled control")
        element.click(timeout=timeout or config.get("action_timeout_ms", 3000), force=force)


def random_sleep(min_s=None, max_s=None):
    """
    Sleep for a random duration between min_s and max_s (seconds).
    If not provided, uses values from config.
    """
    if min_s is None:
        min_s = config["min_action_delay"]
    if max_s is None:
        max_s = config["max_action_delay"]
    # small skew: sample from a beta-like distribution to favor shorter waits but allow long tails
    u = random.random()
    # bias a bit toward shorter durations
    bias = u**1.4
    wait = min_s + bias * (max_s - min_s)
    time.sleep(wait)
    return wait


def human_type(page: Page, selector: str, text: str):
    """
    Type text into input like a human: uses per-keystroke delay randomized by config.
    Falls back to page.fill if typing is too slow for big text like a resume field.
    """
    if not text:
        return
    # if the text is long, it's often better to fill directly after a short pause
    if len(text) > 200:
        random_sleep(0.2, 0.8)
        page.fill(selector, text)
        random_sleep()
        return

    # per-char delay in ms
    min_ms = config["typing_delay_min"]
    max_ms = config["typing_delay_max"]

    # choose a per-keystroke delay
    delay = random.randint(min_ms, max_ms)

    page.click(selector)  # focus
    random_sleep(0.05, 0.2)
    page.type(selector, text, delay=delay)  # ms per char
    random_sleep()


def human_click(page: Page, selector: str, timeout=30000):
    """
    Click an element like a human: wait for it, move mouse a bit (if available), then click.
    Uses random_sleep before and after click to mimic human reaction.
    """
    # small pre-click random pause
    random_sleep()
    locator = page.locator(selector)
    locator.wait_for(state="visible", timeout=timeout)
    try:
        # Try a real click
        locator.click()
    except Exception:
        # fallback: use JS click (less human-like but robust)
        page.evaluate("el => el.click()", locator.element_handle())
    random_sleep()


def wait_for_page_full_load(page, selector=None, timeout=45000):
    """
    Wait for full load on pages like LinkedIn that constantly make background requests.
    - Waits for DOM + load.
    - Optionally waits for a key selector (e.g. main content).
    - Then waits until the rendered content actually settles.

    The settle step replaces what used to be a flat 2.5-5s sleep on every
    navigation. LinkedIn usually finishes rendering well inside that window, so
    the fixed sleep was mostly dead time; polling returns as soon as the DOM
    stops changing and only falls back to the full budget when it really is
    still loading.
    """
    page.wait_for_load_state("domcontentloaded", timeout=timeout)
    page.wait_for_load_state("load", timeout=timeout)

    # Optional: wait for main content or feed to be visible
    if selector:
        try:
            page.wait_for_selector(selector, timeout=timeout)
        except Exception:
            print(f"⚠️ Selector {selector} not found within {timeout} ms.")

    wait_until_settled(page)


def wait_until_settled(page, max_wait=None, poll=0.25, stable_polls=2):
    """Block until the DOM stops growing, then add a small human-like jitter."""
    if max_wait is None:
        max_wait = config.get("max_settle_wait", 4.0)

    deadline = time.monotonic() + max_wait
    last_size, stable = -1, 0
    while time.monotonic() < deadline:
        try:
            size = page.evaluate("() => document.body ? document.body.innerHTML.length : 0")
        except Exception:
            break
        if size == last_size:
            stable += 1
            if stable >= stable_polls:
                break
        else:
            stable = 0
            last_size = size
        time.sleep(poll)

    # Keep a short randomized pause so interactions still look human.
    random_sleep()
