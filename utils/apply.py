import json
import os

import yaml
from dotenv import load_dotenv

from utils.humanize import random_sleep, wait_for_page_full_load

load_dotenv()


RESUME_PATH: str = os.getenv("RESUME_PATH", "")
PHONE: str = os.getenv("PHONE", "")
EMAIL: str = os.getenv("EMAIL", "")

with open("config.yaml") as f:
    config = yaml.safe_load(f).get("job_search", {})

YEARS_OF_EXPERIENCE = config.get("years_of_experience", 5)
EDUCATION_LEVEL = config.get("education_level", "Bachelor's Degree")
WORK_AUTHORIZATION = config.get("work_authorization", "Yes")
REQUIRE_SPONSORSHIP = config.get("require_sponsorship", "No")
WILLING_TO_RELOCATE = config.get("willing_to_relocate", "Yes")
NOTICE_PERIOD = config.get("notice_period", "15")
CURRENT_SALARY = config.get("current_salary", "1000000")
SALARY_EXPECTATION = config.get("salary_expectation", "1500000")
PREVIOUSLY_WORKED = config.get("previously_worked", "No")
REFERENCE_CHECK = config.get("reference_check", "Yes")
CERTIFICATIONS = config.get("certifications", "None")


answered_questions: set[str] = set()
unanswered_questions: set[str] = set()


def save_questions_data():
    questions_file = "questions.json"
    data = {
        "answered_questions": list(answered_questions),
        "unanswered_questions": list(unanswered_questions),
    }

    # If file exists, read and update values
    if os.path.exists(questions_file):
        with open(questions_file, "r") as f:
            existing_data = json.load(f)

        # Merge the lists (avoid duplicates)
        existing_data["answered_questions"] = list(
            set(existing_data.get("answered_questions", []))
            | set(data["answered_questions"])
        )
        existing_data["unanswered_questions"] = list(
            set(existing_data.get("unanswered_questions", []))
            | set(data["unanswered_questions"])
        )

        # Save updated data
        with open(questions_file, "w") as f:
            json.dump(existing_data, f, indent=4)

    else:
        # Create new file
        with open(questions_file, "w") as f:
            json.dump(data, f, indent=4)


def apply_for_jobs(page, jobs, limit=config.get("max_jobs", 10)):
    total = min(len(jobs), limit)
    print(f"➡️ Starting to apply to {total} jobs (out of {len(jobs)})")

    applied_count = 0
    for idx, job in enumerate(jobs[:limit], start=1):
        title = job.get("title", "Unknown")
        company = job.get("company", "Unknown")
        print(f"[{idx}/{total}] Applying to: {title} at {company}")

        try:
            success = apply_easy_apply_job(page, job)
            print(f"[{idx}/{total}] {'✅ Applied' if success else '❌ Skipped/Failed'}")
            if success:
                applied_count += 1
        except Exception as e:
            print(f"[{idx}/{total}] Exception while applying: {e}")

        random_sleep()

    print(f"➡️ Done. Applied to {applied_count}/{total} jobs.")

    save_questions_data()
    return applied_count


def _find_easy_apply_button(page):
    """Find the Easy Apply button using multiple strategies for LinkedIn's current UI."""
    # Strategy 1: Playwright text selector (matches visible text)
    btn = page.locator("button:has-text('Easy Apply')").first
    try:
        if btn.is_visible(timeout=2000):
            return btn
    except Exception:
        pass

    # Strategy 2: aria-label based
    btn = page.locator("button[aria-label*='Easy Apply']")
    try:
        if btn.first.is_visible(timeout=1000):
            return btn.first
    except Exception:
        pass

    # Strategy 3: Use JS to find any button whose text contains 'Easy Apply'
    handle = page.evaluate_handle("""
        () => {
            const buttons = document.querySelectorAll('button');
            for (const btn of buttons) {
                if (btn.innerText && btn.innerText.includes('Easy Apply') && btn.offsetParent !== null) {
                    return btn;
                }
            }
            // Also check role="button" elements
            const roleBtns = document.querySelectorAll('[role="button"]');
            for (const btn of roleBtns) {
                if (btn.innerText && btn.innerText.includes('Easy Apply') && btn.offsetParent !== null) {
                    return btn;
                }
            }
            // Check for any clickable element with 'Easy Apply'
            const all = document.querySelectorAll('div, span, a');
            for (const el of all) {
                const text = el.innerText || '';
                if (text.trim() === 'Easy Apply' && el.offsetParent !== null) {
                    // Walk up to find the closest clickable ancestor
                    let parent = el;
                    for (let i = 0; i < 5; i++) {
                        parent = parent.parentElement;
                        if (!parent) break;
                        if (parent.tagName === 'BUTTON' || parent.tagName === 'A' || parent.getAttribute('role') === 'button') {
                            return parent;
                        }
                    }
                    return el;
                }
            }
            return null;
        }
    """)
    if handle and handle.as_element():
        return handle.as_element()

    return None


