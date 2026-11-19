import io
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from utils import apply, humanize


class ExperienceTests(unittest.TestCase):
    def test_profile_overrides_old_learned_years(self):
        questions = [
            "How many years of work experience do you have with Data Engineering?",
            "How many years of hands-on Python development experience do you have?",
            "Years of professional experience",
            "How many years have you worked with SQL?",
        ]
        with patch.object(apply, "YEARS_OF_EXPERIENCE", 6), patch.object(apply, "KB") as knowledge:
            knowledge.lookup.return_value = "5"
            for question in questions:
                with self.subTest(question=question):
                    self.assertEqual(apply.resolve_answer(question, kind="text"), ("6", "profile"))
            knowledge.lookup.assert_not_called()

    def test_experience_months_are_converted_from_years(self):
        with patch.object(apply, "YEARS_OF_EXPERIENCE", 6):
            self.assertEqual(apply.resolve_answer("How many months of professional experience do you have?", kind="number"), ("72", "profile"))

    def test_experience_options_use_exact_number_or_compatible_range(self):
        with patch.object(apply, "YEARS_OF_EXPERIENCE", 6):
            for options, expected in [(["16 years", "6 years"], "6 years"), (["0-2", "3-5", "6-10"], "6-10"), (["0", "1", "5+"], "5+")]:
                self.assertEqual(apply.resolve_answer("Years of work experience", kind="select", options=options), (expected, "profile"))

    def test_six_is_not_used_for_binary_or_unavailable_experience_options(self):
        with patch.object(apply, "YEARS_OF_EXPERIENCE", 6):
            for options in [["Yes", "No"], ["16 years", "5 years"]]:
                self.assertEqual(apply.resolve_answer("Years of work experience", kind="radio", options=options), (None, "none"))

    def test_other_saved_answers_are_unchanged(self):
        with patch.object(apply, "KB") as knowledge:
            knowledge.lookup.return_value = "42"
            for question in ["What is your age in years?", "Salary expectations", "How many projects have you completed?"]:
                self.assertEqual(apply.resolve_answer(question, kind="number"), ("42", "learned"))


class SponsorshipTests(unittest.TestCase):
    def test_without_sponsorship_uses_profile_not_learned_guess(self):
        with patch.object(apply, "REQUIRE_SPONSORSHIP", "No"), patch.object(apply, "KB") as knowledge:
            knowledge.lookup.return_value = "No"
            self.assertEqual(apply.resolve_answer("Are you comfortable to work without sponsorship?", kind="radio", options=["Yes", "No"]), ("Yes", "profile"))
            knowledge.lookup.assert_not_called()

    def test_required_sponsorship_uses_current_profile(self):
        with patch.object(apply, "REQUIRE_SPONSORSHIP", "No"), patch.object(apply, "KB") as knowledge:
            knowledge.lookup.return_value = "Yes"
            self.assertEqual(apply.resolve_answer("Will you now or in the future require visa sponsorship?"), ("No", "profile"))

    def test_sponsorship_required_profile_reverses_without_answer(self):
        with patch.object(apply, "REQUIRE_SPONSORSHIP", "Yes"):
            self.assertEqual(apply.resolve_answer("Can you work without employer sponsorship?"), ("No", "profile"))

    def test_negated_need_is_not_confused_with_need(self):
        with patch.object(apply, "REQUIRE_SPONSORSHIP", "No"):
            self.assertEqual(apply.resolve_answer("Can you confirm you do not require visa sponsorship?"), ("Yes", "profile"))

    def test_ambiguous_sponsorship_questions_are_not_guessed(self):
        with patch.object(apply, "KB") as knowledge:
            for question in ["Describe your sponsorship situation", "Are you uncomfortable working without sponsorship?", "What sponsorship category applies?"]:
                self.assertEqual(apply.resolve_answer(question), (None, "none"))
            knowledge.lookup.assert_not_called()
            knowledge.infer.assert_not_called()


