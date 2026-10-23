#!/usr/bin/env python3
"""
Parse source/conversations.json → SQLite with FTS5.
Supports both ChatGPT and Claude export formats.

Schema
------
conversations(id, title, create_time, update_time, message_count, preview)
messages(id, conversation_id, role, content, create_time, seq)
search_index [fts5](conversation_id UNINDEXED, title, body)

Can also be run directly:
    python3 build_db.py [--source source/conversations.json] [--db history.db]
"""
import hashlib
import math
import json
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from itertools import islice
from datetime import datetime, timezone
from pathlib import Path

import app_paths

# ── Content-type handling ──────────────────────────────────────────────────────

# Roles whose messages we never show
SKIP_ROLES = frozenset({"system", "tool"})

# Content types we silently ignore
SKIP_CONTENT_TYPES = frozenset({
    "thoughts",
    "reasoning_recap",
    "user_editable_context",
    "tether_browsing_display",
    "tether_quote",
    "code",           # tool invocation JSON (search, browser, etc.) — not user-facing
})


def message_text(content: dict) -> str:
    """Return displayable markdown text from a message content dict."""
    ct = (content or {}).get("content_type", "")

    if ct == "text":
        parts = content.get("parts", [])
        chunks = []
        for p in parts:
            if isinstance(p, str):
                # Canvas artifact parts arrive as a JSON string:
                # '{"name":"...", "type":"document", "content":"# markdown..."}'
                # Unwrap the inner markdown so it renders properly.
                stripped = p.strip()
                if stripped.startswith('{') and '"type"' in stripped and '"content"' in stripped:
                    try:
                        obj = json.loads(stripped)
                        if obj.get("type") in ("document", "canvas", "artifact") and isinstance(obj.get("content"), str):
                            chunks.append(obj["content"].strip())
                            continue
                    except (json.JSONDecodeError, AttributeError):
                        pass
                chunks.append(p)
            elif isinstance(p, dict):
                # Canvas / document artifacts embedded as dict parts
                doc = p.get("content") or p.get("text") or ""
                if isinstance(doc, str) and doc.strip():
                    chunks.append(doc.strip())
        return "\n\n".join(chunks).strip()

    if ct == "code":
        # Code interpreter / tool output — stored in content["text"] directly
        body = (content.get("text") or "").strip()
        lang = (content.get("language") or "").strip()
        return f"```{lang}\n{body}\n```" if body else ""

    if ct == "multimodal_text":
        chunks = []
        for p in content.get("parts", []):
            if isinstance(p, str):
                chunks.append(p)
            elif isinstance(p, dict):
                if p.get("content_type") == "image_asset_pointer":
                    ptr = p.get("asset_pointer", "")
                    # Strip URI schemes: sediment://file_XXX or file-service://file-XXX
                    file_id = (
                        ptr.replace("file-service://", "")
                           .replace("sediment://", "")
                           .split("#")[0]
                    )
                    if file_id:
                        chunks.append(f"![image](/source/{file_id})")
                elif p.get("type") in ("document", "canvas", "artifact"):
                    # Explicit canvas/document parts
                    doc = p.get("content") or p.get("text") or ""
                    if isinstance(doc, str) and doc.strip():
                        chunks.append(doc.strip())
                else:
                    text = p.get("text") or p.get("content") or ""
                    if isinstance(text, str) and text:
                        chunks.append(text)
        return "\n".join(c for c in chunks if c).strip()

    # Generic fallback: unknown content types that carry text directly
    # (e.g. content_type="document" or missing content_type)
    for key in ("text", "content"):
        val = content.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    return ""


# ── Format detection ──────────────────────────────────────────────────────────


def detect_format(data: list) -> str:
    """Return 'claude' or 'chatgpt' based on the first conversation's keys."""
    if not data:
        return "chatgpt"
    first = data[0]
    if "chat_messages" in first or ("uuid" in first and "name" in first):
        return "claude"
    return "chatgpt"


def detect_provider(data) -> str | None:
    """
    Identify a backup file *by its contents*, not its filename.

    Returns 'claude', 'chatgpt', or None. None means the payload is not a
    positively-recognized Claude or ChatGPT conversation export (for example a
    memories/users/projects file, an empty list, or arbitrary JSON) and must
    not be imported.

    The structural markers are mutually exclusive in practice:
      • Claude conversations carry `chat_messages` (and a `uuid` + `name`).
      • ChatGPT conversations carry a `mapping` tree with a `current_node`.
    A couple of leading dict entries are sampled so one malformed record does
    not defeat detection.
    """
    if not isinstance(data, list) or not data:
        return None
    for conv in data[:5]:
        if not isinstance(conv, dict):
            continue
        if "chat_messages" in conv:
            return "claude"
        if "mapping" in conv and "current_node" in conv:
            return "chatgpt"
        if "uuid" in conv and "name" in conv and "created_at" in conv:
            return "claude"
        if "mapping" in conv and ("title" in conv or "create_time" in conv):
            return "chatgpt"
    return None


# ── Reading an export one chat at a time (AUDIT.md #31) ───────────────────────


class NotAList(ValueError):
    """The file is JSON, but not a list of chats."""


_WHITESPACE = re.compile(r"[ \t\n\r]*")


def iter_export(path, chunk_size: int = 1 << 20):
    """Yield the items of the JSON list in `path` one at a time, so only one
    chat (plus a read buffer) is in memory instead of the whole export.
    Raises NotAList when the file holds something other than a list, and
    ValueError (json.JSONDecodeError or UnicodeDecodeError) when it is not
    valid JSON, which can happen after some items were already yielded."""
    decoder = json.JSONDecoder()
    with open(path, encoding="utf-8-sig") as f:
        buf, pos, eof = "", 0, False

        def more(size: int) -> bool:
            nonlocal buf, pos, eof
            data = f.read(size) if not eof else ""
            if not data:
                eof = True
                return False
            buf = buf[pos:] + data
            pos = 0
            return True

        def skip_space() -> None:
            nonlocal pos
            while True:
                pos = _WHITESPACE.match(buf, pos).end()
                if pos < len(buf) or not more(chunk_size):
                    return

        def fail(what: str):
            raise json.JSONDecodeError(what, buf, pos)

        skip_space()
        if pos >= len(buf) or buf[pos] != "[":
            if pos < len(buf) and buf[pos] not in "{\"-0123456789tfn":
                fail("Expecting value")
            raise NotAList("The file is not a list of chats.")
        pos += 1
        first = True
        while True:
            skip_space()
            if pos >= len(buf):
                fail("The file ends in the middle of the list")
            if buf[pos] == "]":
                pos += 1
                skip_space()
                if pos < len(buf):
                    fail("Extra data after the list")
                return
            if not first:
                if buf[pos] != ",":
                    fail("Expecting ',' delimiter")
                pos += 1
                skip_space()
            first = False
            want = chunk_size
            while True:
                try:
                    item, end = decoder.raw_decode(buf, pos)
                except json.JSONDecodeError:
                    # Probably cut off at the end of the buffer: read more,
                    # at least as much again, so a long chat is not re-read
                    # many times.
                    if not more(max(want, len(buf) - pos)):
                        raise
                    want *= 2
                    continue
                if end == len(buf) and more(want):
                    continue  # a number or word may go on in the next chunk
                break
            pos = end
            yield item


def peek_export(path, count: int = 5) -> list:
    """The first `count` items of an export (fewer when it is shorter)."""
    return list(islice(iter_export(path), count))


def export_provider(path) -> str | None:
    """detect_provider for a file, reading only its first few chats. None
    when it is not a recognised export (or not a list); ValueError when it
    does not start as valid JSON."""
    try:
        return detect_provider(peek_export(path))
    except NotAList:
        return None