def _find_form_modal(page):
    """Find the Easy Apply form modal using multiple selectors."""
    selectors = [
        "div.jobs-easy-apply-modal",
        "div[data-test-modal-id='easy-apply-modal']",
        "div.artdeco-modal:has(button:has-text('Submit'))",
        "div.artdeco-modal:has(button:has-text('Next'))",
        "div[role='dialog']:has(button:has-text('Submit'))",
        "div[role='dialog']:has(button:has-text('Next'))",
        "div[role='dialog']:has(input[type='file'])",
        "div[role='dialog']:has(button:has-text('Review'))",
    ]
    for sel in selectors:
        try:
            modal = page.wait_for_selector(sel, timeout=5000)
            if modal:
                print(f"  Found modal with selector: {sel}")
                return modal
        except Exception:
            continue
    return None


def _find_form_modal_silent(page):
    """Re-fetch the modal without printing (used inside step loop)."""
    selectors = [
        "div.jobs-easy-apply-modal",
        "div[data-test-modal-id='easy-apply-modal']",
        "div.artdeco-modal:has(button:has-text('Submit'))",
        "div.artdeco-modal:has(button:has-text('Next'))",
        "div[role='dialog']:has(button:has-text('Submit'))",
        "div[role='dialog']:has(button:has-text('Next'))",
        "div[role='dialog']:has(input[type='file'])",
        "div[role='dialog']:has(button:has-text('Review'))",
    ]
    for sel in selectors:
        try:
            modal = page.wait_for_selector(sel, timeout=3000)
            if modal:
                return modal
        except Exception:
            continue
    return None


