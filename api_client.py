"""
Candidate Database API client — wraps all REST calls the agent uses.
"""
import os
import uuid
import requests
from datetime import datetime, timezone


BASE_URL = os.getenv(
    "CANDIDATE_BASE_URL",
    "https://candidate-db-api.service.external.usea2.aws.prodigaltech.com"
)
API_KEY = os.getenv("CANDIDATE_API_KEY", "")


def _headers(extra: dict | None = None) -> dict:
    h = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def get_schema() -> dict:
    """Fetch field definitions and validation rules (no auth needed)."""
    r = requests.get(f"{BASE_URL}/v1/schema", timeout=15)
    r.raise_for_status()
    return r.json()


def list_candidates(collection_status: str | None = None) -> list[dict]:
    """List all candidates in the workspace, optionally filtered by status."""
    params = {}
    if collection_status:
        params["collection_status"] = collection_status
    r = requests.get(f"{BASE_URL}/v1/candidates", headers=_headers(), params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("candidates", [])


def get_candidate(candidate_id: str) -> dict:
    """Fetch a single candidate record by ID."""
    r = requests.get(f"{BASE_URL}/v1/candidates/{candidate_id}", headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def patch_candidate(candidate_id: str, updates: dict, version: int) -> dict:
    """
    Update a candidate record using optimistic concurrency (If-Match header).
    Returns the updated record or raises on conflict (409) / validation (422).
    """
    idempotency_key = str(uuid.uuid4())
    extra = {
        "If-Match": f'"{version}"',
        "Idempotency-Key": idempotency_key,
    }
    r = requests.patch(
        f"{BASE_URL}/v1/candidates/{candidate_id}",
        headers=_headers(extra),
        json=updates,
        timeout=15,
    )
    if r.status_code == 409:
        raise ConflictError(f"Version conflict for {candidate_id}. Re-read the record first.")
    if r.status_code == 422:
        raise ValidationError(f"422 Unprocessable: {r.text}")
    r.raise_for_status()
    return r.json()


def reset_workspace() -> dict:
    """Restore all candidates to their original data (use for testing only)."""
    r = requests.post(f"{BASE_URL}/v1/reset", headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


class ConflictError(Exception):
    pass


class ValidationError(Exception):
    pass


# ── Helper: decide which mandatory fields are still missing ───────────────────

MANDATORY_FIELDS = [
    "email", "college", "degree", "branch",
    "graduation_year", "cgpa", "preferred_location",
    "available_from", "internship_duration_months",
]
# phone and full_name are always expected present


def missing_fields(candidate: dict) -> list[str]:
    """Return list of mandatory field names that are null/missing."""
    return [f for f in MANDATORY_FIELDS if candidate.get(f) is None]


def now_iso() -> str:
    """Current datetime as ISO 8601 with UTC offset."""
    return datetime.now(tz=timezone.utc).astimezone().isoformat()