def file_sha256(path, *, job=None) -> str:
    """The file's SHA-256, read in pieces rather than all at once."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            if job:
                job.check()
            digest.update(block)
    return digest.hexdigest()


# ── Free disk space (AUDIT.md #31) ────────────────────────────────────────────

# The database takes about 2.7 times the export's size on disk (mostly the
# search index), so ask for 3 times before building or importing.
SPACE_FACTOR = 3


class NotEnoughSpace(OSError):
    """Not enough free disk space for the database an export would make."""


def _megabytes(n: int) -> str:
    return f"{max(1, round(n / 1_000_000)):,} MB"


def check_free_space(db_path, export_bytes: int) -> None:
    """Refuse to start when the disk holding the database cannot take what
    this export will add, instead of failing part-way with a full disk."""
    folder = Path(db_path).resolve().parent
    needed = int(export_bytes) * SPACE_FACTOR
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return  # can't tell; go ahead as before
    if free < needed:
        raise NotEnoughSpace(
            f"There isn't enough free disk space to bring in this export. It "
            f"needs about {_megabytes(needed)} free on the drive that holds "
            f"the app's database, and there is {_megabytes(free)}. Free some "
            f"space and try again."
        )


# ── Claude format helpers ─────────────────────────────────────────────────────

# Claude content block types to skip
CLAUDE_SKIP_TYPES = frozenset({"thinking", "tool_use", "tool_result", "token_budget"})


def _iso_to_ts(s: str) -> float | None:
    """Convert ISO 8601 string (with optional Z) to a unix timestamp float."""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _numeric_ts(value) -> float | None:
    """An exported Unix time, or unknown; never turn broken dates into 1970."""
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


_IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "heic", "heif"})


def claude_message_parts(msg: dict) -> tuple[str, list]:
    """
    Extract (text, attachments) from a Claude chat message.

    Returns:
        text        – plain message text only (no attachment content embedded)
        attachments – list of {name, type, content} dicts for chips/side-panel
    """
    is_assistant = msg.get("sender", msg.get("role", "")) == "assistant"
    text_chunks = []
    for block in (msg.get("content") or []):
        if not isinstance(block, dict):
            continue
        if block.get("type") in CLAUDE_SKIP_TYPES:
            continue
        if block.get("type") == "text":
            text = (block.get("text") or "").strip()
            if text:
                text_chunks.append(text)
    # NOTE: Never fall back to top-level msg["text"] for assistant messages.
    # That field contains Claude's internal verbalized thinking/planning notes,
    # not the user-facing response. For human messages it's also redundant
    # (human content blocks always have the text).  Drop the fallback entirely.

    # Build attachment list (each has name, type, content for side-panel).
    att_bag: Counter = Counter()
    # Attachments with no extracted text, by file name: kept as a "missing
    # file" chip (AUDIT.md #12) and given the file id from `files` below.
    no_text: dict[str, list] = defaultdict(list)
    attachments = []
    for att in (msg.get("attachments") or []):
        if not isinstance(att, dict):
            continue
        extracted = (att.get("extracted_content") or "").strip()
        name  = (att.get("file_name") or "").strip()
        ftype = (att.get("file_type") or "").strip()
        # Use a friendly display name: prefer file_name, else "Pasted text", else type
        display_name = name or ("Pasted text" if ftype in ("txt", "text") else ftype or "file")
        attachments.append({"name": display_name, "type": ftype, "content": extracted})
        if extracted:
            att_bag[name] += 1
        else:
            no_text[name].append(attachments[-1])

    # Images / files without extracted content: chip referencing file by uuid.
    for fi in (msg.get("files") or []):
        if not isinstance(fi, dict):
            continue
        fn   = (fi.get("file_name") or "").strip()
        uuid = (fi.get("file_uuid") or "").strip()
        ext  = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
        if att_bag[fn] > 0:
            att_bag[fn] -= 1
            continue
        if no_text[fn]:
            entry = no_text[fn].pop(0)
            entry["type"] = entry["type"] or ext or "image"
            if uuid:
                entry["uuid"] = uuid
            continue
        display = fn or "Image"
        attachments.append({
            "name": display,
            "type": ext or "image",
            "content": "",
            "uuid": uuid,   # kept so frontend can request /source/files/<uuid>
        })

    # For assistant messages: extract output files from present_files tool_result
    # These are Claude-generated files (docx, etc.) referenced via local_resource blocks.
    if is_assistant:
        for block in (msg.get("content") or []):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            rc = block.get("content") or []
            if isinstance(rc, str):
                continue
            for item in (rc if isinstance(rc, list) else []):
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "local_resource":
                    fp = item.get("file_path") or ""
                    ext = fp.rsplit(".", 1)[-1].lower() if "." in fp else ""
                    uuid = (item.get("uuid") or "").strip()
                    # Use the human-readable display name from the export
                    display_name = (item.get("name") or "").strip()
                    if not display_name:
                        display_name = fp.split("/")[-1] if fp else ""
                    if display_name:
                        # Append extension if not already present
                        if ext and not display_name.lower().endswith("." + ext):
                            display_name = f"{display_name}.{ext}"
                        attachments.append({
                            "name": display_name,
                            "type": ext,
                            "content": "",
                            "uuid": uuid,
                        })

    return "\n\n".join(text_chunks), attachments


# Content block types the viewer shows. Everything else is either dropped on
# purpose (CLAUDE_SKIP_TYPES) or a type this app does not know yet.
CLAUDE_SHOWN_TYPES = frozenset({"text"})


def _new_content_types() -> dict:
    return {"dropped": Counter(), "unknown": Counter()}


def _plain_content_types(found: dict) -> dict:
    """Counters as plain dicts, so a record can be stored as JSON."""
    return {k: dict(sorted(v.items())) for k, v in found.items()}


def claude_content_types(raw_msgs: list) -> dict:
    """Count the content blocks of one Claude chat that are not shown:
    `dropped` (thinking, tool use and results, known and left out on purpose)
    and `unknown` (a block type this app does not know, e.g. one a future
    export adds). The import audit adds them up (AUDIT.md #12), so a format
    change shows instead of vanishing silently."""
    found = _new_content_types()
    for msg in raw_msgs:
        if not isinstance(msg, dict):
            continue
        for block in (msg.get("content") or []):
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type") or "(no type)")
            if kind in CLAUDE_SHOWN_TYPES:
                continue
            found["dropped" if kind in CLAUDE_SKIP_TYPES else "unknown"][kind] += 1
    return _plain_content_types(found)


def claude_artifact_ids(msg: dict) -> list[str]:
    """Return list of artifact IDs created in this assistant message."""
    ids = []
    for block in (msg.get("content") or []):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("name") == "artifacts":
            inp = block.get("input") or {}
            if inp.get("command") == "create" and inp.get("id"):
                ids.append(inp["id"])
    return ids


def _apply_artifact_ops(msg: dict, msg_seq: int, state: dict):
    """
    Apply artifact operations from one assistant message to the running state dict.
    state: {artifact_id: {title, type, lang, content, msg_seq}}
    Artifacts are created once and updated/rewritten across subsequent messages.
    """
    for block in (msg.get("content") or []):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("name") == "artifacts":
            inp = block.get("input") or {}
            aid = inp.get("id")
            if not aid:
                continue
            c = inp.get("command", "")
            if c == "create":
                state[aid] = {
                    "content": inp.get("content") or "",
                    "title":   inp.get("title") or "",
                    "type":    inp.get("type") or "",
                    "lang":    inp.get("language") or "",
                    "msg_seq": msg_seq,
                }
            elif aid in state:
                if c == "rewrite":
                    new_c = inp.get("content") or ""
                    if new_c:
                        state[aid]["content"] = new_c
                    if inp.get("title"):
                        state[aid]["title"] = inp["title"]
                    state[aid]["msg_seq"] = msg_seq
                elif c == "update":
                    old_s = inp.get("old_str", "")
                    new_s = inp.get("new_str", "")
                    content = state[aid]["content"]
                    if old_s in content:
                        state[aid]["content"] = content.replace(old_s, new_s, 1)
                    state[aid]["msg_seq"] = msg_seq


def _assistant_chain(first: dict, get_last) -> list:
    """The assistant messages that follow each other from `first` (a reply
    that used tools is several in a row). No fixed limit on its length
    (AUDIT.md #4); a message already in the chain ends it, so a loop in the
    export cannot run forever."""
    chain = [first]
    seen = {first.get("uuid", "")}
    while True:
        nxt = get_last(chain[-1].get("uuid", ""), "assistant")
        if nxt is None or nxt.get("uuid", "") in seen:
            return chain
        seen.add(nxt.get("uuid", ""))
        chain.append(nxt)


def _claude_active_path(conv: dict, cid: str, raw_msgs: list):
    """
    Parse a single Claude conversation dict using tree traversal.

    Claude's export stores all branch variants flat.  parent_message_uuid forms
    a tree; messages sharing the same UUID are human/assistant pairs in the same
    "turn slot".  Multiple children with the same (parent, sender) are retries.
    We walk the active path (always taking the LAST sibling = most recent edit)
    and record all siblings so the UI can show ← / → navigation.

    Returns (msgs, artifacts).  Returns ([], []) when no active path can be
    recovered — the caller decides how to fall back rather than dropping the
    conversation.
    """
    if not raw_msgs:
        return [], []

    # All message UUIDs in this conversation.
    msg_uuid_set = {m.get("uuid") for m in raw_msgs}

    # Group by (parent_uuid, sender) to detect sibling branches.
    # Messages whose parent is NOT in msg_uuid_set are root-level entries.
    VIRTUAL_ROOT = "__ROOT__"
    by_ps: dict[tuple, list] = {}
    for m in raw_msgs:
        p = m.get("parent_message_uuid") or ""
        p_key = p if p in msg_uuid_set else VIRTUAL_ROOT
        s = m.get("sender", "")
        by_ps.setdefault((p_key, s), []).append(m)

    def get_last(parent_key: str, sender: str):
        group = by_ps.get((parent_key, sender), [])
        return group[-1] if group else None

    def get_all(parent_key: str, sender: str):
        return by_ps.get((parent_key, sender), [])

    # ── Walk the active path ───────────────────────────────────────────────────
    result: list[dict] = []
    artifacts_out: list[dict] = []
    artifact_state: dict = {}   # {artifact_id: {title, type, lang, content, msg_seq}}
    seen: set[str] = set()
    seq = 0

    cur_human = get_last(VIRTUAL_ROOT, "human")
    if not cur_human:
        return [], []

    # No fixed limit on turns (AUDIT.md #4): every message is visited at most
    # once (`seen`), so a loop in the export still ends.
    while True:
        h_uuid = cur_human.get("uuid", "")
        if h_uuid in seen:
            break
        seen.add(h_uuid)

        # Siblings at this human-message slot (same parent, sender=human)
        p_of_h = cur_human.get("parent_message_uuid") or ""
        p_key_of_h = p_of_h if p_of_h in msg_uuid_set else VIRTUAL_ROOT
        siblings_h = get_all(p_key_of_h, "human")
        b_idx_h = siblings_h.index(cur_human) + 1
        b_cnt_h = len(siblings_h)

        h_text, h_atts = claude_message_parts(cur_human)

        siblings_h_data = None
        if b_cnt_h > 1:
            sibs = []
            for sib in siblings_h:
                st, sa = claude_message_parts(sib)
                # Include the assistant response that goes with this user message version
                sib_uuid = sib.get("uuid", "")
                sib_asst = get_last(sib_uuid, "assistant")
                asst_text, asst_atts = claude_message_parts(sib_asst) if sib_asst else ("", [])
                asst_artifact_ids = claude_artifact_ids(sib_asst) if sib_asst else []
                sibs.append({
                    "content":          st,
                    "attachments":      sa,
                    "asst_content":     asst_text,
                    "asst_attachments": asst_atts,
                    "asst_artifact_ids": asst_artifact_ids,
                })
            siblings_h_data = json.dumps(sibs, ensure_ascii=False)

        if h_text or h_atts:
            result.append({
                "conversation_id": cid,
                "role":            "user",
                "content":         h_text,
                "attachments":     json.dumps(h_atts, ensure_ascii=False) if h_atts else None,
                "siblings":        siblings_h_data,
                "branch_index":    b_idx_h,
                "create_time":     _iso_to_ts(cur_human.get("created_at") or ""),
                "seq":             seq,
                "message_uuid":    _message_uuid(cur_human),
            })
            seq += 1

        # Assistant response to this human message
        # Claude compute-tool chains produce multiple consecutive assistant messages:
        #   human → assistant_1(bash_tool only) → assistant_2(bash_tool + final text)
        # Also, some siblings (retries) are dead-ends; we must find the sibling
        # whose chain actually leads to the next human turn.
        all_direct_siblings = get_all(h_uuid, "assistant")
        first_asst = None
        asst_chain: list = []
        nxt_from_chain = None

        if all_direct_siblings:
            # Try siblings in reverse (most recent first); pick the first one
            # whose chain connects to a human continuation.
            for cand in reversed(all_direct_siblings):
                chain = _assistant_chain(cand, get_last)
                lid = chain[-1].get("uuid", "")
                nxt = get_last(lid, "human")
                if nxt is not None:
                    first_asst = cand
                    asst_chain = chain
                    nxt_from_chain = nxt
                    break
            if first_asst is None:
                # No sibling leads to a continuation — use the last sibling
                first_asst = all_direct_siblings[-1]
                asst_chain = _assistant_chain(first_asst, get_last)

        if first_asst:
            leaf_uuid = asst_chain[-1].get("uuid", "")

            # Siblings/retries are always at the first level (direct children of human)
            siblings_a = all_direct_siblings
            b_idx_a = siblings_a.index(first_asst) + 1
            b_cnt_a = len(siblings_a)

            # Collect text, attachments, and artifact IDs from the whole chain
            all_text_chunks: list[str] = []
            all_atts: list = []
            all_artifact_ids: list = []
            for node in asst_chain:
                node_text, node_atts = claude_message_parts(node)
                if node_text:
                    all_text_chunks.append(node_text)
                all_atts.extend(node_atts)
                all_artifact_ids.extend(claude_artifact_ids(node))
                _apply_artifact_ops(node, seq, artifact_state)

            a_text = "\n\n".join(all_text_chunks)
            a_atts = all_atts
            a_artifact_ids = list(dict.fromkeys(all_artifact_ids))  # dedupe, preserve order

            siblings_a_data = None
            if b_cnt_a > 1:
                sibs = []
                for sib in siblings_a:
                    st, sa = claude_message_parts(sib)
                    sibs.append({"content": st, "attachments": sa,
                                 "artifact_ids": claude_artifact_ids(sib)})
                siblings_a_data = json.dumps(sibs, ensure_ascii=False)

            if a_text or a_atts or a_artifact_ids:
                result.append({
                    "conversation_id": cid,
                    "role":            "assistant",
                    "content":         a_text,
                    "attachments":     json.dumps(a_atts, ensure_ascii=False) if a_atts else None,
                    "artifact_ids":    json.dumps(a_artifact_ids, ensure_ascii=False) if a_artifact_ids else None,
                    "siblings":        siblings_a_data,
                    "branch_index":    b_idx_a,
                    "create_time":     _iso_to_ts(first_asst.get("created_at") or ""),
                    "seq":             seq,
                    "message_uuid":    _message_uuid(first_asst),
                })
                seq += 1

            # Next human: from the leaf's continuation (found during sibling search)
            nxt = nxt_from_chain if nxt_from_chain else get_last(leaf_uuid, "human")
            if nxt is None:
                nxt = get_last(h_uuid, "human")
            cur_human = nxt
        else:
            # No assistant yet; check for chained human (rare)
            cur_human = get_last(h_uuid, "human")

        if cur_human is None:
            break

    # Convert accumulated artifact state → output rows
    for aid, a in artifact_state.items():
        artifacts_out.append({
            "id":      aid,
            "conv_id": cid,
            "msg_seq": a["msg_seq"],
            "title":   a["title"],
            "type":    a["type"],
            "lang":    a["lang"],
            "content": a["content"],
        })

    return result, artifacts_out


# ── Synthetic IDs, fallback parsing, and meta ─────────────────────────────────


def _synthetic_id(conv: dict) -> str:
    """
    Deterministic synthetic ID for a conversation that has no Claude UUID.

    Based on a stable SHA-256 of the raw conversation object, so re-importing
    the same export always produces the same ID.  Two byte-for-byte identical
    raw records intentionally map to the same ID (see duplicate reporting).
    """
    canonical = json.dumps(conv, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"synthetic-{digest}"


def _claude_fallback_messages(conv: dict, cid: str, raw_msgs: list) -> list:
    """
    Recover displayable human/assistant messages when the branch parser cannot
    find an active path.  Ignores tree structure entirely: takes every message
    that yields text or attachments, ordered by timestamp then source order.
    """
    rows = []
    seq = 0
    # Drop non-dict entries before sorting: the sort key reads each entry, so a
    # single malformed one would raise here — inside the handler that is itself
    # the recovery path — and cost the conversation every recoverable message.
    ordered = sorted(
        ((i, m) for i, m in enumerate(raw_msgs) if isinstance(m, dict)),
        key=lambda pair: (_iso_to_ts(pair[1].get("created_at") or "") or 0, pair[0]),
    )
    for _idx, m in ordered:
        sender = m.get("sender", m.get("role", ""))
        if sender not in ("human", "assistant"):
            continue
        text, atts = claude_message_parts(m)
        if not text and not atts:
            continue
        rows.append({
            "conversation_id": cid,
            "role":            "user" if sender == "human" else "assistant",
            "content":         text,
            "attachments":     json.dumps(atts, ensure_ascii=False) if atts else None,
            "artifact_ids":    None,
            "siblings":        None,
            "branch_index":    1,
            "create_time":     _iso_to_ts(m.get("created_at") or ""),
            "seq":             seq,
            "message_uuid":    _message_uuid(m),
        })
        seq += 1
    return rows


def _claude_meta(conv: dict, cid: str, msgs: list, status: str) -> dict:
    """Build the conversations-table row for a Claude conversation."""
    preview = next(
        (m["content"][:300] for m in msgs if m["role"] == "user" and m["content"]),
        "",
    )
    if not preview:
        preview = next((m["content"][:300] for m in msgs if m["content"]), "")
    raw_name = (conv.get("name") or "").strip()
    if not raw_name and preview:
        raw_name = preview[:60].replace("\n", " ").strip()
    if not raw_name:
        raw_name = "Untitled"
    return {
        "id":            cid,
        "title":         raw_name,
        "create_time":   _iso_to_ts(conv.get("created_at") or ""),
        "update_time":   _iso_to_ts(conv.get("updated_at") or ""),
        "message_count": len(msgs),
        "preview":       preview,
        "import_status": status,
    }


def import_claude_conversation(conv: dict, index: int) -> dict:
    """
    Import one Claude conversation with full accounting — never drops a record.

    Returns a record dict:
        {meta, msgs, artifacts, status, synthetic, error, index}

    status is one of: normal | fallback | metadata_only | parse_error
    """
    raw_uuid = conv.get("uuid")
    if raw_uuid and str(raw_uuid).strip():
        cid = str(raw_uuid).strip()          # preserve the real Claude UUID exactly
        synthetic = False
    else:
        cid = _synthetic_id(conv)
        synthetic = True

    raw_msgs = conv.get("chat_messages") or []
    error = None

    try:
        active_msgs, active_arts = _claude_active_path(conv, cid, raw_msgs)
        if active_msgs:
            # Branch parser recovered messages (with text and/or attachments).
            msgs, artifacts, status = active_msgs, active_arts, "normal"
        else:
            # Branch parsing failed — try the structure-agnostic fallback.
            fb = _claude_fallback_messages(conv, cid, raw_msgs)
            if fb:
                msgs, artifacts, status = fb, [], "fallback"
            else:
                # Nothing displayable at all — keep as a metadata-only record.
                msgs, artifacts, status = [], [], "metadata_only"
    except Exception as e:  # noqa: BLE001 — must never let one record abort import
        error = f"{type(e).__name__}: {e}"
        try:
            msgs = _claude_fallback_messages(conv, cid, raw_msgs)
        except Exception:
            msgs = []
        artifacts, status = [], "parse_error"

    meta = _claude_meta(conv, cid, msgs, status)
    try:
        content_types = claude_content_types(raw_msgs if isinstance(raw_msgs, list) else [])
    except Exception:  # noqa: BLE001 — counting must never cost the chat
        content_types = _plain_content_types(_new_content_types())
    return {
        "meta":      meta,
        "msgs":      msgs,
        "artifacts": artifacts,
        "status":    status,
        "synthetic": synthetic,
        "error":     error,
        "index":     index,
        "content_types": content_types,
    }


# ── Thread extraction ─────────────────────────────────────────────────────────


def _message_uuid(msg) -> str | None:
    """The export's own id for one message (Claude `uuid`, ChatGPT `id`).

    Kept so links and bookmarks can find the same message after a re-import
    even when its position in the chat changed (an edited or regenerated
    earlier message switches branches). It is only ever looked up together
    with its conversation id, so ids from different providers never meet."""
    if not isinstance(msg, dict):
        return None
    value = msg.get("uuid") or msg.get("id")
    value = str(value).strip() if value is not None else ""
    return value or None


def extract_thread(mapping: dict, current_node: str) -> list:
    """
    Walk from current_node back to root via parent pointers.
    Returns messages in chronological order (root → leaf).
    Handles the branching case: current_node always points to the
    last message of the active branch.
    """
    path, seen = [], set()
    node_id = current_node
    while node_id and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        node = mapping[node_id]
        if node.get("message"):
            msg = node["message"]
            # The node id doubles as the message id when a message lacks its own.
            if isinstance(msg, dict) and not msg.get("id"):
                msg = {**msg, "id": node_id}
            path.append(msg)
        node_id = node.get("parent")
    path.reverse()
    return path


# ── Per-conversation parser ───────────────────────────────────────────────────


def parse_conversation(conv: dict, cid: str | None = None):
    """
    Returns (meta_dict, message_list) or (None, []) when the
    conversation has no usable messages.

    cid overrides the conversation's own id, which is how the importer hands
    over a synthesized one for a record that shipped without any id at all.
    """
    cid = cid or conv.get("id") or conv.get("conversation_id")
    if not cid:
        return None, []

    mapping     = conv.get("mapping") or {}
    current_node = conv.get("current_node")
    if not current_node:
        return None, []

    thread = extract_thread(mapping, current_node)

    msgs = []
    for seq, msg in enumerate(thread):
        role = (msg.get("author") or {}).get("role", "")
        if role in SKIP_ROLES:
            continue
        content = msg.get("content") or {}
        if content.get("content_type") in SKIP_CONTENT_TYPES:
            continue
        text = message_text(content)
        if not text:
            continue
        metadata = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
        model_slug = next(
            (
                str(value).strip()
                for value in (
                    metadata.get("model_slug"),
                    metadata.get("default_model_slug"),
                    msg.get("model_slug"),
                    msg.get("model"),
                )
                if value is not None and str(value).strip()
            ),
            None,
        )
        msgs.append({
            "conversation_id": cid,
            "role":            role,
            "content":         text,
            "attachments":     None,
            "siblings":        None,
            "branch_index":    1,
            "create_time":     _numeric_ts(msg.get("create_time")),
            "seq":             seq,
            "message_uuid":    _message_uuid(msg),
            # ChatGPT exports identify the model per message. Keep the exact
            # imported value: one conversation may use several models.
            "model_slug":      model_slug,
        })

    if not msgs:
        return None, []

    preview = next(
        (m["content"][:300] for m in msgs if m["role"] == "user"), ""
    )
    meta = {
        "id":            cid,
        "title":         (conv.get("title") or "Untitled").strip(),
        "create_time":   _numeric_ts(conv.get("create_time")),
        "update_time":   _numeric_ts(conv.get("update_time")),
        "message_count": len(msgs),
        "preview":       preview,
    }
    return meta, msgs


# ChatGPT content types the viewer shows (message_text), and the parts of a
# multimodal message it understands.
CHATGPT_SHOWN_TYPES = frozenset({"text", "multimodal_text"})
CHATGPT_SHOWN_PARTS = frozenset({"image_asset_pointer"})


def chatgpt_content_types(conv: dict) -> dict:
    """The ChatGPT side of claude_content_types: messages of every branch,
    counted by content type. Messages from system and tool roles count as
    dropped under "<role> message"."""
    found = _new_content_types()
    mapping = conv.get("mapping") if isinstance(conv.get("mapping"), dict) else {}
    for node in mapping.values():
        msg = node.get("message") if isinstance(node, dict) else None
        if not isinstance(msg, dict):
            continue
        role = (msg.get("author") or {}).get("role", "")
        content = msg.get("content") if isinstance(msg.get("content"), dict) else {}
        kind = str(content.get("content_type") or "(no type)")
        if role in SKIP_ROLES:
            found["dropped"][f"{role} message"] += 1
            continue
        if kind in SKIP_CONTENT_TYPES:
            found["dropped"][kind] += 1
            continue
        if kind not in CHATGPT_SHOWN_TYPES:
            found["unknown"][kind] += 1
            continue
        for part in (content.get("parts") or []) if kind == "multimodal_text" else []:
            if not isinstance(part, dict):
                continue
            ptype = part.get("content_type")
            if ptype and ptype not in CHATGPT_SHOWN_PARTS and not (
                    part.get("text") or part.get("content")):
                found["unknown"][str(ptype)] += 1
    return _plain_content_types(found)


def import_chatgpt_conversation(conv: dict, index: int) -> dict:
    """
    Import one ChatGPT conversation with the same never-drop accounting used for
    Claude, so the audit totals are consistent regardless of source format.

    status is one of: normal | metadata_only | parse_error
    """
    raw_id = conv.get("id") or conv.get("conversation_id")
    if raw_id and str(raw_id).strip():
        cid = str(raw_id).strip()
        synthetic = False
    else:
        cid = _synthetic_id(conv)
        synthetic = True

    error = None
    msgs = []
    try:
        # Pass cid explicitly: for a record with no id of its own the parser
        # would otherwise bail out and lose a perfectly good message tree.
        _meta, parsed = parse_conversation(conv, cid)
        if parsed:
            for m in parsed:
                m["conversation_id"] = cid
            msgs, status = parsed, "normal"
        else:
            status = "metadata_only"
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
        msgs, status = [], "parse_error"

    title = (conv.get("title") or "").strip() or "Untitled"
    preview = next((m["content"][:300] for m in msgs if m["role"] == "user" and m["content"]), "")
    if not preview:
        preview = next((m["content"][:300] for m in msgs if m["content"]), "")
    meta = {
        "id":            cid,
        "title":         title,
        "create_time":   _numeric_ts(conv.get("create_time")),
        "update_time":   _numeric_ts(conv.get("update_time")),
        "message_count": len(msgs),
        "preview":       preview,
        "import_status": status,
        # ChatGPT Custom GPT identity. Keep the raw export values; user-facing
        # names live separately in userdata.db and survive rebuilds.
        "gizmo_id": (str(conv.get("gizmo_id")).strip() or None) if conv.get("gizmo_id") else None,
        "gizmo_type": (str(conv.get("gizmo_type")).strip() or None) if conv.get("gizmo_type") else None,
    }
    try:
        content_types = chatgpt_content_types(conv)
    except Exception:  # noqa: BLE001 — counting must never cost the chat
        content_types = _plain_content_types(_new_content_types())
    return {
        "meta":      meta,
        "msgs":      msgs,
        "artifacts": [],
        "status":    status,
        "synthetic": synthetic,
        "error":     error,
        "index":     index,
        "content_types": content_types,
    }


def memory_id(account_uuid, content) -> str:
    """A memory's id: the same text from the same account always gets the same
    id, so it survives a rebuild (AUDIT.md #11)."""
    text = f"{account_uuid or ''}\n{content or ''}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """\
DROP TABLE IF EXISTS messages;
DROP TABLE IF EXISTS conversations;
DROP TABLE IF EXISTS search_index;
DROP TABLE IF EXISTS memories;
DROP TABLE IF EXISTS project_docs;
DROP TABLE IF EXISTS projects;

CREATE TABLE conversations (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    create_time   REAL,
    update_time   REAL,
    message_count INTEGER,
    preview       TEXT,
    import_status TEXT DEFAULT 'normal',
    source_index  INTEGER,
    -- 'claude' | 'chatgpt'. Conversation identity is (id, provider): the
    -- original stable UUID plus the tool it came from.
    provider      TEXT,
    -- ChatGPT Custom GPT identity from the export (NULL for ordinary chats / Claude).
    gizmo_id      TEXT,
    gizmo_type    TEXT
);

CREATE TABLE messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role            TEXT,
    content         TEXT,
    attachments     TEXT,
    artifact_ids    TEXT,
    siblings        TEXT,
    branch_index    INTEGER DEFAULT 1,
    create_time     REAL,
    seq             INTEGER,
    -- The export's own message id (see _message_uuid). seq is the message's
    -- position in the shown branch and can move on re-import; this cannot.
    message_uuid    TEXT,
    -- ChatGPT model recorded on this exact message (NULL for Claude or when the
    -- export omitted it). Distinct values form the conversation's model list.
    model_slug      TEXT
);

CREATE INDEX idx_msg_conv ON messages (conversation_id, seq);
CREATE INDEX idx_msg_uuid ON messages (conversation_id, message_uuid);
CREATE INDEX idx_msg_model ON messages (model_slug, conversation_id);

CREATE VIRTUAL TABLE search_index USING fts5(
    conversation_id UNINDEXED,
    title,
    body,
    tokenize = 'trigram'
);

CREATE TABLE memories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_uuid TEXT,
    content      TEXT,
    -- Made from the memory's own text (memory_id()), so it is the same after
    -- a rebuild. The row number above is not.
    memory_id    TEXT
);

CREATE TABLE projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT,
    create_time  REAL,
    update_time  REAL
);

