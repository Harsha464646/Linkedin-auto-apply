import csv
import json
import os
import re
import time
from datetime import datetime

import yaml
from dotenv import load_dotenv

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from utils.humanize import bounded_click, config as timing_config, navigate_page, random_sleep, timed_action
from utils.knowledge import KnowledgeBase
from utils.monitoring import reporter

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

# Self-learning store: remembers which answers actually led to a confirmed
# submission so repeat questions are answered instantly and correctly.
KB = KnowledgeBase(defaults=config)


def _sponsorship_answer(question):
    required = str(REQUIRE_SPONSORSHIP).strip().lower()
    if required not in {"yes", "no"}:
        return None
    q = " ".join(question.lower().replace("\u2019", "'").split())
    if re.search(r"\b(uncomfortable|unwilling|unable|not comfortable|not willing|not able)\b", q):
        return None
    without = re.search(r"\bwithout (?:\w+ ){0,3}sponsor(?:ship)?\b", q)
    negated_need = re.search(r"\b(?:not|don't)\s+(?:need|require)\b", q)
    if without or negated_need:
        return "Yes" if required == "no" else "No"
    if re.search(r"\b(?:need|require|requiring)\b.{0,120}\bsponsor", q) or "sponsorship required" in q:
        return "Yes" if required == "yes" else "No"
    return None


def _experience_count_question(question):
    q = (question or "").lower().strip()
    return bool(
        re.search(r"\b(years?|yrs?|months?|mos?)\b", q)
        and re.search(r"\b(experience|worked|working)\b", q)
        and not re.match(r"(do|does|did|are|is|have|has|can|will|would)\b", q)
    )


def _experience_profile_answer(question, kind=None, options=None):
    value = float(YEARS_OF_EXPERIENCE)
    if re.search(r"\b(months?|mos?)\b", question, re.I) and not re.search(r"\b(years?|yrs?)\b", question, re.I):
        value *= 12
    if not options:
        return None if kind in {"select", "radio"} else f"{value:g}"
    number = r"[0-9]+(?:\.[0-9]+)?"
    ranges = []
    for option in options:
        text = re.sub(r"\b(years?|yrs?|months?|mos?)\b", "", str(option).lower()).strip()
        text = text.replace("\u2013", "-").replace("\u2014", "-")
        if re.fullmatch(number, text) and float(text) == value:
            return option
        interval = re.fullmatch(rf"({number})\s*(?:-|to)\s*({number})", text)
        lower_bound = re.fullmatch(rf"({number})\s*\+", text)
        if interval and float(interval[1]) <= value <= float(interval[2]):
            ranges.append(option)
        elif lower_bound and value >= float(lower_bound[1]):
            ranges.append(option)
    return ranges[0] if len(ranges) == 1 else None


def resolve_answer(question, kind=None, options=None):
    """Decide how to answer a form question.

    Sponsorship and experience counts use explicit profile rules, not learned guesses.
    Other questions try a learned answer, config rules, then
    a fuzzy match against learned questions (wording differs between
    postings), then inference from the widget itself. Returns
    ``(answer, source)`` where source is used for logging.
    """
    if not question:
        return None, "none"
    if "sponsor" in question.lower():
        answer = _sponsorship_answer(question)
        return answer, "profile" if answer is not None else "none"
    if _experience_count_question(question):
        answer = _experience_profile_answer(question, kind=kind, options=options)
        return answer, "profile" if answer is not None else "none"

    learned = KB.lookup(question)
    if learned is not None:
        return learned, "learned"

    rule = get_answer_for_question(question)
    if rule is not None:
        return rule, "rule"

    fuzzy = KB.fuzzy_lookup(question)
    if fuzzy is not None:
        return fuzzy, "learned~"

    inferred = KB.infer(question, kind=kind, options=options)
    if inferred is not None:
        return inferred, "inferred"

    return None, "none"


