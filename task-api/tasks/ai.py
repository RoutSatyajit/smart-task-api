"""
The "AI" layer of the API, kept behind two small, swappable functions.

    parse_task_text(text: str, today: datetime.date) -> dict
        Returns {"title": str, "description": str, "due_date": "YYYY-MM-DD" | None,
                 "priority": "low" | "medium" | "high", "recurrence": str | None}

    break_down_task(task) -> list[dict]
        Returns a list of subtask dicts with the same keys as above.

Version 1 of both is rule-based (datetime + string methods + regex). An LLM
version only has to keep the same signature and return the same dict shape.
The views NEVER trust the output: every dict is passed through TaskSerializer
before anything is saved.

This module does not import Django at the top, so the rule-based functions can
be unit-tested and debugged on their own.
"""
import calendar
import json
import re
import urllib.request
from datetime import date, timedelta


class ParserError(Exception):
    """The parser could not produce a result (-> 502)."""


class LLMNotConfiguredError(Exception):
    """LLM parser selected but LLM_API_KEY is missing (-> 503)."""


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

LEADING_PHRASES = [
    r"remind me to",
    r"remind me",
    r"please",
    r"i need to",
    r"i have to",
    r"i must",
    r"don'?t forget to",
    r"remember to",
    r"todo:?",
    r"task:?",
]

RECURRENCE_PATTERNS = [
    (r"\b(?:of\s+)?every\s+day\b|\bdaily\b|\beveryday\b", "daily"),
    (r"\b(?:of\s+)?every\s+week\b|\bweekly\b", "weekly"),
    (r"\b(?:of\s+)?every\s+month\b|\bmonthly\b", "monthly"),
    (r"\b(?:of\s+)?every\s+year\b|\byearly\b|\bannually\b", "yearly"),
]

HIGH_PRIORITY_WORDS = r"\b(urgent|urgently|asap|important|critical|immediately)\b"
LOW_PRIORITY_WORDS = r"\b(low priority|whenever|someday|no rush)\b"


# --------------------------------------------------------------------------
# Date helpers
# --------------------------------------------------------------------------
def next_day_of_month(today, day):
    """Next date (today or later) whose day-of-month is `day`. Skips months
    that don't have that day (e.g. the 31st skips April)."""
    if not 1 <= day <= 31:
        return None
    year, month = today.year, today.month
    for _ in range(14):
        if day <= calendar.monthrange(year, month)[1]:
            candidate = date(year, month, day)
            if candidate >= today:
                return candidate
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return None