CREATE TABLE project_docs (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    filename   TEXT,
    content    TEXT,
    create_time REAL
);

DROP TABLE IF EXISTS artifacts;

-- An artifact is identified by (chat, artifact name): Claude reuses short
-- names such as "snake-game" in different chats.
CREATE TABLE artifacts (
    id       TEXT NOT NULL,
    conv_id  TEXT NOT NULL,
    msg_seq  INTEGER,
    title    TEXT,
    type     TEXT,
    lang     TEXT,
    content  TEXT,
    PRIMARY KEY (conv_id, id)
);

DROP TABLE IF EXISTS import_sources;

-- One row per top-level object in conversations.json, including the extras
-- collapsed away when several objects share an ID. `conversations` keeps one
-- row per distinct ID, so counting the audit from there would come out lower
-- than the source file; count it from here instead.
CREATE TABLE import_sources (
    source_index    INTEGER PRIMARY KEY,
    conversation_id TEXT,
    title           TEXT,
    import_status   TEXT,
    synthetic       INTEGER DEFAULT 0,
    kept            INTEGER DEFAULT 1,  -- 0 = collapsed duplicate of this ID
    -- Each source object's own counts and dates. Reading these off the winning
    -- conversations row instead would make a collapsed record report the
    -- winner's message count and dates, which is exactly what the audit is
    -- meant to let you check.
    create_time     REAL,
    update_time     REAL,
    message_count   INTEGER,
    preview         TEXT
);