def _short(text, limit=90):
    """Trim a label for logging — some fieldsets (the resume picker) expose
    hundreds of characters of option text as their 'question'."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t if len(t) <= limit else t[:limit] + "…"


def _radio_group_answered(group_element):
    """True when the radio group already has a selected option."""
    try:
        return bool(
            group_element.query_selector("[role='radio'][aria-checked='true']")
            or group_element.query_selector("input[type='radio']:checked")
        )
    except Exception:
        return False


def _radio_options_text(group_element):
    """Visible labels of the options in a radio group."""
    try:
        opts = group_element.query_selector_all("[role='radio']") or \
            group_element.query_selector_all("input[type='radio']")
        texts = []
        for opt in opts:
            t = _radio_option_text(group_element, opt)
            if t and t not in texts:
                texts.append(t)
        return texts
    except Exception:
        return []


def _select_options_text(select_element):
    """Visible labels of the selectable (non-placeholder) options."""
    try:
        texts = []
        for opt in select_element.query_selector_all("option"):
            if opt.get_attribute("disabled") is not None:
                continue
            if not opt.evaluate("el => el.value"):
                continue
            t = (opt.inner_text() or "").strip()
            if t and t not in texts:
                texts.append(t)
        return texts
    except Exception:
        return []


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


APPLICATION_LOG = "application_log.csv"

# Human-readable meaning of each status the applier can return.
STATUS_LABELS = {
    "CONFIRMED": "✅ Confirmed sent",
    "SUBMIT_UNCONFIRMED": "⚠️ Submitted but NOT confirmed",
    "INCOMPLETE": "❌ Could not finish (unanswered questions)",
    "NO_EASY_APPLY": "⏭️ No Easy Apply button (external listing)",
    "NO_MODAL": "❌ Application form did not open",
    "ALREADY_APPLIED": "⏭️ Already applied",
    "ERROR": "❌ Error while applying",
    "CLOSED": "Job no longer accepting applications",
}


def log_application(job, status, reason=""):
    """Append a row to application_log.csv so the user can track every job the
    bot touched and its outcome (confirmed / failed / skipped / etc.)."""
    try:
        file_exists = os.path.exists(APPLICATION_LOG)
        with open(APPLICATION_LOG, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(
                    ["timestamp", "status", "status_detail", "title",
                     "company", "job_id", "reason", "link"]
                )
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                status,
                STATUS_LABELS.get(status, status),
                job.get("title", "Unknown"),
                job.get("company", "Unknown"),
                job.get("job_id", ""),
                reason,
                job.get("link", ""),
            ])
        return True
    except Exception as e:
        print(f"  ⚠️ Could not write to application log: {e}")
        return False


def apply_for_jobs(page, jobs, limit=config.get("max_jobs", 10), target_confirmed=None, on_processed=None):
    if target_confirmed is not None and target_confirmed <= 0:
        return 0
    total = min(len(jobs), limit)
    print(f"➡️ Starting to apply to {total} jobs (out of {len(jobs)})")

    applied_count = 0
    processed_count = 0
    for idx, job in enumerate(jobs[:limit], start=1):
        title = job.get("title", "Unknown")
        company = job.get("company", "Unknown")
        print(f"[{idx}/{total}] Applying to: {title} at {company}")
        reporter.update(phase="applying", current_job=job, activity=f"Processing job {idx}/{total}")

        status = "ERROR"
        reason = ""
        try:
            with timed_action(f"job {job.get('job_id', 'unknown')} total"):
                status = apply_easy_apply_job(page, job)
        except Exception as e:
            status = "ERROR"
            reason = str(e)
            reporter.error(e, "application")
            print(f"[{idx}/{total}] Exception while applying: {e}")

        # Promote the answers used here to confirmed knowledge only when the
        # application actually went through.
        KB.commit(success=(status == "CONFIRMED"))

        persisted = log_application(job, status, reason)
        reporter.record_application(job, status, persisted=persisted)
        processed_count += 1
        reporter.update(activity=f"Job result: {status}")
        print(f"[{idx}/{total}] {STATUS_LABELS.get(status, status)}")
        if status == "CONFIRMED":
            applied_count += 1
        if on_processed:
            on_processed(job, status)

        random_sleep()
        if target_confirmed is not None and applied_count >= target_confirmed:
            break

    print(f"➡️ Done. Confirmed {applied_count}/{processed_count} processed jobs in this batch.")
    print(f"📄 Full history logged to {APPLICATION_LOG}")

    confirmed, known, unknown = KB.stats()
    print(f"🧠 Knowledge: {confirmed} confirmed answers ({known} tracked), "
          f"{unknown} question(s) still unanswered → learned_answers.json")

    save_questions_data()
    return applied_count


# Strict Easy Apply button finder.
#
# The job-details page also renders a "More jobs for you" sidebar whose cards
# carry an "Easy Apply" *badge*. Those cards are <a> links pointing at a
# DIFFERENT job. A loose "any element containing the text 'Easy Apply'" search
# matches those badges, walks up to the card's <a>, and clicking it navigates
# away to another job — which produced NO_MODAL and "Execution context was
# destroyed" failures. So we only accept a real button whose OWN label is
# exactly "Easy Apply" (or whose aria-label starts with it), and never an <a>.
_EASY_APPLY_JS = """(root, targetJobId, boundary) => {
    const visible = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
    for (const el of root.querySelectorAll("button, [role='button']")) {
        if (!visible(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
        if (boundary && !(el.compareDocumentPosition(boundary) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
        if (el.closest("aside, [data-view-name='job-card'], li[data-occludable-job-id], .jobs-similar-jobs")) continue;
        const owner = el.closest('[data-job-id]');
        if (owner && targetJobId && owner.getAttribute('data-job-id') !== targetJobId) continue;
        // Reject anything nested inside a link (i.e. a job recommendation card).
        if (el.closest('a[href]')) continue;
        const txt = (el.innerText || '').replace(/\\s+/g, ' ').trim();
        const aria = (el.getAttribute('aria-label') || '').trim();
        if (/^easy apply$/i.test(txt) || /^easy apply\\b/i.test(aria)) return el;
    }
    return null;
}"""


_JOB_ACTION_JS = """targetJobId => {
    const urlId = (location.href.match(/\\/jobs\\/view\\/(\\d+)/) || [])[1]
        || new URL(location.href).searchParams.get('currentJobId');
    if (targetJobId && urlId !== targetJobId) return false;
    const main = document.querySelector('main');
    if (!main) return false;
    const visible = el => !!el && el.getClientRects().length > 0
        && getComputedStyle(el).visibility !== 'hidden';
    const boundary = Array.from(main.querySelectorAll('h2, h3, [role="heading"]'))
        .find(el => visible(el) && /^(about the job|more jobs|similar jobs|recommended jobs)\\b/i.test(el.innerText.trim()));
    const unrelated = el => {
        const owner = el.closest('[data-job-id]');
        return el.closest("aside, [data-view-name='job-card'], li[data-occludable-job-id], .jobs-similar-jobs")
            || (owner && targetJobId && owner.getAttribute('data-job-id') !== targetJobId)
            || (boundary && !(el.compareDocumentPosition(boundary) & Node.DOCUMENT_POSITION_FOLLOWING));
    };
    const heading = Array.from(main.querySelectorAll('h1, [role="heading"][aria-level="1"], .job-details-jobs-unified-top-card__job-title'))
        .find(el => visible(el) && !unrelated(el));
    const headingLink = heading && (heading.closest('a[href]') || heading.querySelector('a[href]'));
    const headingId = headingLink && (headingLink.href.match(/\\/jobs\\/view\\/(\\d+)/) || [])[1];
    if (targetJobId && headingId && headingId !== targetJobId) return false;
    for (let root = heading ? heading.parentElement : main; root && main.contains(root); root = root.parentElement) {
        const notices = Array.from(root.querySelectorAll('p, span'))
            .filter(el => visible(el) && !unrelated(el))
            .map(el => (el.innerText || '').replace(/\\s+/g, ' ').trim());
        if (notices.some(text => /^(no longer accepting applications|this job is no longer available)/i.test(text)))
            return {status: 'closed', button: null};
        if (notices.some(text => /^(applied(?:$| on | \\d)|you applied\\b|application submitted\\b)/i.test(text)))
            return {status: 'applied', button: null};
        const easy = (""" + _EASY_APPLY_JS + """)(root, targetJobId, boundary);
        if (easy) return {status: 'ready', button: easy};
        const external = Array.from(root.querySelectorAll('a[href], button, [role="button"]'))
            .find(el => visible(el) && !unrelated(el) && !el.disabled
                && el.getAttribute('aria-disabled') !== 'true'
                && /^(apply|apply now|apply on company website)$/i.test((el.innerText || '').trim()));
        if (external) return {status: 'external', button: null};
    }
    return false;
}"""


def _wait_for_job_action(page, target_job_id, timeout=None):
    with timed_action("job action readiness"):
        state = page.wait_for_function(
            _JOB_ACTION_JS, arg=target_job_id, polling=150,
            timeout=timeout or timing_config.get("readiness_timeout_ms", 10000),
        )
        try:
            status = state.get_property("status")
            button = state.get_property("button")
            try:
                return status.json_value(), button.as_element()
            finally:
                status.dispose()
                if not button.as_element():
                    button.dispose()
        finally:
            state.dispose()


def _find_easy_apply_button(page, timeout=10000):
    """Find the real Easy Apply button on the job details page.

    The button renders asynchronously, so poll until it appears rather than
    relying on a single fixed sleep. Returns None when the listing has no Easy
    Apply option (e.g. an external "Apply" link or an already-applied job).
    """
    try:
        _, button = _wait_for_job_action(page, _job_id_from_url(page.url), timeout=timeout)
        return button
    except PlaywrightTimeoutError:
        return None


def _job_id_from_url(url):
    """Extract the LinkedIn job id from a job URL, if present."""
    for pattern in (r"/jobs/view/(\d+)", r"currentJobId=(\d+)"):
        m = re.search(pattern, url or "")
        if m:
            return m.group(1)
    return None


# Locate the Easy Apply modal in ONE DOM pass.
#
# This used to be a list of 11 CSS selectors each fed to wait_for_selector()
# with its own timeout. Because wait_for_selector BLOCKS for the full timeout
# when a selector does not match, every call paid for the misses before the
# hit: ~6s on entry, ~4s on every form step, and a full ~33s when no modal
# existed at all. That was the dominant source of the bot's lag.
#
# Instead we evaluate all candidates in priority order inside a single JS call
# and poll it, so a present modal is returned in ~ms and an absent one costs
# only the (short) overall timeout.
#
# ``strict`` matters: an Easy Apply <dialog> is inserted into the DOM before
# its contents render, so accepting any open dialog immediately hands back an
# empty modal ("no Next/Review/Submit button found"). While polling we only
# accept a modal that already has an action button; the looser fallbacks are
# allowed just once, after the deadline.
_LOCATE_MODAL_JS = """(strict) => {
    const visible = el => el && (el.offsetParent !== null || el.getClientRects().length > 0);
    const action = /^(Submit application|Submit|Review|Next|Continue to next step)$/i;
    const hasAction = el => Array.from(el.querySelectorAll('button'))
        .some(b => visible(b) && !b.disabled && b.getAttribute('aria-disabled') !== 'true'
            && action.test((b.innerText || '').trim()));
    const ready = el => hasAction(el);

    const dialogs = Array.from(document.querySelectorAll('dialog'));
    // 1) An open <dialog> carrying an Easy Apply action button (new SDUI).
    for (const d of dialogs) {
        if (!d.hasAttribute('open') || !visible(d)) continue;
        if (ready(d)) return d;
    }
    // 2) Legacy role=dialog modal with an action button.
    for (const d of document.querySelectorAll("div[role='dialog'], div.jobs-easy-apply-modal, div[data-test-modal-id='easy-apply-modal']")) {
        if (visible(d) && ready(d)) return d;
    }
    if (strict) return null;

    // 3) Any open/visible dialog at all.
    for (const d of dialogs) {
        if (d.hasAttribute('open') && visible(d)) return d;
    }
    // 4) Last resort: climb from a visible action button to its container.
    const btn = Array.from(document.querySelectorAll('button'))
        .find(b => visible(b) && action.test((b.innerText || '').trim()));
    if (btn) {
        let el = btn;
        for (let i = 0; i < 15 && el; i++) {
            if (el.tagName === 'DIALOG' || el.getAttribute('role') === 'dialog') return el;
            el = el.parentElement;
        }
        return null;
    }
    return null;
}"""


def _locate_modal(page, timeout=4000, verbose=False):
    """Find the Easy Apply modal (new SDUI <dialog> or legacy markup).

    Polls a single JS probe so a fully-rendered modal returns almost
    immediately, falling back to looser matching only once time runs out.
    """
    deadline = time.monotonic() + timeout / 1000
    while True:
        expired = time.monotonic() >= deadline
        try:
            handle = page.evaluate_handle(_LOCATE_MODAL_JS, not expired)
            if handle and handle.as_element():
                if verbose:
                    print("  Found Easy Apply modal.")
                return handle.as_element()
            handle.dispose()
        except Exception:
            pass
        if expired:
            return None
        time.sleep(0.15)


def _find_form_modal(page):
    """Find the Easy Apply form modal right after clicking Easy Apply."""
    with timed_action("application modal readiness"):
        return _locate_modal(page, timeout=8000, verbose=True)


def _find_form_modal_silent(page):
    """Re-fetch the modal without printing (used inside step loop)."""
    return _locate_modal(page, timeout=2500, verbose=False)


# Snapshot of the modal used to tell "the form advanced to the next page" from
# "the Next click did nothing". Easy Apply forms are often several pages long
# with everything pre-filled from the profile (contact info, resume, work
# history, education), so "we filled no fields this step" is NOT evidence of
# being stuck — only an unchanged modal is.
_MODAL_SIGNATURE_JS = """() => {
    const d = Array.from(document.querySelectorAll('dialog[open], div[role="dialog"]'))
        .find(el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden');
    if (!d) return '';
    const text = (d.innerText || '').replace(/\\s+/g, ' ');
    const progress = (text.match(/\\d+\\s*\\/\\s*\\d+\\s*pages?/i) || [''])[0];
    const fields = Array.from(d.querySelectorAll('input, select, textarea, fieldset'))
        .map(e => e.id || e.getAttribute('name') || e.tagName).join(',');
    return progress + '||' + text + '||' + fields;
}"""


def _modal_signature(page):
    try:
        return page.evaluate(_MODAL_SIGNATURE_JS)
    except Exception:
        return ""


def _wait_for_step_change(page, before, timeout=None):
    with timed_action("form transition"):
        try:
            result = page.wait_for_function(
                """before => {
                    const signature = (""" + _MODAL_SIGNATURE_JS + """)();
                    const dialog = Array.from(document.querySelectorAll('dialog[open], div[role="dialog"]'))
                        .find(el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden');
                    if (dialog && /this field is required|please enter|please select|enter a valid/i.test(dialog.innerText))
                        return 'blocked';
                    const ready = dialog && Array.from(dialog.querySelectorAll('button')).some(
                        el => el.getClientRects().length > 0 && !el.disabled && el.getAttribute('aria-disabled') !== 'true'
                            && /^(next|continue to next step|review|submit|submit application)$/i.test(el.innerText.trim())
                    );
                    if (ready && signature && signature !== before) return 'advanced';
                    return false;
                }""",
                arg=before, polling=100,
                timeout=timeout or timing_config.get("step_timeout_ms", 4000),
            )
            try:
                return result.json_value() == "advanced"
            finally:
                result.dispose()
        except PlaywrightTimeoutError:
            return False


def _finalize_submit(page, submit_btn, job):
    """Click the Submit button and VERIFY the application was actually sent.

    Requires an explicit sent message or the target job's applied state.
    A Done button or disappearing dialog alone does not establish success.
    """
    title = job.get("title", "Unknown")
    try:
        bounded_click(submit_btn, label="Submit application")
    except Exception as e:
        print(f"  Submit click did not complete for {title}: {type(e).__name__}")
        return False

    # Poll for a confirmation state.
    reporter.update(activity="Checking submission confirmation")
    confirmed = False
    deadline = time.monotonic() + timing_config.get("confirmation_timeout_ms", 5000) / 1000
    started = time.monotonic()
    while time.monotonic() < deadline:
        try:
            state = page.evaluate("""targetJobId => {
                const urlId = (location.href.match(/\\/jobs\\/view\\/(\\d+)/) || [])[1]
                    || new URL(location.href).searchParams.get('currentJobId');
                if (targetJobId && targetJobId !== urlId) return {};
                // Confirmation must come from an explicit signal. The original
                // check matched the word "premium" against document.body --
                // LinkedIn renders "Try Premium" on every page, so it reported
                // EVERY submit as confirmed. Conversely, scoping only to the
                // dialog misses the case where LinkedIn swaps in a post-apply
                // upsell dialog; require dialog text or the target job's state.
                const visible = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
                const dlg = Array.from(document.querySelectorAll('dialog[open], div[role="dialog"]')).find(visible);
                const dlgText = (dlg && dlg.innerText) || '';
                const scope = dlgText.toLowerCase();

                const sent = /your application was sent|application was sent|application sent|application submitted/.test(scope);
                // The job page also flips to an "Applied ..." state once the
                // submission lands.
                const jobState = (""" + _JOB_ACTION_JS + """)(targetJobId);
                const applied = jobState && jobState.status === 'applied';

                let doneBtn = false, hasErr = false, stillApplying = false;
                if (dlg) {
                    const btns = Array.from(dlg.querySelectorAll('button')).filter(b => b.offsetParent !== null);
                    doneBtn = btns.some(b => /^done$/i.test((b.innerText || '').trim()));
                    hasErr = /this field is required|please enter|please select|enter a valid/i.test(dlgText);
                    // A Submit/Next still on screen means the form did not advance.
                    stillApplying = btns.some(b => /^(submit application|submit|next|review)$/i
                        .test((b.innerText || '').trim()));
                }
                return {sent, applied, doneBtn, hasErr, stillApplying, dialogOpen: !!dlg};
            }""", str(job.get("job_id") or _job_id_from_url(page.url) or ""))
        except Exception:
            break
        if state.get("hasErr"):
            print(f"  ⚠️ Submit blocked by validation errors for {title} (unanswered required field).")
            break
        if not state.get("stillApplying") and (state.get("sent") or state.get("applied")):
            confirmed = True
            break
        # The form closing without errors is not proof LinkedIn accepted it.
        # Wait for an explicit confirmation rather than trusting disappearance.
        page.wait_for_timeout(100)
    if timing_config.get("timing_logs", True):
        print(f"  [timing] submission confirmation: {time.monotonic() - started:.2f}s ({'confirmed' if confirmed else 'unconfirmed'})")

    # Dismiss the confirmation / any leftover dialog.
    try:
        dismiss = page.query_selector(
            "button:has-text('Done'), button[aria-label='Dismiss'], button:has-text('Dismiss')"
        )
        if confirmed and dismiss and dismiss.is_visible():
            bounded_click(dismiss, label="dismiss confirmation")
    except Exception:
        pass

    if confirmed:
        print(f"  ✅ Application CONFIRMED sent for {title}")
    else:
        print(f"  ⚠️ Clicked Submit but could NOT confirm submission for {title}")
    return confirmed


def _open_easy_apply(page, target_job_id):
    """Allow page initialization, then retry only an ignored or detached opener once."""
    with timed_action("application opener"):
        try:
            page.wait_for_load_state("load", timeout=5000)
        except PlaywrightTimeoutError:
            pass
        for attempt in range(2):
            if attempt and page.evaluate("""() => Array.from(document.querySelectorAll('dialog[open], div[role="dialog"]'))
                    .some(el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden')"""):
                return _find_form_modal(page)
            state, button = _wait_for_job_action(page, target_job_id, timeout=3000)
            if state != "ready" or not button:
                return None
            try:
                bounded_click(button, label="Easy Apply" if not attempt else "Easy Apply retry")
            except PlaywrightTimeoutError:
                modal = _locate_modal(page, timeout=1500, verbose=True)
                if modal:
                    return modal
                raise
            except PlaywrightError as error:
                if not attempt and "not attached" in str(error).lower():
                    continue
                raise
            landed_job_id = _job_id_from_url(page.url)
            if target_job_id and landed_job_id and landed_job_id != target_job_id:
                return None
            modal = _locate_modal(page, timeout=2500 if not attempt else 8000, verbose=True)
            if modal:
                return modal
        return None


def apply_easy_apply_job(page, job):
    target_job_id = str(job.get("job_id") or _job_id_from_url(job.get("link", "")) or "")
    navigate_page(page, job["link"], label="job navigation")

    try:
        # Check if already applied to this job
        status, easy_apply_btn = _wait_for_job_action(page, target_job_id)
        if status == "applied":
            print("  ⏭️ Already applied to this job, skipping.")
            return "ALREADY_APPLIED"

        # Find the Easy Apply button
        if status == "closed":
            print("  Job no longer accepting applications, skipping.")
            return "CLOSED"
        if status == "external":
            print("  External Apply link detected, skipping.")
            return "NO_EASY_APPLY"
        if not easy_apply_btn:
            return "ERROR"

        print("  Found Easy Apply button, clicking...")
        # Re-query and retry the opener as fallback; never retry Submit.
        form_modal = _open_easy_apply(page, target_job_id)

        # Guard: clicking must NOT have navigated us to a different job. If it
        # did, the click landed on the wrong element and continuing would both
        # fail and risk acting on someone else's listing.
        landed_job_id = _job_id_from_url(page.url)
        if target_job_id and landed_job_id and landed_job_id != target_job_id:
            print(f"  ⚠️ Click navigated away to job {landed_job_id} (expected {target_job_id}); aborting.")
            return "NO_MODAL"

        # Wait for form modal to appear
        if not form_modal:
            print("⚠️ Form modal not found after clicking Easy Apply.")
            return "NO_MODAL"

        # Multi-step application handling (Easy Apply forms run up to ~5 pages
        # plus a review page, so allow headroom).
        max_steps = 15
        step_count = 0
        empty_next_count = 0  # Track consecutive Next clicks with no fields filled
        review_click_count = 0  # Track consecutive Review clicks to avoid infinite loop

        while step_count < max_steps:
            step_count += 1
            print(f"  Processing step {step_count}...")
            reporter.update(activity=f"Filling form step {step_count}")
            step_started = time.monotonic()
            filled_something = False
            profile_blocked = False

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
                        phone_input.fill(PHONE, timeout=timing_config.get("action_timeout_ms", 3000))
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
                        email_input.fill(EMAIL, timeout=timing_config.get("action_timeout_ms", 3000))
                        print(f"    Filled email: {EMAIL}")
                        filled_something = True
                except Exception:
                    pass

            # Handle file upload
            file_input = form_modal.query_selector("input[type='file']")
            if file_input and RESUME_PATH:
                try:
                    file_input.set_input_files(RESUME_PATH, timeout=timing_config.get("readiness_timeout_ms", 10000))
                    print(f"    Uploaded resume: {RESUME_PATH}")
                    filled_something = True
                except Exception as e:
                    print(f"    Error uploading resume: {e}")

            # Handle text inputs and textareas (for experience, notice period, etc.)
            text_inputs = form_modal.query_selector_all(
                "input[type='text'], input[type='number'], textarea"
            )
            for inp in text_inputs:
                experience_field = False
                try:
                    if not inp.is_visible():
                        continue
                    current_val = inp.input_value()
                    label_text = get_label_for_input(page, inp)
                    experience_field = _experience_count_question(label_text)
                    if current_val and not experience_field:
                        continue
                    kind = inp.get_attribute("type") or "text"
                    answer, source = resolve_answer(label_text, kind=kind)
                    if experience_field and answer is None:
                        profile_blocked = True
                    if answer is not None and current_val.strip() == str(answer):
                        continue
                    if answer:
                        answered_questions.add(label_text)
                        inp.fill(str(answer), timeout=timing_config.get("action_timeout_ms", 3000))
                        print(f"    Filled [{source}]: {label_text} -> {answer}")
                        KB.record_used(label_text, answer, kind=kind, source=source)
                        filled_something = True
                    else:
                        unanswered_questions.add(label_text)
                        KB.record_unanswered(label_text, kind=kind)
                        print(f"    ❓ No answer for: {label_text}")
                except Exception as e:
                    profile_blocked |= experience_field
                    print(f"    Error filling input: {e}")

            # Handle radio buttons
            radio_groups = form_modal.query_selector_all(
                "fieldset, div[role='radiogroup']"
            )
            for group in radio_groups:
                experience_field = False
                try:
                    # Already answered (e.g. LinkedIn pre-selects the most
                    # recent resume) — preserve choices except experience counts.
                    label_text = get_label_for_input(page, group)
                    experience_field = _experience_count_question(label_text)
                    if _radio_group_answered(group) and not experience_field:
                        continue
                    options = _radio_options_text(group)
                    answer, source = resolve_answer(
                        label_text, kind="radio", options=options
                    )
                    if answer and select_radio_option(group, str(answer)):
                        print(f"    Selected radio [{source}]: {_short(label_text)} -> {answer}")
                        KB.record_used(label_text, answer, kind="radio",
                                       options=options, source=source)
                        filled_something = True
                        continue
                    profile_blocked |= experience_field
                    unanswered_questions.add(label_text)
                    KB.record_unanswered(label_text, kind="radio", options=options)
                    print(f"    ❓ No answer for radio: {_short(label_text)} "
                          f"(options: {options[:6]})")
                except Exception as e:
                    profile_blocked |= experience_field
                    print(f"    Error handling radio group: {e}")

            # Handle dropdowns/select elements
            selects = form_modal.query_selector_all("select")
            for sel in selects:
                experience_field = False
                try:
                    # Leave non-experience pre-populated dropdowns alone (LinkedIn
                    # pre-fills things like phone country code and email).
                    label_text = get_label_for_input(page, sel)
                    experience_field = _experience_count_question(label_text)
                    if (sel.input_value() or "").strip() and not experience_field:
                        continue
                    options = _select_options_text(sel)
                    answer, source = resolve_answer(
                        label_text, kind="select", options=options
                    )
                    if answer and select_dropdown_option(sel, str(answer)):
                        print(f"    Selected dropdown [{source}]: {label_text} -> {answer}")
                        KB.record_used(label_text, answer, kind="select",
                                       options=options, source=source)
                        filled_something = True
                        continue
                    profile_blocked |= experience_field
                    unanswered_questions.add(label_text)
                    KB.record_unanswered(label_text, kind="select", options=options)
                    print(f"    ❓ No answer for dropdown: {label_text} (options: {options[:8]})")
                except Exception as e:
                    profile_blocked |= experience_field
                    print(f"    Error handling dropdown: {e}")

            if profile_blocked:
                print("    Could not apply the configured experience value; leaving this application incomplete.")
                break
            if timing_config.get("timing_logs", True):
                print(f"  [timing] form {step_count} fields: {time.monotonic() - step_started:.2f}s")
            if filled_something:
                random_sleep(0.1, 0.3)

            # Helper: find a button by multiple strategies (modal-level then page-level)
            def _find_btn(*text_patterns):
                for pattern in text_patterns:
                    try:
                        for btn in form_modal.query_selector_all(f"button:has-text('{pattern}')"):
                            if btn.is_visible() and btn.is_enabled() and btn.get_attribute("aria-disabled") != "true":
                                return btn
                    except Exception:
                        pass
                # Fallback: search from page level inside any visible dialog
                # (covers both new <dialog> and legacy div[role='dialog'])
                for pattern in text_patterns:
                    for container in ("dialog[open]", "div[role='dialog']"):
                        try:
                            for btn in page.query_selector_all(f"{container} button:has-text('{pattern}')"):
                                if btn.is_visible() and btn.is_enabled() and btn.get_attribute("aria-disabled") != "true":
                                    return btn
                        except Exception:
                            pass
                # Fallback: aria-label
                for pattern in text_patterns:
                    try:
                        for btn in form_modal.query_selector_all(f"button[aria-label*='{pattern}']"):
                            if btn.is_visible() and btn.is_enabled() and btn.get_attribute("aria-disabled") != "true":
                                return btn
                    except Exception:
                        pass
                return None

            # Detect submit button
            submit_btn = _find_btn('Submit application', 'Submit')
            if submit_btn:
                return "CONFIRMED" if _finalize_submit(page, submit_btn, job) else "SUBMIT_UNCONFIRMED"
            else:
                # Check for review button
                review_btn = _find_btn('Review')
                if review_btn:
                    review_click_count += 1
                    if review_click_count >= 2:
                        print("  ⚠️ Stuck in Review loop (2 consecutive Review clicks), aborting")
                        break
                    sig_before = _modal_signature(page)
                    bounded_click(review_btn, label="Review")
                    print("    Clicked Review button")
                    if _wait_for_step_change(page, sig_before):
                        review_click_count = 0
                    # Immediately check for Submit after Review
                    form_modal = _find_form_modal_silent(page)
                    if form_modal:
                        submit_btn = _find_btn('Submit application', 'Submit')
                        if submit_btn:
                            return "CONFIRMED" if _finalize_submit(page, submit_btn, job) else "SUBMIT_UNCONFIRMED"
                    continue

                # Check for "Next" button to continue multi-step form
                next_btn = _find_btn('Next', 'Continue to next step')
                if next_btn:
                    review_click_count = 0  # Reset review counter when Next succeeds
                    sig_before = _modal_signature(page)
                    bounded_click(next_btn, label="Next")
                    print("    Clicked Next button")
                    advanced = _wait_for_step_change(page, sig_before)
                    # Advancing a page counts as progress even when nothing
                    # needed filling; only an unchanged modal means we're stuck
                    # (typically a required field we could not answer).
                    if not advanced:
                        empty_next_count += 1
                        if empty_next_count >= 2:
                            print("  ⚠️ Stuck on the same form page after Next, aborting")
                            break
                    else:
                        empty_next_count = 0
                    continue

                # Debug: log what buttons ARE visible in the modal
                try:
                    all_btns = page.evaluate("""() => {
                        const dlg = document.querySelector("dialog[open]") || document.querySelector("dialog") || document.querySelector("div[role='dialog']");
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
                bounded_click(close_btn, label="close application")
                random_sleep()
                # Confirm discard if prompted
                discard_btn = page.query_selector("button:has-text('Discard')")
                if discard_btn and discard_btn.is_visible():
                    bounded_click(discard_btn, label="discard application")
        except Exception:
            pass
        return "INCOMPLETE"

    except Exception as e:
        reporter.error(e, "application")
        print(f"  ⚠️ Error applying to job: {e}")
        # Try to close modal
        try:
            close_btn = page.query_selector("button[aria-label='Dismiss'], button:has-text('Discard')")
            if close_btn and close_btn.is_visible():
                bounded_click(close_btn, label="close application")
                random_sleep()
                discard_btn = page.query_selector("button:has-text('Discard')")
                if discard_btn and discard_btn.is_visible():
                    bounded_click(discard_btn, label="discard application")
        except Exception:
            pass
        return "ERROR"


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
            legend_text = legend.inner_text().strip()
            if legend_text:
                return legend_text

        # New SDUI radio groups: <fieldset role="radiogroup"> has an EMPTY
        # legend; the question text lives in the fieldset (or its parent)
        # alongside the option texts. Extract it by stripping the option
        # labels and any validation error text.
        try:
            tag = element.evaluate("el => el.tagName")
        except Exception:
            tag = ""
        role_attr = element.get_attribute("role") or ""
        if tag == "FIELDSET" or role_attr == "radiogroup":
            question = element.evaluate("""el => {
                const opts = Array.from(el.querySelectorAll("[role='radio'], label, option"))
                    .map(o => (o.innerText || '').trim())
                    .filter(Boolean);
                const container = el.parentElement || el;
                let txt = (container.innerText || '').replace(/\\s+/g, ' ').trim();
                for (const o of opts) {
                    if (o) txt = txt.split(o).join(' ');
                }
                txt = txt.replace(/This field is required/ig, '')
                         .replace(/\\s+/g, ' ')
                         .trim();
                return txt;
            }""")
            if question:
                return question.strip()

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
        kw in q_lower for kw in ["current fixed ctc", "current ctc", "current salary"]
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


def _click_element(el):
    """Click a visible, enabled control with a bounded fallback."""
    try:
        if not el.is_visible() or not el.is_enabled():
            return False
        try:
            bounded_click(el, label="radio option")
        except PlaywrightTimeoutError:
            bounded_click(el, label="radio fallback", timeout=1000, force=True)
        return True
    except Exception:
        return False


def _select_radio_target(opt):
    if not opt.is_enabled():
        return False
    if opt.is_visible():
        return _click_element(opt)
    label = opt.evaluate_handle("""el => Array.from(el.labels || []).find(
        label => label.getClientRects().length > 0 && getComputedStyle(label).visibility !== 'hidden'
    ) || null""")
    try:
        return bool(label.as_element() and _click_element(label.as_element()))
    finally:
        label.dispose()


def _radio_option_text(group_element, opt):
    """Get the visible text for a radio option (new SDUI or legacy)."""
    # New SDUI: the role=radio wrapper's innerText holds the option label
    try:
        txt = (opt.inner_text() or "").strip()
        if txt:
            return txt
    except Exception:
        pass
    # Legacy: associated <label for="id">
    try:
        oid = opt.get_attribute("id")
        if oid:
            lbl = group_element.query_selector(f"label[for='{oid}']")
            if lbl:
                t = (lbl.inner_text() or "").strip()
                if t:
                    return t
    except Exception:
        pass
    try:
        return (opt.get_attribute("value") or "").strip()
    except Exception:
        return ""


def select_radio_option(group_element, answer):
    """Select a radio option, supporting both the new SDUI (role='radio'
    wrappers with option text in a <p>, inputs without a value attribute) and
    the legacy input[type='radio'] + <label> markup."""
    try:
        answer_lower = str(answer).lower().strip()

        # Prefer the new SDUI role="radio" wrappers; fall back to raw inputs.
        options = group_element.query_selector_all("[role='radio']")
        if not options:
            options = group_element.query_selector_all("input[type='radio']")

        if not options:
            return False

        texts = [_radio_option_text(group_element, opt).lower() for opt in options]
        attempted = set()
        # 1) Exact match on visible option text
        for index, opt in enumerate(options):
            if texts[index] == answer_lower:
                attempted.add(index)
                if _select_radio_target(opt):
                    return True

        # 2) Substring match (either direction)
        for index, opt in enumerate(options):
            t = texts[index]
            if index not in attempted and t and (answer_lower in t or t in answer_lower):
                if _select_radio_target(opt):
                    return True

        return False
    except Exception as e:
        print(f"Error selecting radio: {e}")
        return False


def select_dropdown_option(select_element, answer):
    """Select a dropdown option matching the answer.

    Placeholder options ("Select an option") are disabled and often have empty
    text/value. They must be skipped explicitly: an empty option text makes the
    substring test `option_text in answer` trivially true, which used to select
    the disabled placeholder and stall for the full 30s Playwright timeout.
    """
    try:
        answer_lower = str(answer).lower().strip()
        if not answer_lower:
            return False

        candidates = []
        for option in select_element.query_selector_all("option"):
            try:
                if option.get_attribute("disabled") is not None:
                    continue
                text = (option.inner_text() or "").strip()
                value = option.evaluate("el => el.value") or ""
                if not text or not value:
                    continue  # placeholder
                candidates.append((text, value))
            except Exception:
                continue

        def _pick(value):
            # Bound the wait so a stubborn option can never stall the run.
            select_element.select_option(value=value, timeout=5000)
            return True

        # 1) Exact match on option text
        for text, value in candidates:
            if text.lower() == answer_lower:
                return _pick(value)
        # 2) Substring match (both texts non-empty by construction)
        for text, value in candidates:
            if answer_lower in text.lower() or text.lower() in answer_lower:
                return _pick(value)
        return False
    except Exception as e:
        print(f"Error selecting dropdown: {e}")