def next_weekday(today, weekday_index):
    """Next given weekday after today (never today itself).
    "next friday" and "friday" both mean the coming Friday."""
    days_ahead = (weekday_index - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


def _remove(pattern, text):
    return re.sub(pattern, " ", text, flags=re.IGNORECASE)


def _clean_title(text):
    text = re.sub(r"\s+", " ", text).strip()
    # Drop dangling connector words left behind after removing date phrases.
    for _ in range(3):
        text = re.sub(r"^(?:to|on|by|the|at|of|for|and)\s+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+(?:on|by|the|at|of|for|before|due|and|in)$", "", text, flags=re.IGNORECASE)
    text = text.strip(" ,.;:!-")
    return text[:1].upper() + text[1:] if text else ""


# --------------------------------------------------------------------------
# parse_task_text
# --------------------------------------------------------------------------
def parse_task_text(text, today):
    """
    Rule-based natural-language parser.

    >>> parse_task_text("Remind me to pay rent on the 1st of every month", date(2026, 11, 27))
    {'title': 'Pay rent', 'description': 'Remind me to pay rent on the 1st of every month',
     'due_date': '2026-12-01', 'priority': 'medium', 'recurrence': 'monthly'}
    """
    if not isinstance(text, str):
        raise ParserError("text must be a string")

    original = text.strip()
    work = original
    due = None
    recurrence = None
    priority = "medium"

    # 1. Strip leading filler ("remind me to ...").
    for phrase in LEADING_PHRASES:
        work = re.sub(rf"^\s*{phrase}\s+", "", work, flags=re.IGNORECASE)

    # 2. Priority keywords.
    if re.search(HIGH_PRIORITY_WORDS, work, re.IGNORECASE):
        priority = "high"
        work = _remove(HIGH_PRIORITY_WORDS, work)
    elif re.search(LOW_PRIORITY_WORDS, work, re.IGNORECASE):
        priority = "low"
        work = _remove(LOW_PRIORITY_WORDS, work)

    # 3. Recurrence ("every month", "weekly", ...).
    for pattern, value in RECURRENCE_PATTERNS:
        if re.search(pattern, work, re.IGNORECASE):
            recurrence = value
            work = _remove(pattern, work)
            break

    # 4. Explicit ISO date: 2026-12-05 (validity is checked later by the serializer).
    match = re.search(r"\b(?:on\s+|by\s+|due\s+)?(\d{4}-\d{2}-\d{2})\b", work, re.IGNORECASE)
    if match:
        due = match.group(1)
        work = work.replace(match.group(0), " ")

    # 5. Relative days.
    if due is None:
        relative = [
            (r"\b(?:by\s+|on\s+)?(?:the\s+)?day after tomorrow\b", 2),
            (r"\b(?:by\s+|on\s+)?tomorrow\b", 1),
            (r"\b(?:by\s+|on\s+)?today\b|\btonight\b", 0),
            (r"\b(?:by\s+)?next week\b", 7),
        ]
        for pattern, days in relative:
            if re.search(pattern, work, re.IGNORECASE):
                due = (today + timedelta(days=days)).isoformat()
                work = _remove(pattern, work)
                break

    # 6. "in 3 days" / "in 2 weeks".
    if due is None:
        match = re.search(r"\bin\s+(\d{1,3})\s+(day|days|week|weeks)\b", work, re.IGNORECASE)
        if match:
            n = int(match.group(1))
            days = n * 7 if match.group(2).startswith("week") else n
            due = (today + timedelta(days=days)).isoformat()
            work = work.replace(match.group(0), " ")

    # 7. Weekday names: "on friday", "next monday", "every tuesday".
    if due is None:
        day_names = "|".join(WEEKDAYS)
        match = re.search(rf"\b(on\s+|next\s+|this\s+|every\s+)?({day_names})s?\b", work, re.IGNORECASE)
        if match:
            prefix = (match.group(1) or "").strip().lower()
            due = next_weekday(today, WEEKDAYS.index(match.group(2).lower())).isoformat()
            if prefix == "every" and recurrence is None:
                recurrence = "weekly"
            work = work.replace(match.group(0), " ")

    # 8. Day of month: "on the 1st", "15th of the month".
    if due is None:
        match = re.search(
            r"\b(?:on\s+|by\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b(?:\s+of\s+(?:the\s+|each\s+)?month)?",
            work,
            re.IGNORECASE,
        )
        if match:
            day_date = next_day_of_month(today, int(match.group(1)))
            if day_date:
                due = day_date.isoformat()
            work = work.replace(match.group(0), " ")

    # 9. Times are not stored; strip "at 5pm" / "at 17:30" from the title.
    work = _remove(r"\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b", work)

    title = _clean_title(work)

    return {
        "title": title,
        "description": original,
        "due_date": due,
        "priority": priority,
        "recurrence": recurrence,
    }


# --------------------------------------------------------------------------
# break_down_task
# --------------------------------------------------------------------------
SPLIT_PATTERN = r"\s*(?:,|;|\band then\b|\bthen\b|\band\b|&)\s*"

TEMPLATE_STEPS = ["Plan: {t}", "Do: {t}", "Review: {t}"]


def _split_parts(text):
    if not text:
        return []
    parts = [p.strip(" .") for p in re.split(SPLIT_PATTERN, text, flags=re.IGNORECASE)]
    return [p for p in parts if p]


def break_down_task(task):
    """
    Rule-based breakdown. Splits the title on commas / "and" / "then";
    if that gives one part, tries the description; otherwise uses a
    Plan / Do / Review template.

    `task` only needs .title, .description, .priority and .due_date.
    """
    parts = _split_parts(task.title)
    if len(parts) < 2:
        parts = _split_parts(task.description)
    if len(parts) < 2:
        parts = [step.format(t=task.title) for step in TEMPLATE_STEPS]

    due = task.due_date.isoformat() if hasattr(task.due_date, "isoformat") else task.due_date
    return [
        {
            "title": part[:1].upper() + part[1:],
            "description": f"Subtask of: {task.title}",
            "priority": task.priority,
            "due_date": due,
            "completed": False,
        }
        for part in parts
    ]


# --------------------------------------------------------------------------
# Optional LLM wrapper (same signature as parse_task_text)
# --------------------------------------------------------------------------
LLM_PROMPT = (
    "Convert the user's task into JSON with exactly these keys: "
    '"title" (short string), "description" (string), '
    '"due_date" ("YYYY-MM-DD" or null), "priority" ("low"|"medium"|"high"), '
    '"recurrence" ("daily"|"weekly"|"monthly"|"yearly" or null). '
    "Today is {today}. Reply with JSON only."
)


def llm_parse_task_text(text, today):
    """Calls an OpenAI-compatible chat completions API. Output is still
    validated by TaskSerializer in the view, exactly like user input."""
    from django.conf import settings

    api_key = getattr(settings, "LLM_API_KEY", None)
    if not api_key:
        raise LLMNotConfiguredError("LLM_API_KEY is not set")

    payload = {
        "model": settings.LLM_MODEL,
        "messages": [
            {"role": "system", "content": LLM_PROMPT.format(today=today.isoformat())},
            {"role": "user", "content": text},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    req = urllib.request.Request(
        settings.LLM_API_URL,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            content = json.loads(resp.read())["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as exc:  # network error, bad JSON, unexpected shape
        raise ParserError(f"LLM call failed: {exc}") from exc


def get_task_parser():
    """Pick the parser from settings.TASK_PARSER ("rule" or "llm")."""
    from django.conf import settings

    if getattr(settings, "TASK_PARSER", "rule") == "llm":
        return llm_parse_task_text
    return parse_task_text