CREATE INDEX idx_import_sources_status ON import_sources (import_status);

DROP TABLE IF EXISTS imported_backups;

-- One row per backup file brought in through the "Import New Chats" screen.
-- Drives the import-history table. `first_chat`/`last_chat` are unix
-- timestamps spanning every conversation/message in that file; `total_chats`
-- is how many conversations the file contained; the count columns are what
-- that one import did to the database.
CREATE TABLE imported_backups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name    TEXT,
    provider     TEXT,
    file_hash    TEXT,
    first_chat   REAL,
    last_chat    REAL,
    total_chats  INTEGER,
    imported_at  REAL,
    added        INTEGER DEFAULT 0,
    updated      INTEGER DEFAULT 0,
    unchanged    INTEGER DEFAULT 0,
    skipped      INTEGER DEFAULT 0,
    errors       INTEGER DEFAULT 0,
    -- JSON {content type: count}: content left out on purpose, and content of
    -- a type this app does not know (AUDIT.md #12).
    dropped_types TEXT,
    unknown_types TEXT
);

CREATE INDEX idx_imported_backups_provider ON imported_backups (provider);
"""


# ── Build ─────────────────────────────────────────────────────────────────────


def _bad_entry_record(conv, index: int) -> dict:
    """A top-level entry that is not a chat object. Accounted for in the
    build's audit, never dropped silently."""
    return {
        "meta": {
            "id": _synthetic_id({"__raw__": repr(conv), "__index__": index}),
            "title": "Untitled", "create_time": None, "update_time": None,
            "message_count": 0, "preview": "", "import_status": "parse_error",
        },
        "msgs": [], "artifacts": [], "status": "parse_error",
        "synthetic": True,
        "error": f"top-level entry is {type(conv).__name__}, not an object",
        "index": index,
        "content_types": _plain_content_types(_new_content_types()),
    }


