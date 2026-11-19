from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import yaml
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from utils.humanize import navigate_page, timed_action

with open("config.yaml") as f:
    config = yaml.safe_load(f)["job_search"]


# Track applied/seen job IDs across cycles to avoid re-processing
_seen_job_ids: set = set()


SEARCH_READY_TIMEOUT_MS = 15000
SCROLL_RENDER_TIMEOUT_MS = 1200
_CARD_SELECTOR = (
    "div[data-job-id], li[data-occludable-job-id], "
    "div[data-view-name='job-card'], a[href*='/jobs/view/'], "
    "a[href*='/jobs/collections/']"
)
_EMPTY_SELECTOR = (
    ".jobs-search-no-results-banner, .jobs-search-results-list__no-results, "
    ".jobs-search-results-list__empty-state, .jobs-search-two-pane__no-results, "
    "[data-test-job-search-no-results]"
)


def normalize_search_url(url, page_offset=0):
    """Preserve search filters while enforcing Easy Apply and one current offset."""
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in {"f_AL", "start", "currentJobId"}
    ]
    query.append(("f_AL", "true"))
    if page_offset > 0:
        query.append(("start", str(page_offset)))
    return urlunsplit(parts._replace(query=urlencode(query)))


_RESULT_LIST_JS = """cards => {
    for (const sentinel of document.querySelectorAll('div[data-results-list-top-scroll-sentinel]')) {
        for (let sibling = sentinel.nextElementSibling; sibling; sibling = sibling.nextElementSibling) {
            const candidates = sibling.matches('ul, [role="list"]')
                ? [sibling] : Array.from(sibling.querySelectorAll('ul, [role="list"]'));
            for (const list of candidates) {
                const rect = list.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0
                    && !['hidden', 'collapse'].includes(getComputedStyle(list).visibility)
                    && list.querySelector(cards)) return list;
            }
        }
    }
    return null;
}"""


def _wait_for_results(page):
    """Wait only for populated search results or a visible known empty state."""
    state = page.wait_for_function(
        """({cards, empty}) => {
            const visible = el => el && el.getBoundingClientRect().height > 0 &&
                el.getBoundingClientRect().width > 0 &&
                !['hidden', 'collapse'].includes(getComputedStyle(el).visibility);
            const list = (""" + _RESULT_LIST_JS + """)(cards);
            if (list) return 'results';
            if ([...document.querySelectorAll(empty)].some(visible)) return 'empty';
            return false;
        }""",
        arg={"cards": _CARD_SELECTOR, "empty": _EMPTY_SELECTOR},
        timeout=SEARCH_READY_TIMEOUT_MS,
    )
    try:
        return state.json_value()
    finally:
        state.dispose()


def _scroll_container(ul_container):
    """Resolve the list's nearest genuinely scrollable ancestor."""
    return ul_container.evaluate_handle(
        """list => {
            for (let el = list; el; el = el.parentElement) {
                if (/(auto|scroll|overlay)/.test(getComputedStyle(el).overflowY) &&
                    el.scrollHeight > el.clientHeight + 1) return el;
            }
            return document.scrollingElement || list;
        }"""
    ).as_element()


def _wait_for_new_cards(page, ul_container, jobs):
    """Give lazy or virtualized cards a bounded render window, including at bottom."""
    try:
        state = page.wait_for_function(
            r"""({list, cards, known}) => {
                const seen = new Set(known);
                return [...list.querySelectorAll(cards)].some(card => {
                    const anchors = card.matches('a[href]') ? [card] : Array.from(card.querySelectorAll('a[href*="/jobs/"]'));
                    const anchor = anchors.find(link => (link.innerText || '').trim());
                    if (!anchor) return false;
                    const href = anchor.getAttribute('href');
                    const path = href && new URL(href, 'https://www.linkedin.com').pathname;
                    const owner = card.closest('[data-job-id], [data-occludable-job-id]');
                    const id = (owner && list.contains(owner) &&
                        (owner.getAttribute('data-job-id') ||
                         owner.getAttribute('data-occludable-job-id'))) ||
                        (path && path.startsWith('/jobs/view/') && path.split('/')[3]);
                    const key = id || (href && new URL(href, 'https://www.linkedin.com').href);
                    return key && !seen.has(key);
                });
            }""",
            arg={
                "list": ul_container,
                "cards": _CARD_SELECTOR,
                "known": [job["job_id"] or job["link"] for job in jobs],
            },
            timeout=SCROLL_RENDER_TIMEOUT_MS,
        )
        state.dispose()
    except PlaywrightTimeoutError:
        pass


def job_key(job):
    return str(job.get("job_id") or job.get("link") or "")


def mark_job_seen(job):
    key = job_key(job)
    if key:
        _seen_job_ids.add(key)


def search_easy_apply_jobs(page, scroll_times=10, page_offset=0, limit=None, mark_seen=True):
    limit = max(0, int(config.get("max_jobs", 10) if limit is None else limit))
    with timed_action("search total"):
        return _search_easy_apply_jobs(page, scroll_times, page_offset, limit, mark_seen)


