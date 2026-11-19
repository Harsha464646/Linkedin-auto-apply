import unittest
from contextlib import ExitStack, nullcontext
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from utils import search


class SearchURLTests(unittest.TestCase):
    def test_preserves_filters_and_enforces_easy_apply(self):
        url = (
            "https://www.linkedin.com/jobs/search/?keywords=data%20engineer"
            "&f_TPR=r3600&f_AL=false&f_AL=true&start=25&start=50"
            "&currentJobId=123&geoId=42&facet=a&facet=b&blank=#results"
        )
        result = urlsplit(search.normalize_search_url(url, 75))
        self.assertEqual(result.fragment, "results")
        self.assertEqual(parse_qs(result.query, keep_blank_values=True), {
            "keywords": ["data engineer"], "f_TPR": ["r3600"],
            "f_AL": ["true"], "start": ["75"], "geoId": ["42"],
            "facet": ["a", "b"], "blank": [""],
        })

    def test_first_page_removes_stale_offset_and_selected_job(self):
        result = search.normalize_search_url(
            "https://www.linkedin.com/jobs/search/?start=50&currentJobId=123", 0
        )
        self.assertEqual(parse_qs(urlsplit(result).query), {"f_AL": ["true"]})

    def test_url_without_query(self):
        result = search.normalize_search_url("https://www.linkedin.com/jobs/search/", 25)
        self.assertEqual(parse_qs(urlsplit(result).query), {
            "f_AL": ["true"], "start": ["25"],
        })


class SearchPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 900, "height": 700})
        self.context.route("**/*", lambda route: route.abort())
        self.page = self.context.new_page()
        self.page.set_default_timeout(1500)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(self.context.close)
        self.stack.enter_context(patch.object(self.page, "goto", return_value=None))
        self.navigate = self.stack.enter_context(patch.object(
            search, "navigate_page", return_value=None, create=True
        ))
        self.timing = self.stack.enter_context(patch.object(
            search, "timed_action", side_effect=lambda label: nullcontext(), create=True
        ))
        self.stack.enter_context(patch.object(search, "random_sleep", create=True))
        self.stack.enter_context(patch.object(search, "wait_for_page_full_load", create=True))
        self.stack.enter_context(patch.object(search, "SCROLL_RENDER_TIMEOUT_MS", 350, create=True))
        self.stack.enter_context(patch.object(search, "SEARCH_READY_TIMEOUT_MS", 1500, create=True))
        self.stack.enter_context(patch.dict(search.config, {
            "url": "https://www.linkedin.com/jobs/search/?start=99&currentJobId=42&f_TPR=r3600",
            "max_jobs": 100,
        }))
        self.stack.enter_context(patch.object(search, "_seen_job_ids", set()))

    def fixture(self, cards=None, parent_scroll=False, script=""):
        if cards is None:
            cards = self.cards("1", "2")
        parent_style = "height:120px;overflow-y:auto" if parent_scroll else ""
        list_style = "" if parent_scroll else "height:120px;overflow-y:auto"
        self.page.set_content(f"""
            <style>ul {{margin:0;padding:0}} li {{height:100px;list-style:none}}</style>
            <aside><div data-job-id="sidebar"><a href="/jobs/view/sidebar/">Sidebar</a></div></aside>
            <section id="scroller" style="{parent_style}">
                <div data-results-list-top-scroll-sentinel></div>
                <ul id="results" style="{list_style}">{cards}</ul>
            </section>
            <script>
                window.scrollCalls = 0;
                const originalScrollBy = Element.prototype.scrollBy;
                Element.prototype.scrollBy = function(...args) {{
                    window.scrollCalls++;
                    return originalScrollBy.apply(this, args);
                }};
                {script}
            </script>
        """)

    @staticmethod
    def cards(*ids):
        return "".join(
            f'<li data-occludable-job-id="{jid}"><a href="/jobs/view/{jid}/">Job {jid}</a>'
            '<span class="artdeco-entity-lockup__subtitle">Company</span></li>'
            for jid in ids
        )

    def job_ids(self, **kwargs):
        return [job["job_id"] for job in search.search_easy_apply_jobs(self.page, **kwargs)]

    def test_scoped_results_navigation_and_timing(self):
        self.fixture()
        self.assertEqual(self.job_ids(scroll_times=0, page_offset=25), ["1", "2"])
        self.navigate.assert_called_once()
        query = parse_qs(urlsplit(self.navigate.call_args.args[1]).query)
        self.assertEqual(query, {"f_AL": ["true"], "f_TPR": ["r3600"], "start": ["25"]})
        self.assertEqual([call.args[0] for call in self.timing.call_args_list],
                         ["search total", "search scrolling"])

    def test_banner_between_sentinel_and_results_list(self):
        self.fixture(script="""
            document.querySelector('[data-results-list-top-scroll-sentinel]').insertAdjacentHTML(
                'afterend', '<section class="jobs-search-cjs-deprecation-banner"><div>Search update</div></section>'
            );
        """)
        self.assertEqual(self.job_ids(scroll_times=0), ["1", "2"])

    def test_banner_list_is_not_mistaken_for_results(self):
        self.fixture(script="""
            document.querySelector('[data-results-list-top-scroll-sentinel]').insertAdjacentHTML(
                'afterend', '<section><ul><li>Search tips</li></ul></section><div>Update</div>'
            );
        """)
        self.assertEqual(self.job_ids(scroll_times=0), ["1", "2"])

    def test_unrelated_sidebar_does_not_satisfy_missing_results(self):
        from playwright.sync_api import TimeoutError
        self.fixture(cards="")
        with patch.object(search, "SEARCH_READY_TIMEOUT_MS", 200):
            with self.assertRaises(TimeoutError):
                search._wait_for_results(self.page)

    def test_no_growth_stops_before_ten_scrolls(self):
        self.fixture()
        self.assertEqual(self.job_ids(), ["1", "2"])
        self.assertEqual(self.page.evaluate("window.scrollCalls"), 1)

    def test_virtualized_same_count_and_delayed_bottom_render(self):
        replacement = self.cards("3", "4")
        self.fixture(script=f"""
            results.addEventListener('scroll', () => {{
                if (window.loaded) return;
                window.loaded = true;
                setTimeout(() => {{ results.innerHTML = `{replacement}`; }}, 120);
            }});
        """)
        self.assertEqual(self.job_ids(), ["1", "2", "3", "4"])
        self.assertGreaterEqual(self.page.evaluate("window.scrollCalls"), 2)
        self.assertLess(self.page.evaluate("window.scrollCalls"), 10)

    def test_scroll_continues_through_known_cards_before_lazy_boundary(self):
        replacement = self.cards("3", "4")
        self.fixture(script=f"""
            results.style.height = '60px';
            results.addEventListener('scroll', () => {{
                if (window.loaded || results.scrollTop < 90) return;
                window.loaded = true;
                setTimeout(() => {{ results.innerHTML = `{replacement}`; }}, 120);
            }});
        """)
        self.assertEqual(self.job_ids(), ["1", "2", "3", "4"])

    def test_enough_candidates_avoids_unnecessary_scrolling(self):
        self.fixture(cards=self.cards("1", "2", "3"))
        with patch.dict(search.config, {"max_jobs": 2}):
            self.assertEqual(self.job_ids(), ["1", "2"])
        self.assertEqual(self.page.evaluate("window.scrollCalls"), 0)

    def test_non_scrollable_list_uses_scrollable_parent(self):
        replacement = self.cards("3", "4")
        self.fixture(parent_scroll=True, script=f"""
            scroller.addEventListener('scroll', () => {{
                if (window.loaded) return;
                window.loaded = true;
                setTimeout(() => {{ results.innerHTML = `{replacement}`; }}, 100);
            }});
        """)
        self.assertEqual(self.job_ids(), ["1", "2", "3", "4"])
        self.assertGreater(self.page.evaluate("scroller.scrollTop"), 0)
        self.assertEqual(self.page.evaluate("results.scrollTop"), 0)

    def test_delayed_initial_cards_are_ready(self):
        self.fixture(cards="", script=f"""
            setTimeout(() => {{ results.innerHTML = `{self.cards('1', '2')}`; }}, 120);
        """)
        self.assertEqual(self.job_ids(scroll_times=0), ["1", "2"])

    def test_empty_results_without_sentinel(self):
        self.page.set_content('<div class="jobs-search-no-results-banner">No matching jobs found.</div>')
        self.assertEqual(self.job_ids(), [])

    def test_delayed_empty_results_after_sentinel(self):
        self.fixture(cards="", script="""
            setTimeout(() => {
                document.body.insertAdjacentHTML('beforeend',
                    '<div class="jobs-search-no-results-banner">No jobs found</div>');
            }, 120);
        """)
        self.assertEqual(self.job_ids(), [])
        self.assertEqual(self.page.evaluate("window.scrollCalls"), 0)

    def test_hidden_empty_banner_does_not_override_loading_results(self):
        self.fixture(cards="", script=f"""
            document.body.insertAdjacentHTML('beforeend',
                '<div class="jobs-search-no-results-banner" hidden>No jobs found</div>');
            setTimeout(() => {{ results.innerHTML = `{self.cards('1')}`; }}, 120);
        """)
        self.assertEqual(self.job_ids(scroll_times=0), ["1"])

    def test_scroll_budget_is_capped_at_ten(self):
        self.fixture(script="""
            let nextId = 3;
            Element.prototype.scrollBy = function() {
                window.scrollCalls++;
                results.innerHTML = `<li data-occludable-job-id="${nextId}">
                    <a href="/jobs/view/${nextId}/">Job ${nextId}</a></li>`;
                nextId++;
            };
        """)
        self.assertEqual(self.job_ids(scroll_times=100), [str(i) for i in range(1, 13)])
        self.assertEqual(self.page.evaluate("window.scrollCalls"), 10)

    def test_mixed_card_versions_are_all_collected(self):
        self.fixture(cards=(
            '<li><div data-job-id="1"><a href="/jobs/view/1/">Classic</a></div></li>'
            + self.cards("2")
            + '<li><div data-view-name="job-card"><a href="/jobs/view/3/">New</a></div></li>'
        ))
        self.assertEqual(set(self.job_ids(scroll_times=0)), {"1", "2", "3"})

    def test_known_collection_links_do_not_end_render_wait_early(self):
        initial = self.cards("1", "2").replace("/jobs/view/1/", "/jobs/collections/recommended/?currentJobId=1")
        self.fixture(cards=initial, script=f"""
            results.addEventListener('scroll', () => {{
                if (window.loaded) return;
                window.loaded = true;
                setTimeout(() => {{ results.innerHTML = `{self.cards('3', '4')}`; }}, 250);
            }});
        """)
        self.assertEqual(self.job_ids(), ["1", "2", "3", "4"])

    def test_jobs_past_apply_limit_remain_unseen(self):
        self.fixture(cards=self.cards("1", "2", "3"))
        with patch.dict(search.config, {"max_jobs": 2}):
            self.assertEqual(self.job_ids(scroll_times=0), ["1", "2"])
            self.assertEqual(search._seen_job_ids, {"1", "2"})
            self.assertEqual(self.job_ids(scroll_times=0), ["3"])

    def test_unprocessed_candidates_remain_available_after_target_stop(self):
        self.fixture(cards=self.cards("1", "2", "3"))
        batch = search.search_easy_apply_jobs(self.page, scroll_times=0, limit=2, mark_seen=False)
        self.assertEqual([job["job_id"] for job in batch], ["1", "2"])
        self.assertEqual(search._seen_job_ids, set())
        search.mark_job_seen(batch[0])
        following = search.search_easy_apply_jobs(self.page, scroll_times=0, limit=2, mark_seen=False)
        self.assertEqual([job["job_id"] for job in following], ["2", "3"])
        self.assertEqual(search._seen_job_ids, {"1"})

    def test_title_ignores_empty_logo_link_and_leading_newline(self):
        self.fixture(cards='<li data-occludable-job-id="7"><a href="/jobs/view/7/"><span></span></a><a href="/jobs/view/7/" style="white-space:pre-line">\n Data Warehouse Engineer\n Verified</a><span class="artdeco-entity-lockup__subtitle">Test</span></li>')
        jobs = search.search_easy_apply_jobs(self.page, scroll_times=0)
        self.assertEqual(jobs[0]["title"], "Data Warehouse Engineer")

    def test_unrendered_placeholders_are_not_processed_as_unknown_jobs(self):
        self.fixture(cards=self.cards("1") + '<li data-occludable-job-id="2"></li>')
        self.assertEqual(self.job_ids(scroll_times=0), ["1"])

    def test_placeholder_hydration_is_waited_for_when_scrolling(self):
        self.fixture(cards=self.cards("1") + '<li data-occludable-job-id="2"></li>', script="""
            results.addEventListener('scroll', () => {
                setTimeout(() => {
                    const item = results.querySelector('[data-occludable-job-id="2"]');
                    item.innerHTML = '<a href="/jobs/view/2/">Data Engineer</a>';
                }, 150);
            }, {once: true});
        """)
        self.assertEqual(self.job_ids(), ["1", "2"])

    def test_link_only_fallback_extracts_id_and_title(self):
        self.fixture(cards='<li><a href="/jobs/view/7/?tracking=x">Data Engineer</a></li>')
        jobs = search.search_easy_apply_jobs(self.page, scroll_times=0)
        self.assertEqual([job["job_id"] for job in jobs], ["7"])
        self.assertEqual(jobs[0]["title"], "Data Engineer")


if __name__ == "__main__":
    unittest.main()