def import_record(conv, index: int, provider: str) -> dict:
    """One export entry turned into an importer record."""
    if not isinstance(conv, dict):
        return _bad_entry_record(conv, index)
    if provider == "claude":
        return import_claude_conversation(conv, index)
    return import_chatgpt_conversation(conv, index)


def iter_records(path, provider: str, include_bad: bool = False):
    """The export's records, read and parsed one chat at a time. Entries that
    are not objects are left out unless `include_bad`."""
    for i, conv in enumerate(iter_export(path)):
        if isinstance(conv, dict) or include_bad:
            yield import_record(conv, i, provider)


def add_content_types(total: dict, found: dict | None) -> None:
    """Add one record's content-type counts to a running total."""
    for group in ("dropped", "unknown"):
        bucket = total.setdefault(group, {})
        for kind, n in ((found or {}).get(group) or {}).items():
            bucket[kind] = bucket.get(kind, 0) + n


def scan_export(path, provider: str, include_bad: bool = False, *, job=None) -> dict:
    """First reading of an export, one chat at a time: which source entry
    wins each id (the most recovered messages, tie → earliest entry, the
    rule dedup_records uses), how many entries share each id, the date range
    and the content-type counts. The second reading then writes only the
    winners, so the whole export is never held in memory (AUDIT.md #31).
    Raises ValueError for a file that is not valid JSON."""
    # id -> (sort key, first date, last date) of the copy winning so far. The
    # date range only counts winning copies, as backup_date_range over
    # dedup_records did.
    winners: dict[str, tuple] = {}
    entries: Counter = Counter()
    content_types: dict = {"dropped": {}, "unknown": {}}
    total = 0
    for rec in iter_records(path, provider, include_bad):
        if job:
            job.progress("Reading export to count chats…", done=total)
        total += 1
        cid = rec["meta"]["id"]
        entries[cid] += 1
        key = (-len(rec["msgs"]), rec["index"])
        if cid not in winners or key < winners[cid][0]:
            winners[cid] = (key, *backup_date_range([rec]))
        add_content_types(content_types, rec.get("content_types"))
    lows = [lo for _key, lo, _hi in winners.values() if lo is not None]
    highs = [hi for _key, _lo, hi in winners.values() if hi is not None]
    first = min(lows) if lows else None
    last = max(highs) if highs else None
    return {
        "winners": {cid: w[0][1] for cid, w in winners.items()},
        "entries": entries,
        "total": total,
        "first": first,
        "last": last,
        "content_types": content_types,
    }


