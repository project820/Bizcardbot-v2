"""Build a chat-scoped CSV in memory; never create a plaintext export file."""

from __future__ import annotations

import csv
import io
from datetime import datetime

from . import cards
from .draft import CardDraft
from .metrics import _known_cost

HEADERS = ("받은 날짜", "이름", "직함", "회사", "휴대전화", "회사전화", "팩스", "이메일",
           "웹사이트", "주소", "메모", "만남 메모", "신규/갱신", "AI 비용(원)")


def _safe(value) -> str:
    text = str(value) if value is not None else ""
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def _received_date(card: dict) -> str:
    try:
        value = card.get("created_at") or ""
        try:
            created = datetime.fromisoformat(value)
        except ValueError:
            created = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
        return cards.followup_localtime(created.timestamp()).date().isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


def build(chat_id) -> tuple[bytes, int]:
    saved = cards.list_cards(chat_id, ["saved"])
    output = io.StringIO(newline="")
    writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(HEADERS)
    for card in saved:
        draft = card.get("draft") or {}
        if not isinstance(draft, dict):
            draft = CardDraft.from_session_value(draft).to_dict()
        cost = (card.get("cost") or {}).get("krw")
        values = [_received_date(card)] + [draft.get(key) or "" for key in ("name", "title", "company")]
        for key in ("mobile", "work", "fax", "email", "website", "address"):
            value = draft.get(key) or []
            values.append(" / ".join(str(item) for item in value) if isinstance(value, list) else str(value))
        values += [draft.get("notes") or "", card.get("caption") or "",
                   {"created": "신규", "updated": "갱신"}.get(card.get("save_action"), ""),
                   f"{cost:.2f}" if _known_cost(cost) else ""]
        writer.writerow([_safe(value) for value in values])
    return output.getvalue().encode("utf-8-sig"), len(saved)
