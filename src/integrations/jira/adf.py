"""Atlassian Document Format (ADF) helpers for Jira Cloud REST v3.

Jira Cloud ``/rest/api/3`` comments and rich-text fields use ADF JSON, not
wiki markup or Markdown. See:
https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Union


def adf_to_text(node: Any) -> str:
    """Best-effort plain text from ADF (preserves headings, lists, code fences)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(p for p in (adf_to_text(x) for x in node) if p)
    if not isinstance(node, dict):
        return str(node)

    ntype = node.get("type") or ""
    children = node.get("content") or []

    if ntype == "text":
        return str(node.get("text") or "")
    if ntype == "hardBreak":
        return "\n"
    if ntype == "mention":
        return str((node.get("attrs") or {}).get("text") or "@user")
    if ntype == "emoji":
        return str((node.get("attrs") or {}).get("shortName") or "")
    if ntype == "inlineCard":
        return str((node.get("attrs") or {}).get("url") or "")

    if ntype == "heading":
        level = int((node.get("attrs") or {}).get("level") or 2)
        hashes = "#" * max(1, min(level, 6))
        inner = "".join(adf_to_text(c) for c in children).strip()
        return f"\n{hashes} {inner}\n" if inner else ""

    if ntype == "paragraph":
        inner = "".join(adf_to_text(c) for c in children)
        return f"{inner}\n" if inner else "\n"

    if ntype == "codeBlock":
        lang = str((node.get("attrs") or {}).get("language") or "").strip()
        inner = "".join(adf_to_text(c) for c in children)
        fence = f"```{lang}".rstrip()
        return f"\n{fence}\n{inner}\n```\n"

    if ntype == "blockquote":
        inner = adf_to_text(children).strip()
        quoted = "\n".join(f"> {line}" for line in inner.splitlines() or [""])
        return f"\n{quoted}\n"

    if ntype in ("bulletList", "orderedList"):
        lines: List[str] = []
        for i, item in enumerate(children, start=1):
            item_text = adf_to_text(item).strip()
            if not item_text:
                continue
            # listItem may contain paragraphs; flatten first line as bullet
            first, *rest = item_text.splitlines()
            prefix = f"{i}. " if ntype == "orderedList" else "- "
            lines.append(f"{prefix}{first}")
            for r in rest:
                lines.append(f"  {r}")
        return ("\n".join(lines) + "\n") if lines else ""

    if ntype == "listItem":
        return adf_to_text(children)

    if ntype == "rule":
        return "\n---\n"

    if ntype in ("table", "tableRow", "tableCell", "tableHeader"):
        return adf_to_text(children)

    if ntype == "doc":
        parts = [adf_to_text(c) for c in children]
        text = "".join(parts)
        # Collapse excess blank lines but keep structure
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + ("\n" if text.strip() else "")

    # Default: recurse
    return "".join(adf_to_text(c) for c in children)


def _text_node(text: str, marks: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    node: Dict[str, Any] = {"type": "text", "text": text}
    if marks:
        node["marks"] = list(marks)
    return node


def _inline_from_plain(text: str) -> List[Dict[str, Any]]:
    """Split plain text into ADF inline nodes (``code`` and *strong* spans)."""
    if not text:
        return []
    nodes: List[Dict[str, Any]] = []
    # `code` then *bold* — simple non-nested pass
    pattern = re.compile(r"`([^`]+)`|\*([^*]+)\*")
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            nodes.append(_text_node(text[pos : m.start()]))
        if m.group(1) is not None:
            nodes.append(_text_node(m.group(1), [{"type": "code"}]))
        else:
            nodes.append(_text_node(m.group(2), [{"type": "strong"}]))
        pos = m.end()
    if pos < len(text):
        nodes.append(_text_node(text[pos:]))
    return nodes or [_text_node(text)]


def _paragraph(text: str) -> Dict[str, Any]:
    return {"type": "paragraph", "content": _inline_from_plain(text) or [_text_node("")]}


def _heading(text: str, level: int = 3) -> Dict[str, Any]:
    return {
        "type": "heading",
        "attrs": {"level": max(1, min(int(level), 6))},
        "content": _inline_from_plain(text) or [_text_node(text)],
    }


def _bullet_list(items: Sequence[str]) -> Dict[str, Any]:
    content = []
    for item in items:
        content.append(
            {
                "type": "listItem",
                "content": [_paragraph(item)],
            }
        )
    return {"type": "bulletList", "content": content}


def report_markup_to_adf(text: str) -> Dict[str, Any]:
    """Convert NFE lightweight report markup into a Jira Cloud ADF document.

    Supported lines:
    - ``## Heading`` / ``### Heading`` / ``h3. Heading`` → heading
    - ``* item`` / ``- item`` → bullet list (grouped)
    - blank line → block separator
    - otherwise → paragraph

    Inline: `` `code` `` and ``*bold*``.
    """
    raw = (text or "").replace("\r\n", "\n").strip()
    if not raw:
        return {"type": "doc", "version": 1, "content": [_paragraph("")]}

    blocks: List[Dict[str, Any]] = []
    bullet_buf: List[str] = []

    def flush_bullets() -> None:
        nonlocal bullet_buf
        if bullet_buf:
            blocks.append(_bullet_list(bullet_buf))
            bullet_buf = []

    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped:
            flush_bullets()
            continue

        h3 = re.match(r"^h([1-6])\.\s+(.*)$", stripped, re.I)
        md_h = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if h3:
            flush_bullets()
            blocks.append(_heading(h3.group(2).strip(), int(h3.group(1))))
            continue
        if md_h:
            flush_bullets()
            blocks.append(_heading(md_h.group(2).strip(), len(md_h.group(1))))
            continue

        bullet = re.match(r"^[\*\-]\s+(.*)$", stripped)
        if bullet:
            bullet_buf.append(bullet.group(1).strip())
            continue

        flush_bullets()
        blocks.append(_paragraph(stripped))

    flush_bullets()
    if not blocks:
        blocks = [_paragraph(raw[:4000])]
    return {"type": "doc", "version": 1, "content": blocks}


def adf_doc(content: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "doc", "version": 1, "content": list(content)}