def build(source: Path, db_path: Path, *, job=None, reset=True, scan=None) -> None:
    source, db_path = Path(source), Path(db_path)
    print(f"Loading {source} …", flush=True)
    # Refuse before anything is written when the disk is too full.
    check_free_space(db_path, source.stat().st_size)

    fmt = detect_format(peek_export(source))

    # ── Pass 1: read every entry once to pick the copy kept for each id ────────
    scan = scan if scan is not None else scan_export(source, fmt, include_bad=True, job=job)
    total = scan["total"]
    winners = scan["winners"]
    print(f"{total} conversations found ({fmt} format). Indexing…", flush=True)

    db = sqlite3.connect(db_path)
    try:
        if reset:
            db.executescript(SCHEMA)
        # When this database was built. A bookmark older than the build, with no
        # message id or fingerprint, was made against an earlier database, so its
        # position cannot be trusted here (message_ids.reanchor_bookmarks).
        db.execute("CREATE TABLE IF NOT EXISTS history_build (id INTEGER PRIMARY KEY CHECK (id = 1), built_at REAL)")
        db.execute("INSERT OR REPLACE INTO history_build (id, built_at) VALUES (1, ?)", (time.time(),))
        # Recorded so the server can tell a Claude export from a ChatGPT one
        # without re-parsing the source file. Claude-only UI keys off this. It
        # sits with the other preferences in userdata.db, which a rebuild never
        # touches (renames, Archive, Recycle Bin and pins live there too).
        db.execute("ATTACH DATABASE ? AS udb", (str(app_paths.userdata_path(db_path)),))
        db.execute("CREATE TABLE IF NOT EXISTS udb.ui_preferences (pref_key TEXT PRIMARY KEY, pref_value TEXT)")
        db.execute(
            "INSERT OR REPLACE INTO udb.ui_preferences(pref_key, pref_value) VALUES ('dataset_format', ?)",
            (json.dumps(fmt),),
        )

        db.commit()
        if job:
            job.progress("Saving rebuilt chats…", done=0, total=total, force=True)

        # ── Pass 2: write the kept copy of each id, one chat at a time ─────────────
        # Every entry gets an import_sources row, not just the one that wins the
        # ID, so the audit's totals still match the export and the collapsed
        # indices stay traceable.
        status_counts: Counter = Counter()
        n_synth = 0
        parse_errors: list[tuple] = []
        duplicates: dict[str, list] = defaultdict(list)
        artifact_count = 0
        for rec in iter_records(source, fmt, include_bad=True):
            if job:
                job.check()
            i = rec["index"]
            if i % 100 == 0:
                sys.stderr.write(f"\r  {i:>5}/{total}")
                sys.stderr.flush()
            meta = rec["meta"]
            cid = meta["id"]
            kept = winners.get(cid) == i
            status_counts[rec["status"]] += 1
            n_synth += 1 if rec["synthetic"] else 0
            if rec["status"] == "parse_error":
                parse_errors.append((i, cid, meta["title"], rec["error"]))
            if scan["entries"][cid] > 1:
                duplicates[cid].append((i, meta["title"]))
            try:
                if job:
                    if not db.in_transaction:
                        db.execute("BEGIN IMMEDIATE")
                    if db.execute("SELECT 1 FROM udb.deleted_records WHERE conversation_id = ? AND provider = ?",
                                  (cid, fmt)).fetchone():
                        continue  # keep permanently deleted text out of the audit too
                db.execute(
                    "INSERT OR REPLACE INTO import_sources "
                    "(source_index, conversation_id, title, import_status, synthetic, kept, "
                    " create_time, update_time, message_count, preview) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (i, cid, meta["title"], rec["status"], 1 if rec["synthetic"] else 0,
                     1 if kept else 0, meta.get("create_time"), meta.get("update_time"),
                     meta.get("message_count"), meta.get("preview")),
                )
                if not kept:
                    continue
                # Record which top-level object in the export this row came from, so
                # an audit view can trace a DB record back to the exact source entry.
                meta["source_index"] = i
                meta.setdefault("gizmo_id", None)
                meta.setdefault("gizmo_type", None)
                # Every conversation in one build shares the file's detected provider.
                if job:
                    reconcile_backup(db, [rec], fmt, skip_binned=False)
                else:
                    _insert_conv_rows(db, meta, rec["msgs"], rec["artifacts"], fmt)
                artifact_count += len(rec["artifacts"])
            finally:
                if job:
                    job.state['progress'] = i + 1
                    job.batch(db, i + 1)
        sys.stderr.write(f"\r  {total}/{total}\n")
        print(f"Artifacts indexed: {artifact_count}")

        # ── Memories ──────────────────────────────────────────────────────────────
        memories_path = source.parent / "memories.json"
        if memories_path.exists():
            with open(memories_path, encoding="utf-8") as f:
                memories_data = json.load(f)
            if isinstance(memories_data, list):
                for entry in memories_data:
                    if job:
                        job.check()
                    content = (entry.get("conversations_memory") or "").strip()
                    if content:
                        account = entry.get("account_uuid", "")
                        db.execute(
                            "INSERT INTO memories (account_uuid, content, memory_id) VALUES (?, ?, ?)",
                            (account, content, memory_id(account, content)),
                        )
            db.commit()
            print(f"Memories indexed.")

        # ── Projects ──────────────────────────────────────────────────────────────
        projects_dir = source.parent / "projects"
        proj_count = 0
        if projects_dir.exists():
            for proj_file in sorted(projects_dir.glob("*.json")):
                if job:
                    job.check()
                with open(proj_file, encoding="utf-8") as f:
                    proj = json.load(f)
                pid = proj.get("uuid")
                if not pid:
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO projects VALUES (?, ?, ?, ?, ?)",
                    (
                        pid,
                        (proj.get("name") or "Untitled Project").strip(),
                        (proj.get("description") or "").strip(),
                        _iso_to_ts(proj.get("created_at") or ""),
                        _iso_to_ts(proj.get("updated_at") or ""),
                    ),
                )
                for doc in (proj.get("docs") or []):
                    doc_id = doc.get("uuid")
                    if not doc_id:
                        continue
                    db.execute(
                        "INSERT OR REPLACE INTO project_docs VALUES (?, ?, ?, ?, ?)",
                        (
                            doc_id,
                            pid,
                            (doc.get("filename") or "").strip(),
                            (doc.get("content") or "").strip(),
                            _iso_to_ts(doc.get("created_at") or ""),
                        ),
                    )
                proj_count += 1
                if job:
                    job.batch(db, proj_count)
            print(f"{proj_count} project(s) indexed.")

        # ── Record the seed export in the import history ──────────────────────────
        # The "Import New Chats" screen skips any file whose hash it has already
        # imported and renames the rest to Provider-conversations-DATE.json. Without
        # this row the seed source/conversations.json is treated as a brand-new
        # backup, imported again and renamed away — which then breaks the documented
        # `rm -f history.db && python3 app.py` rebuild, since startup needs that
        # exact path. Recording its hash here keeps the seed in place while still
        # letting a genuinely new export (different bytes) import normally.
        if job:
            job.check()
        db_total = len(winners)               # distinct records actually stored
        try:
            db.execute(
                "INSERT INTO imported_backups "
                "(file_name, provider, file_hash, first_chat, last_chat, "
                " total_chats, imported_at, added, updated, unchanged, skipped, errors, "
                " dropped_types, unknown_types) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?)",
                (source.name, fmt, file_sha256(source, job=job), scan["first"], scan["last"],
                 db_total, datetime.now(timezone.utc).timestamp(), db_total,
                 json.dumps(scan["content_types"]["dropped"]),
                 json.dumps(scan["content_types"]["unknown"])),
            )
        except OSError:
            pass  # can't hash the seed → it may re-import later, but the build stands

        db.commit()

    except BaseException as exc:
        import background_jobs
        if isinstance(exc, background_jobs.Cancelled):
            db.commit()
        else:
            db.rollback()
        raise
    finally:
        db.close()

    # ── Import audit ───────────────────────────────────────────────────────────
    n_normal   = status_counts["normal"]
    n_fallback = status_counts["fallback"]
    n_meta     = status_counts["metadata_only"]
    n_error    = status_counts["parse_error"]
    dup_extra  = sum(len(entries) - 1 for entries in duplicates.values())

    print("\n─── Import audit ───────────────────────────")
    print(f"Raw conversations: {total}")
    print(f"Normal imports:    {n_normal}")
    print(f"Fallback imports:  {n_fallback}")
    print(f"Metadata only:     {n_meta}")
    print(f"Synthetic IDs:     {n_synth}")
    print(f"Parse errors:      {n_error}")
    print(f"Database total:    {db_total}")
    print("────────────────────────────────────────────")

    # Every raw record carries exactly one status — these must sum to the total.
    accounted = n_normal + n_fallback + n_meta + n_error
    if accounted != total:
        print(f"WARNING: status counts ({accounted}) do not sum to raw total ({total}).")

    # Report duplicates explicitly instead of silently removing them.
    if duplicates:
        print(f"\nDuplicate source records: {len(duplicates)} ID(s), "
              f"{dup_extra} extra record(s) collapsed:")
        for cid, entries in duplicates.items():
            idxs = ", ".join(str(i) for i, _title in entries)
            print(f"  id={cid}  title={entries[0][1]!r}  source indices=[{idxs}]")
        print(f"(Database total {db_total} + {dup_extra} duplicate(s) = {total} raw.)")

    # Any parse error names its source record so it can never disappear silently.
    if n_error:
        print(f"\nParse errors ({n_error}):")
        for i, cid, title, error in parse_errors:
            print(f"  index={i}  id={cid}  title={title!r}  error={error}")

    # Content the viewer leaves out, so a change in the export format shows.
    for group, label in (("dropped", "Left out on purpose"),
                         ("unknown", "Unknown content types (not shown)")):
        found = scan["content_types"][group]
        if found:
            print(f"\n{label}: " + ", ".join(f"{k} {n}" for k, n in sorted(found.items())))

    print(f"\nDone — {db_total} conversations indexed → {db_path}")


