"""Size limits for requests and text fields (AUDIT.md #10).

A request over its size limit gets `413`. A text field over its limit gets
`400` naming the field, instead of being cut short without a word. Keep
ENDPOINTS.md in step with these numbers.
"""
from __future__ import annotations

import json

from api_common import ApiError

MB = 1024 * 1024

# Whole request body, in bytes.
MAX_BODY = 20 * MB
MAX_UPLOAD = 100 * MB

# Text fields, in characters (after spaces are tidied, where the code tidies
# them). AI-written notes and summaries use the same limits as the user's own.
NOTE_MAX = 200_000
FIELD_LIMITS = {
    "name": 200,
    "display_name": 120,
    "folder_name": 200,
    "tag": 40,
    "summary": NOTE_MAX,
    "condensed_summary": NOTE_MAX,
    "user_notes": NOTE_MAX,
    "notes_to_ai": NOTE_MAX,
    "notes_from_ai": NOTE_MAX,
    "markdown": NOTE_MAX,
    "content": 2_000_000,
}
# Not in FIELD_LIMITS: "title" also carries imported chat titles (Compare
# tabs), which can be any length. Only a rename the user types is checked.
CONVERSATION_TITLE_MAX = 1000
FILE_NAME_MAX = 255
# A refused request's unread body is read and thrown away up to this size, so
# the browser sees the error reply instead of a reset connection.
MAX_DRAIN = 256 * MB
LABEL_NAME_MAX = 60
SNAPSHOT_NAME_MAX = 120
PREFERENCE_VALUE_MAX = 100_000  # bytes of JSON per preference


FIELD_NAMES = {
    "condensed_summary": "The condensed summary", "user_notes": "Your notes",
    "notes_to_ai": "Notes to AI", "notes_from_ai": "Notes from AI",
    "markdown": "The text", "display_name": "The display name",
    "folder_name": "The folder name", "content": "The content",
}


def field_label(field: str) -> str:
    return FIELD_NAMES.get(field, "The " + field.replace("_", " "))


def too_long(field: str, limit: int) -> ApiError:
    return ApiError(f"{field_label(field)} is too long (at most {limit:,} characters).", 400)


def check_text(field: str, value: str, limit: int) -> str:
    """Return `value`, or raise if it is longer than `limit`."""
    if len(value) > limit:
        raise too_long(field, limit)
    return value


def check_fields(payload: dict) -> None:
    """Check the top-level text fields of a JSON body against FIELD_LIMITS,
    and each preference value against PREFERENCE_VALUE_MAX."""
    for field, limit in FIELD_LIMITS.items():
        value = payload.get(field)
        if isinstance(value, str):
            check_text(field, value, limit)
    prefs = payload.get("preferences")
    if isinstance(prefs, dict):
        check_preferences(prefs)


def check_preferences(prefs: dict) -> None:
    """Each preference value must fit in PREFERENCE_VALUE_MAX bytes of JSON."""
    for key, value in prefs.items():
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        if size > PREFERENCE_VALUE_MAX:
            raise ApiError(f"The preference {str(key)[:60]} is too large "
                           f"(at most {PREFERENCE_VALUE_MAX:,} bytes).", 413)
