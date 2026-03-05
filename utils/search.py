import yaml

from utils.humanize import random_sleep
from utils.login import wait_for_page_full_load

with open("config.yaml") as f:
    config = yaml.safe_load(f)["job_search"]


def search_easy_apply_jobs(page, scroll_times=10):
    print("🔍 Searching for Easy Apply jobs")
    page.goto(config["url"])
    wait_for_page_full_load(page)
    random_sleep()

    # Find the scroll container after the sentinel div
    sentinel = page.query_selector("div[data-results-list-top-scroll-sentinel]")
    if not sentinel:
        raise Exception("❌ Could not find job results sentinel container.")

    # The next sibling <ul> is the job list container (dynamic class name)
    ul_container = sentinel.evaluate_handle("el => el.nextElementSibling")
    if not ul_container:
        raise Exception("❌ Could not find job list container <ul>.")

    # Bring the UL into view and focus it to simulate human interaction
    page.evaluate(
        "(el) => el.scrollIntoView({block: 'center', inline: 'center'})", ul_container
    )
    page.evaluate("(el) => el.focus()", ul_container)
    random_sleep()

    # Move mouse over it
    box = ul_container.bounding_box()
    print("Box:", box)
    if box:
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        print("🖱️ Hovered and focused job list container.")

    # Perform controlled scrolls inside the UL container
    for i in range(scroll_times):
        print(f"📜 Scrolling job list... ({i + 1}/{scroll_times})")
        page.evaluate(
            """(ul) => { ul.scrollBy(0, ul.scrollHeight / 2); }""", ul_container
        )
        random_sleep()

    # Extract job cards using multiple strategies
    print("🔍 Extracting visible job cards...")

    jobs = []
    seen_ids = set()

    # Strategy 1: data-job-id divs (classic LinkedIn)
    job_divs = page.query_selector_all("div[data-job-id]")

    # Strategy 2: data-occludable-job-id (newer LinkedIn)
    if not job_divs:
        job_divs = page.query_selector_all("li[data-occludable-job-id]")

    # Strategy 3: job-card data-view-name elements (newest LinkedIn)
    if not job_divs:
        job_divs = page.query_selector_all("div[data-view-name='job-card']")

    # Strategy 4: Use JS to find all job card elements
    if not job_divs:
        print("⚠️ Standard selectors failed. Using JS fallback...")
        job_divs = page.query_selector_all("a[href*='/jobs/view/'], a[href*='/jobs/collections/']")

    print(f"🧩 Found {len(job_divs)} job card containers.")

    for div in job_divs:
        try:
            # Extract job ID from data attributes or href
            job_id = div.get_attribute("data-job-id") or div.get_attribute("data-occludable-job-id") or ""

            # Try to get title and company from the card
            title_el = div.query_selector("a[href*='/jobs/']")
            if not title_el:
                title_el = div.query_selector("a")

            # Try multiple company selectors
            company_el = (
                div.query_selector(".artdeco-entity-lockup__subtitle")
                or div.query_selector("span[data-test-app-aware-link]")
                or div.query_selector("div:nth-child(2) span")
            )

            title = "Unknown"
            link = None

            if title_el:
                title = title_el.inner_text().split("\n")[0].strip()
                href = title_el.get_attribute("href") or ""
                if href:
                    if href.startswith("http"):
                        link = href
                    else:
                        link = "https://www.linkedin.com" + href

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
            if job_id and job_id in seen_ids:
                continue
            if job_id:
                seen_ids.add(job_id)

            if link:
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

    print(f"✅ Collected {len(jobs)} Easy Apply jobs.")
    return jobs