# ── Incremental import (the "Import New Chats" screen) ─────────────────────────
#
# These reuse the same per-conversation parsers as the full build, but merge one
# backup file at a time into an existing database instead of rebuilding it.


def parse_backup(data: list, provider: str) -> list:
    """Parse every conversation object in one backup into importer records."""
    records: list[dict] = []
    for i, conv in enumerate(data):
        if not isinstance(conv, dict):
            continue
        if provider == "claude":
            records.append(import_claude_conversation(conv, i))
        else:
            records.append(import_chatgpt_conversation(conv, i))
    return records


def dedup_records(records: list) -> list:
    """Collapse exact-duplicate IDs within a single backup, keeping the record
    with the most recovered messages (tie → earliest source index) — the same
    rule the full build uses."""
    by_id: dict[str, list] = defaultdict(list)
    for rec in records:
        by_id[rec["meta"]["id"]].append(rec)
    out = []
    for _cid, recs in by_id.items():
        out.append(sorted(recs, key=lambda r: (-len(r["msgs"]), r["index"]))[0])
    return out


def backup_date_range(records: list) -> tuple[float | None, float | None]:
    """Earliest and latest timestamp across every conversation and message in a
    backup. Used for the import-history First/Last columns and for the date in
    the renamed filename (which is the latest message timestamp)."""
    times: list[float] = []
    for r in records:
        m = r["meta"]
        for t in (m.get("create_time"), m.get("update_time")):
            if t:
                times.append(t)
        for msg in r["msgs"]:
            t = msg.get("create_time")
            if t:
                times.append(t)
    if not times:
        return None, None
    return min(times), max(times)


def _delete_conv_rows(conn, cid: str) -> None:
    """Remove a conversation's derived rows before re-inserting on update. The
    conversations row itself is INSERT OR REPLACEd, so it is left in place."""
    conn.execute("DELETE FROM messages WHERE conversation_id = ?", (cid,))
    conn.execute("DELETE FROM search_index WHERE conversation_id = ?", (cid,))
    conn.execute("DELETE FROM artifacts WHERE conv_id = ?", (cid,))


