"""Persistent, self-learning answer store for Easy Apply questions.

The bot meets the same screening questions over and over ("How many years of
experience do you have with X?", "Are you authorized to work in the US?").
Rather than relying only on the hand-written keyword rules in ``apply.py``,
this module remembers every question it has seen and which answer actually led
to a confirmed application, so later runs answer instantly and correctly.

Resolution order used by ``apply.py``:

1. ``lookup`` — exact match on a previously *confirmed* answer.
2. config keyword rules (``get_answer_for_question``).
3. ``fuzzy_lookup`` — near-match against learned questions (wording varies
   slightly between postings, e.g. the skill name changes).
4. ``infer`` — reason from the widget itself (numeric field, Yes/No options).

Answers are only promoted to "confirmed" once the application they were used
in was actually submitted successfully, so a wrong guess never hardens into a
permanent bad answer.
"""

import json
import os
import re
from datetime import datetime
from difflib import SequenceMatcher

KNOWLEDGE_FILE = "learned_answers.json"

# Questions whose safe answer is "No" rather than the optimistic default.
_NEGATIVE_INTENT = (
    "sponsorship", "sponsor", "visa status", "h-1b", "h1b",
    "convicted", "felony", "criminal", "disciplinary action",
    "do you need", "will you require", "do you require",
)

_YES_NO = {"yes", "no"}


def normalize(text):
    """Collapse a question to a stable lookup key."""
    t = (text or "").lower().strip()
    t = t.replace("*", " ")
    t = re.sub(r"this field is required", " ", t)
    t = re.sub(r"[^a-z0-9+#. ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _looks_numeric(question, kind, options):
    if kind == "number":
        return True
    if options:
        return False
    return bool(re.search(r"how many|number of|years of|yrs", question or "", re.I))


class KnowledgeBase:
    """Load/persist learned answers and reason about unseen questions."""

    def __init__(self, defaults=None, path=KNOWLEDGE_FILE):
        self.path = path
        self.defaults = defaults or {}
        self.answers = {}
        self.unanswered = {}
        self._pending = []  # answers used in the in-flight application
        self._dirty = False
        self._load()

    # ---------------------------------------------------------------- storage
    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.answers = data.get("answers", {})
            self.unanswered = data.get("unanswered", {})
        except Exception as e:
            print(f"  ⚠️ Could not read {self.path}: {e}")

    def save(self):
        if not self._dirty:
            return
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(
                    {"answers": self.answers, "unanswered": self.unanswered},
                    f, indent=2, ensure_ascii=False, sort_keys=True,
                )
            self._dirty = False
        except Exception as e:
            print(f"  ⚠️ Could not write {self.path}: {e}")

    # ---------------------------------------------------------------- lookups
    def lookup(self, question):
        """Exact match on a confirmed learned answer."""
        rec = self.answers.get(normalize(question))
        if rec and rec.get("confirmed", 0) > 0:
            return rec.get("answer")
        return None

    def fuzzy_lookup(self, question, cutoff=0.86):
        """Nearest confirmed answer for a differently-worded question.

        Handles the common case where only the skill name changes between two
        otherwise identical postings.
        """
        key = normalize(question)
        if not key or len(key) < 12:
            return None
        best, best_score = None, 0.0
        for cand, rec in self.answers.items():
            if not rec.get("confirmed", 0):
                continue
            score = SequenceMatcher(None, key, cand).ratio()
            if score > best_score:
                best, best_score = rec, score
        if best and best_score >= cutoff:
            return best.get("answer")
        return None

    def infer(self, question, kind=None, options=None):
        """Reason an answer out of the widget when nothing is known yet."""
        q = (question or "").lower()
        opts = [o for o in (options or []) if o]

        # Yes/No widgets: answer "No" only for questions where yes is risky.
        if opts and {o.lower() for o in opts} <= _YES_NO | {"true", "false"}:
            negative = any(k in q for k in _NEGATIVE_INTENT)
            want = "No" if negative else "Yes"
            for o in opts:
                if o.lower() == want.lower():
                    return o
            return opts[0]

        # Numeric screening questions default to the configured experience.
        if _looks_numeric(q, kind, opts):
            return str(self.defaults.get("years_of_experience", 5))

        # A dropdown with a single real choice is unambiguous.
        if len(opts) == 1:
            return opts[0]
        return None

    # --------------------------------------------------------------- learning
    def record_used(self, question, answer, kind=None, options=None, source="rule"):
        """Note an answer used in the current application (not yet trusted)."""
        if not question or answer in (None, ""):
            return
        self._pending.append({
            "key": normalize(question), "question": question.strip(),
            "answer": str(answer), "kind": kind,
            "options": list(options or []), "source": source,
        })

    def record_unanswered(self, question, kind=None, options=None):
        """Track a question we could not answer so the user can teach it."""
        key = normalize(question)
        if not key:
            return
        rec = self.unanswered.setdefault(
            key, {"question": (question or "").strip(), "kind": kind,
                  "options": list(options or []), "seen": 0}
        )
        rec["seen"] += 1
        rec["kind"] = kind or rec.get("kind")
        if options:
            rec["options"] = list(options)
        self._dirty = True

    def commit(self, success):
        """Resolve the in-flight application's answers.

        On success the answers are promoted to confirmed knowledge; otherwise
        they are only counted as attempts so a bad guess never hardens.
        """
        for item in self._pending:
            rec = self.answers.setdefault(
                item["key"],
                {"question": item["question"], "answer": item["answer"],
                 "kind": item["kind"], "options": item["options"],
                 "confirmed": 0, "used": 0, "source": item["source"]},
            )
            rec["used"] = rec.get("used", 0) + 1
            if success:
                # A confirmed submission is ground truth for this answer.
                rec["answer"] = item["answer"]
                rec["kind"] = item["kind"] or rec.get("kind")
                rec["options"] = item["options"] or rec.get("options")
                rec["source"] = item["source"]
                rec["confirmed"] = rec.get("confirmed", 0) + 1
                rec["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.unanswered.pop(item["key"], None)
            self._dirty = True
        self._pending.clear()
        self.save()

    def stats(self):
        confirmed = sum(1 for r in self.answers.values() if r.get("confirmed", 0))
        return confirmed, len(self.answers), len(self.unanswered)
