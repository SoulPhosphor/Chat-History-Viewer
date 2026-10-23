"""The rules every conversation list, search and report shares (AUDIT.md #25).

What "not deleted", "this provider" and "display title" mean lives here and
nowhere else. Queries take these pieces instead of writing the SQL out, so
moving a table (session 6) means changing this file, not every query.

Every piece assumes the usual aliases: `c` for `conversations` and `cm` for
`conversation_meta`, which lives in userdata.db (attached as `udb`, see
server.open_db) since session 6. The functions take other aliases for the rare query that
joins the tables twice.
"""

PROVIDERS = ("claude", "chatgpt")


def provider_or_none(value) -> str | None:
    """The provider named by a request ("claude" or "chatgpt"), or None for
    anything else. None means both sides."""
    value = str(value or "").strip().lower()
    return value if value in PROVIDERS else None


def provider_from_qs(qs) -> str | None:
    """The `provider` query-string value, checked as above."""
    return provider_or_none((qs.get("provider") or [""])[0])


def meta_join(on: str = "c.id", meta: str = "cm") -> str:
    """LEFT JOIN the user's per-conversation state (rename, archive, Recycle
    Bin) onto a query. A conversation with no row there is active."""
    return f"LEFT JOIN udb.conversation_meta {meta} ON {meta}.conversation_id = {on}"


def display_title(conv: str = "c", meta: str = "cm") -> str:
    """The user's rename when there is one, otherwise the imported title."""
    return f"COALESCE(NULLIF({meta}.custom_title, ''), {conv}.title)"


def not_deleted(meta: str = "cm") -> str:
    """Not in the Recycle Bin."""
    return f"COALESCE({meta}.deleted, 0) = 0"


def in_recycle_bin(meta: str = "cm") -> str:
    """In the Recycle Bin."""
    return f"COALESCE({meta}.deleted, 0) = 1"


def archived(meta: str = "cm") -> str:
    return f"COALESCE({meta}.archived, 0) = 1"


def not_archived(meta: str = "cm") -> str:
    return f"COALESCE({meta}.archived, 0) = 0"


def is_provider(provider: str, conv: str = "c") -> str:
    """One side only. The value is checked against PROVIDERS, so it is safe
    to put into the SQL as a literal (and keeps the caller's parameters in
    order)."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider!r}")
    return f"{conv}.provider = '{provider}'"


def and_provider(provider: str | None, conv: str = "c") -> str:
    """" AND <one side>", or nothing when the request names no side."""
    return f" AND {is_provider(provider, conv)}" if provider else ""


def providers_in(providers, conv: str = "c") -> tuple[str, list]:
    """Any of several sides, as SQL plus its parameters."""
    providers = [p for p in providers if p in PROVIDERS]
    if not providers:
        return "0", []
    return f"{conv}.provider IN ({','.join('?' * len(providers))})", providers


# The same pieces with the usual aliases, for queries to drop into f-strings.
META_JOIN = meta_join()
DISPLAY_TITLE = display_title()
NOT_DELETED = not_deleted()
IN_RECYCLE_BIN = in_recycle_bin()