def apply_easy_apply_job(page, job):
    page.goto(job["link"])
    wait_for_page_full_load(page)
    random_sleep()

    try:
        # Check if already applied to this job
        already_applied = page.query_selector("span:has-text('Applied'), li-icon[type='success-pebble-icon']")
        if not already_applied:
            already_applied = page.evaluate("""() => {
                const spans = document.querySelectorAll('span');
                for (const s of spans) {
                    if (s.innerText && s.innerText.trim() === 'Applied') return true;
                }
                return false;
            }""")
        if already_applied:
            print("  ⏭️ Already applied to this job, skipping.")
            return False

        # Find the Easy Apply button
        easy_apply_btn = _find_easy_apply_button(page)

        if not easy_apply_btn:
            print("❌ Easy Apply button not found, skipping.")
            return False

        print("  Found Easy Apply button, clicking...")
        try:
            easy_apply_btn.click()
        except Exception:
            # Force click as fallback
            easy_apply_btn.click(force=True)
        print("  Clicked the Easy Apply button.")

        random_sleep()

        # Wait for form modal to appear
        form_modal = _find_form_modal(page)
        if not form_modal:
            print("⚠️ Form modal not found after clicking Easy Apply.")
            return False

        # Multi-step application handling
        max_steps = 10
        step_count = 0
        empty_next_count = 0  # Track consecutive Next clicks with no fields filled
        review_click_count = 0  # Track consecutive Review clicks to avoid infinite loop

        while step_count < max_steps:
            step_count += 1
            print(f"  Processing step {step_count}...")
            random_sleep()
            filled_something = False

            # Re-fetch the modal each step to avoid stale element handles
            form_modal = _find_form_modal_silent(page)
            if not form_modal:
                print("  ⚠️ Modal disappeared mid-application")
                break

            # Fill phone if present
            phone_selectors = [
                "input[name*='phone']",
                "input[id*='phone']",
                "input[id*='phoneNumber']",
                "input[inputmode='tel']",
                "input[type='tel']",
                "input[aria-label*='phone']",
                "input[placeholder*='phone']",
                "input[inputmode='text'][id*='phone']",
            ]

            phone_input = None
            for sel in phone_selectors:
                phone_input = form_modal.query_selector(sel)
                if phone_input:
                    break

            if phone_input and PHONE:
                try:
                    current_val = phone_input.input_value()
                    if not current_val:
                        phone_input.fill(PHONE)
                        print(f"    Filled phone: {PHONE}")
                        filled_something = True
                except Exception:
                    pass

            # Fill email if present
            email_input = form_modal.query_selector("input[name*='email'], input[type='email'], input[id*='email']")
            if email_input and EMAIL:
                try:
                    current_val = email_input.input_value()
                    if not current_val:
                        email_input.fill(EMAIL)
                        print(f"    Filled email: {EMAIL}")
                        filled_something = True
                except Exception:
                    pass

            # Handle file upload
            file_input = form_modal.query_selector("input[type='file']")
            if file_input and RESUME_PATH:
                try:
                    file_input.set_input_files(RESUME_PATH)
                    print(f"    Uploaded resume: {RESUME_PATH}")
                    filled_something = True
                except Exception as e:
                    print(f"    Error uploading resume: {e}")

            # Handle text inputs and textareas (for experience, notice period, etc.)
            text_inputs = form_modal.query_selector_all(
                "input[type='text'], input[type='number'], textarea"
            )
            for inp in text_inputs:
                try:
                    if not inp.is_visible():
                        continue
                    current_val = inp.input_value()
                    if current_val:
                        continue
                    label_text = get_label_for_input(page, inp)
                    answer = get_answer_for_question(label_text)
                    if answer:
                        answered_questions.add(label_text)
                        inp.fill(str(answer))
                        print(f"    Filled: {label_text} -> {answer}")
                        filled_something = True
                    else:
                        unanswered_questions.add(label_text)
                except Exception as e:
                    print(f"    Error filling input: {e}")

            random_sleep()

            # Handle radio buttons
            radio_groups = form_modal.query_selector_all(
                "fieldset, div[role='radiogroup']"
            )
            for group in radio_groups:
                try:
                    label_text = get_label_for_input(page, group)
                    answer = get_answer_for_question(label_text)
                    if answer:
                        if select_radio_option(group, str(answer)):
                            print(f"    Selected radio: {label_text} -> {answer}")
                            filled_something = True
                            continue
                    # Default to "Yes"
                    for fallback in ["Yes", "True"]:
                        if select_radio_option(group, fallback):
                            print(f"    Selected radio: {label_text} -> {fallback}")
                            filled_something = True
                            break
                    else:
                        unanswered_questions.add(label_text)
                except Exception as e:
                    print(f"    Error handling radio group: {e}")

            random_sleep()

            # Handle dropdowns/select elements
            selects = form_modal.query_selector_all("select")
            for sel in selects:
                try:
                    label_text = get_label_for_input(page, sel)
                    answer = get_answer_for_question(label_text)
                    if answer:
                        if select_dropdown_option(sel, str(answer)):
                            print(f"    Selected dropdown: {label_text} -> {answer}")
                            filled_something = True
                            continue
                    for fallback in ["Yes", "True"]:
                        if select_dropdown_option(sel, fallback):
                            print(f"    Selected dropdown: {label_text} -> {fallback}")
                            filled_something = True
                            break
                    else:
                        unanswered_questions.add(label_text)
                except Exception as e:
                    print(f"    Error handling dropdown: {e}")

            random_sleep()

            # Helper: find a button by multiple strategies (modal-level then page-level)
            def _find_btn(*text_patterns):
                for pattern in text_patterns:
                    try:
                        btn = form_modal.query_selector(f"button:has-text('{pattern}')")
                        if btn and btn.is_visible():
                            return btn
                    except Exception:
                        pass
                # Fallback: search from page level inside any visible dialog
                for pattern in text_patterns:
                    try:
                        btn = page.query_selector(f"div[role='dialog'] button:has-text('{pattern}')")
                        if btn and btn.is_visible():
                            return btn
                    except Exception:
                        pass
                # Fallback: aria-label
                for pattern in text_patterns:
                    try:
                        btn = page.query_selector(f"button[aria-label*='{pattern}']")
                        if btn and btn.is_visible():
                            return btn
                    except Exception:
                        pass
                return None

            # Detect submit button
            submit_btn = _find_btn('Submit application', 'Submit')
            if submit_btn:
                submit_btn.click()
                print(f"  ✅ Submitted application for {job.get('title', 'Unknown')}")
                random_sleep()
                # Dismiss any post-apply dialog
                try:
                    dismiss_btn = page.query_selector("button:has-text('Dismiss'), button[aria-label='Dismiss']")
                    if dismiss_btn and dismiss_btn.is_visible():
                        dismiss_btn.click()
                except Exception:
                    pass
                return True
            else:
                # Check for review button
                review_btn = _find_btn('Review')
                if review_btn:
                    review_click_count += 1
                    if review_click_count >= 2:
                        print("  ⚠️ Stuck in Review loop (2 consecutive Review clicks), aborting")
                        break
                    review_btn.click()
                    print("    Clicked Review button")
                    random_sleep()
                    # Immediately check for Submit after Review
                    form_modal = _find_form_modal_silent(page)
                    if form_modal:
                        submit_btn = _find_btn('Submit application', 'Submit')
                        if submit_btn:
                            submit_btn.click()
                            print(f"  ✅ Submitted application for {job.get('title', 'Unknown')}")
                            random_sleep()
                            try:
                                dismiss_btn = page.query_selector("button:has-text('Dismiss'), button[aria-label='Dismiss']")
                                if dismiss_btn and dismiss_btn.is_visible():
                                    dismiss_btn.click()
                            except Exception:
                                pass
                            return True
                    continue

                # Check for "Next" button to continue multi-step form
                next_btn = _find_btn('Next', 'Continue to next step')
                if next_btn:
                    if not filled_something:
                        empty_next_count += 1
                        if empty_next_count >= 3:
                            print("  ⚠️ Stuck in Next loop (3 empty steps), aborting")
                            break
                    else:
                        empty_next_count = 0
                    review_click_count = 0  # Reset review counter when Next succeeds
                    next_btn.click()
                    print("    Clicked Next button")
                    random_sleep()
                    continue

                # Debug: log what buttons ARE visible in the modal
                try:
                    all_btns = page.evaluate("""() => {
                        const dlg = document.querySelector("div[role='dialog']");
                        if (!dlg) return 'no dialog found';
                        const btns = dlg.querySelectorAll('button');
                        return Array.from(btns).filter(b => b.offsetParent !== null).map(b => b.innerText.trim().substring(0, 50)).join(' | ');
                    }""")
                    print(f"  🔍 Visible buttons in dialog: {all_btns}")
                except Exception:
                    pass

                # No actionable button found
                print("  ⚠️ No Next, Review, or Submit button found")
                random_sleep()
                break

        print("  ⚠️ Max steps reached or unable to complete application")
        # Try to close the modal to avoid blocking next job
        try:
            close_btn = page.query_selector("button[aria-label='Dismiss'], button:has-text('Discard')")
            if close_btn and close_btn.is_visible():
                close_btn.click()
                random_sleep()
                # Confirm discard if prompted
                discard_btn = page.query_selector("button:has-text('Discard')")
                if discard_btn and discard_btn.is_visible():
                    discard_btn.click()
        except Exception:
            pass
        return False

    except Exception as e:
        print(f"  ⚠️ Error applying to job: {e}")
        # Try to close modal
        try:
            close_btn = page.query_selector("button[aria-label='Dismiss'], button:has-text('Discard')")
            if close_btn and close_btn.is_visible():
                close_btn.click()
                random_sleep()
                discard_btn = page.query_selector("button:has-text('Discard')")
                if discard_btn and discard_btn.is_visible():
                    discard_btn.click()
        except Exception:
            pass
        return False