def _insert_conv_rows(conn, meta: dict, msgs: list, artifacts: list, provider: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO conversations "
        "(id, title, create_time, update_time, message_count, preview, import_status, source_index, provider, gizmo_id, gizmo_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            meta["id"], meta["title"], meta.get("create_time"), meta.get("update_time"),
            meta.get("message_count"), meta.get("preview"),
            meta.get("import_status", "normal"), meta.get("source_index"), provider,
            meta.get("gizmo_id"), meta.get("gizmo_type"),
        ),
    )
    for m in msgs:
        conn.execute(
            "INSERT INTO messages "
            "(conversation_id, role, content, attachments, artifact_ids, siblings, branch_index, create_time, seq, model_slug, message_uuid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                m["conversation_id"], m.get("role"), m.get("content"), m.get("attachments"),
                m.get("artifact_ids"), m.get("siblings"), m.get("branch_index", 1),
                m.get("create_time"), m.get("seq"), m.get("model_slug"), m.get("message_uuid"),
            ),
        )
    conn.execute(
        "INSERT INTO search_index (conversation_id, title, body) VALUES (?, ?, ?)",
        (meta["id"], meta["title"], "\n".join(m["content"] for m in msgs)),
    )
    for a in artifacts:
        conn.execute(
            "INSERT OR REPLACE INTO artifacts (id, conv_id, msg_seq, title, type, lang, content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (a["id"], a["conv_id"], a.get("msg_seq"), a.get("title"),
             a.get("type"), a.get("lang"), a.get("content")),
        )


def _ts_close(a, b) -> bool:
    """Two conversation timestamps that mean the same instant. Exports round to
    the second, so anything under a second apart (and two missing values) is
    treated as equal."""
    if not a and not b:
        return True
    if not a or not b:
        return False
    return abs(float(a) - float(b)) < 1.0


def _fill_message_ids(conn, cid: str, msgs: list) -> None:
    conn.executemany(
        "UPDATE messages SET message_uuid = ? WHERE conversation_id = ? AND message_uuid IS NULL "
        "AND seq = ? AND role = ? AND content = ?",
        [(m["message_uuid"], cid, m.get("seq"), m.get("role"), m.get("content"))
         for m in msgs if m.get("message_uuid")],
    )


def _bookmarks_before_replace(conn, cid: str):
    """message_ids.bookmarks_before_replace, when the user-data database is
    attached (it is not during a plain build)."""
    import message_ids
    try:
        return message_ids.bookmarks_before_replace(conn, cid)
    except sqlite3.Error:
        return None


def _bookmarks_after_replace(conn, cid: str, remembered) -> None:
    if remembered is None:
        return
    import message_ids
    try:
        message_ids.bookmarks_after_replace(conn, cid, remembered)
    except sqlite3.Error:
        pass


def reconcile_backup(conn, records: list, provider: str, skip_binned: bool = True) -> dict:
    """
    Merge one backup's records into an existing database, honoring conversation
    identity (id + provider) and the deletion flag.

      • UUID not present            → insert                     (added)
      • present, other provider     → skip, never overwrite      (skipped)
      • present, is_deleted = true  → skip, never resurrect      (skipped)
      • present, is_deleted = false → reconcile if content moved (updated)
                                       otherwise leave it alone   (unchanged)

    A rebuild passes skip_binned=False: a chat in the Recycle Bin then gets
    its newest copy too, and stays in the Recycle Bin (the flag is in
    userdata.db and is not touched here).

    A backup that overlaps an older one therefore updates in place and never
    duplicates a conversation, and a conversation the user deleted stays gone.
    Identity is (id, provider): a same UUID held by the other provider is a
    different conversation and is never touched, and an older backup imported
    after a newer one never rolls the stored conversation backward.
    """
    counts = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0, "errors": 0}
    # Each chat is written inside its own savepoint (AUDIT.md #8): a chat that
    # fails part-way is put back exactly as it was, and the rest carry on. The
    # caller commits between batches, never inside a chat savepoint.
    if not conn.in_transaction:
        conn.execute("BEGIN")
    for r in records:
        conn.execute("SAVEPOINT import_chat")
        try:
            _reconcile_one(conn, r, provider, skip_binned, counts)
        except Exception as exc:  # noqa: BLE001 — one bad record must not abort the merge
            conn.execute("ROLLBACK TO SAVEPOINT import_chat")
            counts["errors"] += 1
            import errorlog
            meta = r.get("meta") if isinstance(r, dict) else None
            errorlog.record_exception(
                exc, action="Import a chat",
                ids={"conversation": (meta or {}).get("id"), "provider": provider},
            )
        finally:
            conn.execute("RELEASE SAVEPOINT import_chat")
    return counts


def _reconcile_one(conn, r: dict, provider: str, skip_binned: bool, counts: dict) -> None:
    """reconcile_backup for one record: adds 1 to the count it ends in."""
    meta = r["meta"]
    cid = meta["id"]
    msgs = r["msgs"]
    artifacts = r["artifacts"]
    existing = conn.execute(
        "SELECT title, update_time, message_count, provider, gizmo_id, gizmo_type FROM conversations WHERE id = ?",
        (cid,),
    ).fetchone()
    if existing is None:
        _insert_conv_rows(conn, meta, msgs, artifacts, provider)
        counts["added"] += 1
        return
    # A conversation ID that already belongs to the *other* provider is a
    # distinct conversation that happens to share a UUID. Overwriting it
    # would silently destroy the first provider's chat, so leave it be.
    existing_provider = existing[3]
    if existing_provider and existing_provider != provider:
        counts["skipped"] += 1
        return
    delrow = conn.execute(
        "SELECT deleted FROM udb.conversation_meta WHERE conversation_id = ?",
        (cid,),
    ).fetchone() if skip_binned else None
    if delrow is not None and delrow[0]:
        counts["skipped"] += 1
        return
    same = (
        (existing[0] or "") == (meta["title"] or "")
        and _ts_close(existing[1], meta.get("update_time"))
        and (existing[2] or 0) == (meta.get("message_count") or 0)
        and (existing[4] or None) == (meta.get("gizmo_id") or None)
        and (existing[5] or None) == (meta.get("gizmo_type") or None)
    )
    if same:
        # Unchanged, but this backup may carry message ids the stored
        # copy lacks (imported before ids were kept): fill them in,
        # matching position, role and exact text.
        _fill_message_ids(conn, cid, msgs)
        counts["unchanged"] += 1
        return
    # Never let an older backup roll a newer conversation backward: files
    # are scanned alphabetically, not chronologically, so a stale snapshot
    # can arrive after a fresh one. Only overwrite when the incoming
    # record is at least as new as the stored one. A record with no
    # update_time (or when the stored one has none) can't be ordered, so
    # fall through to the normal reconcile.
    inc_ut = meta.get("update_time")
    if (
        inc_ut is not None
        and existing[1] is not None
        and not _ts_close(inc_ut, existing[1])
        and float(inc_ut) < float(existing[1])
    ):
        counts["unchanged"] += 1
        return
    # Bookmarks on this chat learn their message's id before the
    # messages are replaced, so none ends up on a different message.
    remembered = _bookmarks_before_replace(conn, cid)
    _delete_conv_rows(conn, cid)
    _insert_conv_rows(conn, meta, msgs, artifacts, provider)
    _bookmarks_after_replace(conn, cid, remembered)
    counts["updated"] += 1


if __name__ == "__main__":
    import argparse
    import app_paths

    ap = argparse.ArgumentParser(description="Build ChatGPT history search database")
    ap.add_argument("--source", default=str(app_paths.SOURCE_DIR / "conversations.json"),
                    help="Path to conversations.json  (default: source/conversations.json)")
    ap.add_argument("--db",     default=str(app_paths.DB_PATH),
                    help="Output SQLite database path  (default: history.db)")
    args = ap.parse_args()
    build(Path(args.source), Path(args.db))