def _search_easy_apply_jobs(page, scroll_times, page_offset, limit, mark_seen):
    print(f"🔍 Searching for Easy Apply jobs (offset={page_offset})")
    url = config["url"]
    # Append pagination offset to get fresh results each cycle
    url = normalize_search_url(url, page_offset)
    navigate_page(page, url, label="search navigation")
    if _wait_for_results(page) == "empty":
        return []

    # Find the scroll container after the sentinel div
    sentinel = page.query_selector("div[data-results-list-top-scroll-sentinel]")
    if not sentinel:
        raise Exception("❌ Could not find job results sentinel container.")

    # The next sibling <ul> is the job list container (dynamic class name)
    ul_container = page.evaluate_handle(_RESULT_LIST_JS, _CARD_SELECTOR).as_element()
    if not ul_container:
        raise Exception("❌ Could not find job list container <ul>.")

    jobs = []
    seen_ids = set()
    _extract_job_cards(ul_container, jobs, seen_ids)

    # Bring the UL into view and focus it to simulate human interaction
    page.evaluate(
        "(el) => el.scrollIntoView({block: 'center', inline: 'center'})", ul_container
    )
    page.evaluate("(el) => el.focus({preventScroll: true})", ul_container)

    # Move mouse over it
    box = ul_container.bounding_box()
    print("Box:", box)
    if box:
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        print("🖱️ Hovered and focused job list container.")

    # Perform controlled scrolls inside the UL container
    with timed_action("search scrolling"):
        scroll_times = max(0, min(10, scroll_times))
        for i in range(scroll_times):
            _extract_job_cards(ul_container, jobs, seen_ids)
            if sum(job_key(job) not in _seen_job_ids for job in jobs) >= limit:
                break
            previous_count = len(jobs)
            scroll_container = _scroll_container(ul_container)
            print(f"📜 Scrolling job list... ({i + 1}/{scroll_times})")
            try:
                position = page.evaluate(
                    """el => {
                        const before = el.scrollTop;
                        el.scrollBy({top: Math.max(1, el.clientHeight * 0.85), behavior: 'instant'});
                        return {moved: el.scrollTop !== before,
                                atEnd: el.scrollTop + el.clientHeight >= el.scrollHeight - 1};
                    }""",
                    scroll_container,
                )
            finally:
                scroll_container.dispose()
            _wait_for_new_cards(page, ul_container, jobs)
            _extract_job_cards(ul_container, jobs, seen_ids)
            if len(jobs) == previous_count and (not position["moved"] or position["atEnd"]):
                break

    # Filter out jobs we've already seen/applied to in previous cycles
    new_jobs = []
    for job in jobs:
        if len(new_jobs) >= limit:
            break
        key = job_key(job)
        if key and key in _seen_job_ids:
            continue
        if mark_seen:
            mark_job_seen(job)
        new_jobs.append(job)

    print(f"✅ Collected {len(jobs)} jobs total, {len(new_jobs)} new (excluded {len(jobs) - len(new_jobs)} already seen or over apply limit).")
    return new_jobs


def _extract_job_cards(ul_container, jobs, seen_ids):
    # Extract job cards using multiple strategies
    print("🔍 Extracting visible job cards...")

    # Strategy 1: data-job-id divs (classic LinkedIn)
    job_divs = ul_container.query_selector_all("div[data-job-id]")

    # Strategy 2: data-occludable-job-id (newer LinkedIn)
    job_divs.extend(ul_container.query_selector_all("li[data-occludable-job-id]"))

    # Strategy 3: job-card data-view-name elements (newest LinkedIn)
    job_divs.extend(ul_container.query_selector_all("div[data-view-name='job-card']"))

    # Strategy 4: Use JS to find all job card elements
    if not job_divs:
        print("⚠️ Standard selectors failed. Using JS fallback...")
        job_divs = ul_container.query_selector_all("a[href*='/jobs/view/'], a[href*='/jobs/collections/']")

    print(f"🧩 Found {len(job_divs)} job card containers.")

    for div in job_divs:
        try:
            # Extract job ID from data attributes or href
            job_id = div.get_attribute("data-job-id") or div.get_attribute("data-occludable-job-id") or ""
            if job_id and job_id in seen_ids:
                continue

            # Try to get title and company from the card
            links = [div] if div.evaluate("el => el.matches('a[href]')") else div.query_selector_all("a[href*='/jobs/']")
            title_el = next((link for link in links if (link.inner_text() or "").strip()), None)
            if not title_el:
                continue

            # Try multiple company selectors
            company_el = (
                div.query_selector(".artdeco-entity-lockup__subtitle")
                or div.query_selector("span[data-test-app-aware-link]")
                or div.query_selector("div:nth-child(2) span")
            )

            title = "Unknown"
            link = None

            if title_el:
                title = title_el.inner_text().strip().split("\n")[0].strip()
                href = title_el.get_attribute("href") or ""
                if href:
                    link = urljoin("https://www.linkedin.com", href)

            # If we got a job_id but no link, construct the direct URL
            if not link and job_id:
                link = f"https://www.linkedin.com/jobs/view/{job_id}/"

            # Try to extract job_id from link if we don't have it
            if not job_id and link and "/jobs/view/" in link:
                try:
                    job_id = link.split("/jobs/view/")[1].split("/")[0].split("?")[0]
                except Exception:
                    pass

            company = company_el.inner_text().strip() if company_el else "Unknown"

            # Skip duplicates
            key = job_id or link
            if key and key in seen_ids:
                continue

            if link:
                seen_ids.add(key)
                # Since search URL has f_AL=true, all results are Easy Apply
                jobs.append(
                    {
                        "title": title,
                        "company": company,
                        "link": link,
                        "job_id": job_id,
                    }
                )
        except TimeoutError:
            continue
        except Exception as e:
            print(f"⚠️ Error parsing job card: {e}")
            continue