class NavigationTests(unittest.TestCase):
    def test_navigation_waits_for_target_without_load_or_sleep(self):
        page = Mock()
        with patch.object(humanize, "random_sleep") as sleep:
            humanize.navigate_page(page, "https://example.test/jobs/", selector="main h1")
        page.goto.assert_called_once_with(
            "https://example.test/jobs/", wait_until="domcontentloaded", timeout=30000
        )
        page.wait_for_selector.assert_called_once_with("main h1", state="visible", timeout=10000)
        page.wait_for_load_state.assert_not_called()
        sleep.assert_not_called()

    def test_timing_reports_failure_without_exception_contents(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(ValueError):
            with humanize.timed_action("test action"):
                raise ValueError("private value")
        self.assertIn("[timing] test action", output.getvalue())
        self.assertIn("error", output.getvalue())
        self.assertNotIn("private value", output.getvalue())

    def test_click_timeout_is_bounded(self):
        button = Mock()
        humanize.bounded_click(button, label="Next")
        button.click.assert_called_once_with(timeout=3000, force=False)

    def test_failed_radio_exact_match_is_not_retried_as_substring(self):
        option, group = Mock(), Mock()
        group.query_selector_all.return_value = [option]
        with patch.object(apply, "_radio_option_text", return_value="Yes"), patch.object(apply, "_select_radio_target", return_value=False) as click:
            self.assertFalse(apply.select_radio_option(group, "Yes"))
        click.assert_called_once_with(option)

    def test_login_redirect_query_is_not_an_authenticated_path(self):
        from main import _check_logged_in
        page = Mock()
        page.title.return_value = "LinkedIn"
        page.query_selector.return_value = None
        for url in [
            "https://www.linkedin.com/login?session_redirect=/jobs/search/",
            "https://www.linkedin.com/login?session_redirect=%2Fjobs%2Fsearch",
            "https://example.test/jobs/view/123/",
        ]:
            with self.subTest(url=url):
                page.url = url
                self.assertFalse(_check_logged_in(page))
        page.url = "https://www.linkedin.com/jobs/view/123/"
        self.assertTrue(_check_logged_in(page))

    def test_submit_is_not_retried_after_click_timeout(self):
        page, button = Mock(), Mock()
        button.click.side_effect = PlaywrightTimeoutError("timeout")
        self.assertFalse(apply._finalize_submit(page, button, {"title": "test"}))
        self.assertEqual(button.click.call_count, 1)


class JobReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.route("**/*", lambda route: route.fulfill(
            content_type="text/html", body="<main><h1>Test engineer</h1><div id='actions'></div></main>"
        ))
        self.page.goto("https://example.test/jobs/view/123/")

    def tearDown(self):
        self.page.close()

    def test_prefilled_experience_fields_use_six_without_changing_other_fields(self):
        html = '''<main><p>Data Engineer</p><button onclick="document.querySelector('dialog').showModal()">Easy Apply</button></main>
            <dialog>
            <label for="years">How many years of work experience do you have?</label><input type="number" id="years" value="5">
            <label for="experience-select">Years of professional experience</label><select id="experience-select"><option selected>5</option><option>6</option></select>
            <fieldset><legend>Years of Python experience</legend><input type="radio" name="years" id="radio5" value="5" checked><label for="radio5">5</label><input type="radio" name="years" id="radio6" value="6"><label for="radio6">6</label></fieldset>
            <label for="projects">Projects completed</label><input type="number" id="projects" value="5">
            <button>Submit application</button></dialog>'''
        self.page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=html))
        def inspect_before_submit(page, button, job):
            self.assertEqual(page.locator("#years").input_value(), "6")
            self.assertEqual(page.locator("#experience-select").input_value(), "6")
            self.assertTrue(page.locator("#radio6").is_checked())
            self.assertEqual(page.locator("#projects").input_value(), "5")
            return False
        with patch.object(apply, "YEARS_OF_EXPERIENCE", 6), patch.object(apply, "KB"), \
                patch.object(apply, "PHONE", ""), patch.object(apply, "EMAIL", ""), patch.object(apply, "RESUME_PATH", ""), \
                patch.object(apply, "_finalize_submit", side_effect=inspect_before_submit) as submit:
            result = apply.apply_easy_apply_job(self.page, {"job_id": "123", "link": "https://example.test/jobs/view/123/"})
        self.assertEqual(result, "SUBMIT_UNCONFIRMED")
        submit.assert_called_once()

    def test_unsupported_experience_option_blocks_submission_instead_of_using_five(self):
        html = '''<main><p>Data Engineer</p><button onclick="document.querySelector('dialog').showModal()">Easy Apply</button></main>
            <dialog><label for="years">Years of work experience</label><select id="years"><option selected>5</option><option>16</option></select>
            <button>Submit application</button><button aria-label="Dismiss" onclick="this.closest('dialog').close()">Close</button></dialog>'''
        self.page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=html))
        with patch.object(apply, "YEARS_OF_EXPERIENCE", 6), patch.object(apply, "KB"), \
                patch.object(apply, "PHONE", ""), patch.object(apply, "EMAIL", ""), patch.object(apply, "RESUME_PATH", ""), \
                patch.object(apply, "_finalize_submit") as submit:
            result = apply.apply_easy_apply_job(self.page, {"job_id": "123", "link": "https://example.test/jobs/view/123/"})
        self.assertEqual(result, "INCOMPLETE")
        submit.assert_not_called()

    def test_headingless_live_layout_recognizes_easy_apply(self):
        self.page.set_content('<main><div><p>Senior Analytics Engineer</p><button aria-label="Easy Apply to this job">Easy Apply</button></div><h2>About the job</h2></main>')
        state, button = apply._wait_for_job_action(self.page, "123", timeout=500)
        self.assertEqual(state, "ready")
        self.assertEqual(button.inner_text(), "Easy Apply")

    def test_headingless_layout_preserves_identity_and_recommendation_guards(self):
        layouts = [
            '<main><p>Engineer</p><aside><button aria-label="Easy Apply to this job">Easy Apply</button></aside></main>',
            '<main><p>Engineer</p><h2>About the job</h2><p>Description</p><h2>More jobs for you</h2><button>Easy Apply</button></main>',
            '<main><p>Engineer</p><a href="/jobs/view/999/"><span role="button">Easy Apply</span></a></main>',
        ]
        for html in layouts:
            with self.subTest(html=html):
                self.page.set_content(html)
                with self.assertRaises(PlaywrightTimeoutError):
                    apply._wait_for_job_action(self.page, "123", timeout=150)
        self.page.set_content('<main><p>Engineer</p><button>Easy Apply</button></main>')
        with self.assertRaises(PlaywrightTimeoutError):
            apply._wait_for_job_action(self.page, "999", timeout=150)

    def test_headingless_terminal_states_ignore_description_and_recommendations(self):
        for text, expected in [("Application submitted", "applied"), ("No longer accepting applications", "closed")]:
            with self.subTest(text=text):
                self.page.set_content(f'<main><p>Engineer</p><span>{text}</span><h2>About the job</h2></main>')
                state, button = apply._wait_for_job_action(self.page, "123", timeout=500)
                self.assertEqual(state, expected)
                self.assertIsNone(button)
        self.page.set_content('<main><p>Engineer</p><button>Easy Apply</button><h2>About the job</h2><span>Application submitted</span></main>')
        self.assertEqual(apply._wait_for_job_action(self.page, "123", timeout=500)[0], "ready")

    def test_opener_retries_ignored_click_after_initialization(self):
        self.page.set_content('''<main><p>Data Engineer</p><button id="open">Easy Apply</button></main><dialog><button>Next</button></dialog>
            <script>
            window.opens = 0;
            document.querySelector('#open').addEventListener('click', () => {
                window.opens++;
                if (window.opens === 1) { setTimeout(() => window.hydrated = true, 100); return; }
                if (window.hydrated) document.querySelector('dialog').showModal();
            });
            </script>''')
        self.assertIsNotNone(apply._open_easy_apply(self.page, "123"))
        self.assertEqual(self.page.evaluate("window.opens"), 2)

    def test_opener_does_not_click_again_while_modal_renders(self):
        self.page.set_content('''<main><p>Data Engineer</p><button id="open">Easy Apply</button></main><dialog></dialog>
            <script>
            window.opens = 0;
            document.querySelector('#open').addEventListener('click', () => {
                window.opens++;
                document.querySelector('dialog').showModal();
                setTimeout(() => document.querySelector('dialog').innerHTML = '<button>Next</button>', 200);
            });
            </script>''')
        self.assertIsNotNone(apply._open_easy_apply(self.page, "123"))
        self.assertEqual(self.page.evaluate("window.opens"), 1)

    def test_opener_accepts_open_modal_after_click_timeout_without_reclick(self):
        button, modal = Mock(), Mock()
        button.click.side_effect = PlaywrightTimeoutError("Timed out after performing click action")
        page = Mock(url="https://example.test/jobs/view/123/")
        with patch.object(apply, "_wait_for_job_action", return_value=("ready", button)), \
                patch.object(apply, "_locate_modal", return_value=modal):
            self.assertIs(apply._open_easy_apply(page, "123"), modal)
        self.assertEqual(button.click.call_count, 1)

    def test_opener_reacquires_a_detached_button(self):
        from playwright.sync_api import Error
        stale, fresh, modal = Mock(), Mock(), Mock()
        stale.click.side_effect = Error("Element is not attached to the DOM")
        page = Mock(url="https://example.test/jobs/view/123/")
        page.evaluate.return_value = False
        with patch.object(apply, "_wait_for_job_action", side_effect=[("ready", stale), ("ready", fresh)]), \
                patch.object(apply, "_locate_modal", return_value=modal):
            self.assertIs(apply._open_easy_apply(page, "123"), modal)
        fresh.click.assert_called_once()
        self.assertEqual(stale.click.call_count, 1)

    def test_external_apply_returns_without_full_timeout(self):
        self.page.locator("#actions").evaluate("el => el.innerHTML = '<a href=\"https://example.test/apply\">Apply</a>'")
        started = time.monotonic()
        state, button = apply._wait_for_job_action(self.page, "123", timeout=2000)
        self.assertEqual(state, "external")
        self.assertIsNone(button)
        self.assertLess(time.monotonic() - started, 1)

    def test_recommendation_button_is_not_target_job_action(self):
        self.page.locator("main").evaluate("el => el.insertAdjacentHTML('beforeend', '<aside><button>Easy Apply</button></aside>')")
        with self.assertRaises(PlaywrightTimeoutError):
            apply._wait_for_job_action(self.page, "123", timeout=200)

    def test_waits_for_delayed_action_despite_unrelated_dom_changes(self):
        self.page.evaluate("""() => {
            setInterval(() => document.body.dataset.tick = Date.now(), 20);
            setTimeout(() => document.querySelector('#actions').innerHTML = '<button>Easy Apply</button>', 200);
        }""")
        state, button = apply._wait_for_job_action(self.page, "123", timeout=2000)
        self.assertEqual(state, "ready")
        self.assertEqual(button.inner_text(), "Easy Apply")

    def test_wrong_job_identity_does_not_return_button(self):
        self.page.locator("#actions").evaluate("el => el.innerHTML = '<button>Easy Apply</button>'")
        with self.assertRaises(PlaywrightTimeoutError):
            apply._wait_for_job_action(self.page, "999", timeout=200)

    def test_recommendation_applied_badge_is_ignored(self):
        self.page.locator("main").evaluate("el => el.insertAdjacentHTML('beforeend', '<aside><span>Applied</span></aside>')")
        self.page.locator("#actions").evaluate("el => el.innerHTML = '<button>Easy Apply</button>'")
        state, _ = apply._wait_for_job_action(self.page, "123", timeout=1000)
        self.assertEqual(state, "ready")

    def test_closed_and_already_applied_jobs_exit_early(self):
        for text, expected in [("No longer accepting applications", "closed"), ("You applied yesterday", "applied")]:
            with self.subTest(text=text):
                self.page.locator("#actions").evaluate("(el, text) => el.innerHTML = '<p>' + text + '</p>'", text)
                state, button = apply._wait_for_job_action(self.page, "123", timeout=1000)
                self.assertEqual(state, expected)
                self.assertIsNone(button)

    def test_no_action_is_timeout_not_external_classification(self):
        with self.assertRaises(PlaywrightTimeoutError):
            apply._wait_for_job_action(self.page, "123", timeout=200)

    def test_hidden_radio_uses_associated_visible_label(self):
        self.page.set_content('<fieldset><input style="display:none" type="radio" id="yes" value="yes"><label for="yes">Yes</label></fieldset>')
        group = self.page.query_selector("fieldset")
        started = time.monotonic()
        self.assertTrue(apply.select_radio_option(group, "Yes"))
        self.assertTrue(self.page.locator("#yes").is_checked())
        self.assertLess(time.monotonic() - started, 1)

    def test_step_waits_for_signature_change(self):
        self.page.set_content('<dialog open><p>Page one</p><button>Next</button></dialog>')
        before = apply._modal_signature(self.page)
        self.page.evaluate("setTimeout(() => document.querySelector('dialog p').textContent = 'Page two', 200)")
        self.assertTrue(apply._wait_for_step_change(self.page, before, timeout=1000))

    def test_unchanged_step_returns_false(self):
        self.page.set_content('<dialog open><button>Next</button></dialog>')
        self.assertFalse(apply._wait_for_step_change(self.page, apply._modal_signature(self.page), timeout=200))

    def test_modal_locator_rejects_action_outside_dialog(self):
        self.page.set_content('<main><form><button>Next</button></form></main>')
        self.assertIsNone(apply._locate_modal(self.page, timeout=150))

    def test_modal_locator_waits_for_enabled_action(self):
        self.page.set_content('<dialog open><button disabled>Next</button></dialog>')
        self.page.evaluate("setTimeout(() => document.querySelector('button').disabled = false, 200)")
        modal = apply._locate_modal(self.page, timeout=1000)
        self.assertTrue(modal.query_selector("button").is_enabled())

    def test_closed_dialog_does_not_mask_visible_signature(self):
        self.page.set_content('<dialog><p>Old page</p><button>Next</button></dialog><dialog open><p>Current page</p><button>Next</button></dialog>')
        self.assertIn("Current page", apply._modal_signature(self.page))
        self.assertNotIn("Old page", apply._modal_signature(self.page))

    def test_signature_detects_changes_beyond_first_200_characters(self):
        self.page.set_content('<dialog open><p>' + 'Long job title ' * 30 + '</p><p id="step">One</p><button>Next</button></dialog>')
        before = apply._modal_signature(self.page)
        self.page.locator("#step").evaluate("el => el.textContent = 'Two'")
        self.assertTrue(apply._wait_for_step_change(self.page, before, timeout=200))

    def test_stale_toast_is_not_submission_proof(self):
        self.page.set_content('<main><h1>Test engineer</h1></main><div role="status">Application sent</div><dialog open><button onclick="this.closest(\'dialog\').remove()">Submit application</button></dialog>')
        with patch.dict(humanize.config, {"confirmation_timeout_ms": 200}):
            self.assertFalse(apply._finalize_submit(self.page, self.page.query_selector("button"), {"title": "test", "job_id": "123"}))

    def test_changed_validation_message_is_not_progress(self):
        self.page.set_content('<dialog open><p id="error"></p><button>Next</button></dialog>')
        before = apply._modal_signature(self.page)
        self.page.locator("#error").evaluate("el => el.textContent = 'This field is required'")
        self.assertFalse(apply._wait_for_step_change(self.page, before, timeout=200))

    def test_transition_waits_through_intermediate_spinner(self):
        self.page.set_content('<dialog open><p>Page one</p><button>Next</button></dialog>')
        before = apply._modal_signature(self.page)
        self.page.evaluate("""() => {
            document.querySelector('dialog').innerHTML = '<p>Loading</p>';
            setTimeout(() => document.querySelector('dialog').innerHTML = '<p>Page two</p><button>Review</button>', 200);
        }""")
        self.assertTrue(apply._wait_for_step_change(self.page, before, timeout=1000))
        self.assertEqual(self.page.locator("dialog button").inner_text(), "Review")

    def test_different_job_card_inside_main_is_ignored(self):
        self.page.locator("main").evaluate("el => el.insertAdjacentHTML('beforeend', '<div data-job-id=\"999\"><button>Easy Apply</button></div>')")
        with self.assertRaises(PlaywrightTimeoutError):
            apply._wait_for_job_action(self.page, "123", timeout=200)

    def test_done_button_alone_is_not_submission_proof(self):
        self.page.set_content('<main><h1>Test engineer</h1></main><dialog open><button onclick="this.textContent=\'Done\'">Submit application</button></dialog>')
        with patch.dict(humanize.config, {"confirmation_timeout_ms": 200}):
            self.assertFalse(apply._finalize_submit(self.page, self.page.query_selector("button"), {"title": "test", "job_id": "123"}))

    def test_explicit_submitted_message_is_confirmed(self):
        self.page.set_content('<main><h1>Test engineer</h1></main><dialog open><p></p><button onclick="this.previousElementSibling.textContent=\'Application submitted\';this.textContent=\'Done\'">Submit application</button></dialog>')
        self.assertTrue(apply._finalize_submit(self.page, self.page.query_selector("button"), {"title": "test", "job_id": "123"}))

    def test_complete_multistep_application_on_local_fixture(self):
        html = """<main><p>Test engineer</p><button aria-label="Easy Apply to this job" onclick="applyDialog.showModal()">Easy Apply</button><h2>About the job</h2></main>
            <dialog><p>Contact info</p><button onclick="nextStep()">Next</button></dialog>
            <script>
                const applyDialog = document.querySelector('dialog');
                function nextStep() {
                    setTimeout(() => {
                        applyDialog.innerHTML = '<p>Resume selected</p><button onclick="reviewStep()">Review</button>';
                    }, 200);
                }
                function reviewStep() {
                    setTimeout(() => {
                        applyDialog.innerHTML = '<p>Review application</p><button onclick="submitStep()">Submit application</button>';
                    }, 200);
                }
                function submitStep() {
                    window.submissions = (window.submissions || 0) + 1;
                    applyDialog.innerHTML = '<p>Application submitted</p><button onclick="applyDialog.close()">Done</button>';
                }
            </script>"""
        self.page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=html))
        errors = []
        self.page.on("pageerror", lambda error: errors.append(str(error)))
        with patch.object(apply, "PHONE", ""), patch.object(apply, "EMAIL", ""), patch.object(apply, "RESUME_PATH", ""):
            status = apply.apply_easy_apply_job(self.page, {
                "job_id": "123", "title": "Test engineer", "link": "https://example.test/jobs/view/123/"
            })
        self.assertEqual(errors, [])
        self.assertEqual(status, "CONFIRMED")
        self.assertEqual(self.page.evaluate("window.submissions"), 1)
        self.assertFalse(self.page.locator("dialog").is_visible())

    def test_submit_without_positive_signal_is_unconfirmed(self):
        self.page.set_content('<main><h1>Test engineer</h1></main><dialog open><button onclick="this.closest(\'dialog\').remove()">Submit application</button></dialog>')
        with patch.dict(humanize.config, {"confirmation_timeout_ms": 200}):
            self.assertFalse(apply._finalize_submit(self.page, self.page.query_selector("button"), {"title": "test", "job_id": "123"}))


if __name__ == "__main__":
    unittest.main()