def get_label_for_input(page, element):
    """Extract label text for an input element"""
    try:
        # Try to find associated label
        elem_id = element.get_attribute("id")
        if elem_id:
            label = page.query_selector(f"label[for='{elem_id}']")
            if label:
                return label.inner_text().strip()

        # Try parent label
        parent = element.evaluate("el => el.closest('label')")
        if parent:
            return element.evaluate("el => el.closest('label').innerText").strip()

        # Try aria-label
        aria_label = element.get_attribute("aria-label")
        if aria_label:
            return aria_label.strip()

        # Try placeholder
        placeholder = element.get_attribute("placeholder")
        if placeholder:
            return placeholder.strip()

        # Try fieldset legend (for radio groups)
        legend = element.query_selector("legend")
        if legend:
            return legend.inner_text().strip()

        return ""
    except Exception as e:
        print(f"⚠️ Error getting label for input: {e}")
        return ""


def get_answer_for_question(question_text):
    """Match question text to appropriate answer"""
    if not question_text:
        return None

    q_lower = question_text.lower()

    # Years of experience
    if any(
        kw in q_lower
        for kw in ["years of experience", "years experience", "how many years"]
    ):
        # if any(
        #     tech in q_lower
        #     for tech in ["python", "javascript", "java", "react", "node"]
        # ):
        #     return YEARS_OF_EXPERIENCE.get("specific_skill", "5")

        return YEARS_OF_EXPERIENCE

    # Education
    if any(
        kw in q_lower
        for kw in ["education", "degree", "qualification", "highest level"]
    ):
        return EDUCATION_LEVEL

    # Work authorization
    if any(
        kw in q_lower
        for kw in [
            "authorized to work",
            "legally authorized",
            "work authorization",
            "right to work",
        ]
    ):
        return WORK_AUTHORIZATION

    # Visa sponsorship
    if any(
        kw in q_lower
        for kw in ["visa sponsorship", "require sponsorship", "need sponsorship"]
    ):
        return REQUIRE_SPONSORSHIP

    # Location/onsite
    if any(
        kw in q_lower
        for kw in [
            "comfortable working",
            "willing to work",
            "work onsite",
            "relocate",
            "work in",
        ]
    ):
        return WILLING_TO_RELOCATE

    # Notice period
    if any(
        kw in q_lower
        for kw in [
            "notice period",
            "availability",
            "when can you start",
            "start date",
            "join",
        ]
    ):
        if "month" in q_lower:
            return str(round(int(NOTICE_PERIOD) / 30, 2))

        return NOTICE_PERIOD

    # Current Salary
    if any(
        kw in q_lower for kw in ["current fixed ctc", "current ctc", "expected salary"]
    ):
        return CURRENT_SALARY

    # Salary expectations
    if any(
        kw in q_lower
        for kw in [
            "salary",
            "compensation",
            "expected salary",
            "expected ctc",
            "salary expectation",
        ]
    ):
        return SALARY_EXPECTATION

    # Previous employment
    if any(
        kw in q_lower for kw in ["previously worked", "worked for", "former employee"]
    ):
        return PREVIOUSLY_WORKED

    # Reference check
    if any(kw in q_lower for kw in ["contact", "reference", "previous employer"]):
        return REFERENCE_CHECK

    # Certifications
    if any(kw in q_lower for kw in ["certification", "certified", "certificate"]):
        return CERTIFICATIONS

    return None


