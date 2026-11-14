"""
Validation helpers — enforce all field-level rules from the schema.
The API accepts anything; it is our job to reject bad data before PATCH.
"""
import re
from datetime import date


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LINKEDIN_RE = re.compile(r"^https?://(www\.)?linkedin\.com/", re.I)
GITHUB_RE = re.compile(r"^https?://(www\.)?github\.com/", re.I)

VALID_DEGREES = {"B.Tech", "B.E.", "B.Sc", "BCA", "MBA Tech", "MCA", "M.Tech", "Other"}
VALID_LOCATIONS = {"Mumbai", "Bengaluru", "Remote", "No preference"}
VALID_DURATIONS = {2, 3, 6}
GRAD_YEAR_RANGE = range(2026, 2029)   # 2026-2028 inclusive
AVAIL_FROM_MIN = date(2026, 1, 1)
AVAIL_FROM_MAX = date(2027, 12, 31)


def validate_email(v: str) -> tuple[bool, str]:
    if not EMAIL_RE.match(v.strip()):
        return False, "That doesn't look like a valid email address. Could you double-check it?"
    return True, ""


def validate_cgpa(v) -> tuple[bool, str, float | None]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False, "CGPA should be a number between 0.0 and 10.0.", None
    if not (0.0 <= f <= 10.0):
        return False, f"CGPA must be between 0.0 and 10.0 (you said {f}). Is it perhaps on a 10-point scale?", None
    return True, "", round(f, 2)


def validate_degree(v: str) -> tuple[bool, str]:
    if v.strip() in VALID_DEGREES:
        return True, ""
    opts = ", ".join(sorted(VALID_DEGREES))
    return False, f"I need one of: {opts}. Which best describes your degree?"


def validate_graduation_year(v) -> tuple[bool, str, int | None]:
    try:
        yr = int(v)
    except (TypeError, ValueError):
        return False, "Please give me the graduation year as a 4-digit number (e.g. 2026).", None
    if yr not in GRAD_YEAR_RANGE:
        return False, f"Graduation year must be between 2026 and 2028 (got {yr}).", None
    return True, "", yr


def validate_preferred_location(v: str) -> tuple[bool, str]:
    for loc in VALID_LOCATIONS:
        if loc.lower() == v.strip().lower():
            return True, ""
    opts = ", ".join(VALID_LOCATIONS)
    return False, f"Please choose one of: {opts}."


def validate_available_from(v: str) -> tuple[bool, str, str | None]:
    try:
        d = date.fromisoformat(v.strip())
    except ValueError:
        # Try to parse common formats
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%B %d, %Y", "%d %B %Y"):
            try:
                from datetime import datetime
                d = datetime.strptime(v.strip(), fmt).date()
                break
            except ValueError:
                continue
        else:
            return False, "Please give the date in YYYY-MM-DD format, e.g. 2026-06-01.", None
    if not (AVAIL_FROM_MIN <= d <= AVAIL_FROM_MAX):
        return False, f"The date must be between 2026-01-01 and 2027-12-31 (got {d}).", None
    return True, "", d.isoformat()


def validate_internship_duration(v) -> tuple[bool, str, int | None]:
    try:
        months = int(v)
    except (TypeError, ValueError):
        return False, "Duration should be 2, 3, or 6 months.", None
    if months not in VALID_DURATIONS:
        return False, f"I can only record 2, 3, or 6 months (got {months}). Which is closest?", None
    return True, "", months


def validate_string_field(name: str, v: str, min_len=2, max_len=150) -> tuple[bool, str]:
    s = v.strip()
    if len(s) < min_len:
        return False, f"{name} is too short (minimum {min_len} characters)."
    if len(s) > max_len:
        return False, f"{name} is too long (maximum {max_len} characters)."
    return True, ""


def validate_linkedin(v: str) -> tuple[bool, str]:
    if not LINKEDIN_RE.match(v.strip()):
        return False, "That doesn't look like a LinkedIn URL. It should start with linkedin.com/in/..."
    return True, ""


def validate_github(v: str) -> tuple[bool, str]:
    if not GITHUB_RE.match(v.strip()):
        return False, "That doesn't look like a GitHub profile URL (github.com/username)."
    return True, ""


# Mapping field → validator function (returns ok, msg[, coerced_value])
FIELD_VALIDATORS = {
    "email": validate_email,
    "cgpa": validate_cgpa,
    "degree": validate_degree,
    "graduation_year": validate_graduation_year,
    "preferred_location": validate_preferred_location,
    "available_from": validate_available_from,
    "internship_duration_months": validate_internship_duration,
    "college": lambda v: validate_string_field("College", v, 2, 150),
    "branch": lambda v: validate_string_field("Branch/Specialisation", v, 2, 100),
    "linkedin_url": validate_linkedin,
    "github_url": validate_github,
}


def validate_field(field: str, value) -> dict:
    """
    Validate a single field value.
    Returns {"ok": bool, "error": str, "coerced": value_or_None}
    """
    validator = FIELD_VALIDATORS.get(field)
    if not validator:
        return {"ok": True, "error": "", "coerced": value}

    result = validator(value)
    if len(result) == 3:
        ok, msg, coerced = result
    else:
        ok, msg = result
        coerced = value.strip() if isinstance(value, str) else value

    return {"ok": ok, "error": msg, "coerced": coerced if ok else None}