def select_radio_option(group_element, answer):
    """Select appropriate radio button based on answer"""
    try:
        answer_lower = str(answer).lower()

        # Map answers to common radio button patterns
        if answer_lower in ["yes", "true"]:
            radio = group_element.query_selector(
                "input[type='radio'][value='Yes'], input[type='radio'][value='yes'], input[type='radio'][value='true']"
            )
            if radio and radio.is_visible():
                try:
                    radio.click()
                except Exception:
                    radio.click(force=True)
                return True

        if answer_lower in ["no", "false"]:
            radio = group_element.query_selector(
                "input[type='radio'][value='No'], input[type='radio'][value='no'], input[type='radio'][value='false']"
            )
            if radio and radio.is_visible():
                try:
                    radio.click()
                except Exception:
                    radio.click(force=True)
                return True

        # Try to find radio with matching label
        labels = group_element.query_selector_all("label")
        for label in labels:
            if answer_lower in label.inner_text().lower():
                radio_id = label.get_attribute("for")
                if radio_id:
                    # Use attribute selector to avoid CSS special char issues in IDs
                    radio = group_element.query_selector(
                        f"input[type='radio'][id='{radio_id}']"
                    )
                    if radio and radio.is_visible():
                        try:
                            radio.click()
                        except Exception:
                            radio.click(force=True)
                        return True
                # Fallback: click the label itself
                try:
                    label.click()
                    return True
                except Exception:
                    pass
        return False
    except Exception as e:
        print(f"Error selecting radio: {e}")


def select_dropdown_option(select_element, answer):
    """Select appropriate dropdown option based on answer"""
    try:
        answer_str = str(answer)
        options = select_element.query_selector_all("option")
        for option in options:
            option_text = option.inner_text().strip()
            if (
                answer_str.lower() in option_text.lower()
                or option_text.lower() in answer_str.lower()
            ):
                select_element.select_option(value=option.get_attribute("value"))
                return True
        return False
    except Exception as e:
        print(f"Error selecting dropdown: {e}")
