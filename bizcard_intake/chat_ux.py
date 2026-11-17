"""Shared card-chat wording and selection; Telegram reexports these names."""

from typing import Optional
from . import cards, pipeline


LIST_WORDS = {"목록", "/list", "list", "대기", "대기목록", "/pending"}


HELP_WORDS = {"/start", "/help", "도움말", "help", "사용법"}


HELP_TEXT = (
    "명함 사진을 보내주세요. 여러 장을 한 번에 보내도 명함마다 따로 초안을 만듭니다.\n\n"
    "- 고치기: 초안 메시지에 답장으로 '항목: 값'을 보내세요. 예) 직함: 과장\n"
    "  (이름, 직함, 직함원문, 회사, 휴대전화, 회사전화, 팩스, 이메일, 웹사이트, 주소, 메모. 비우려면 '팩스: -')\n"
    "- 문장으로 써도 됩니다. 예) 이메일은 kim@acme.com 이야\n"
    "- '목록': 저장 안 된 명함과 일괄 저장 버튼"
)


def batch_summary(members: list[dict], scope: str, title: str) -> tuple[str, Optional[dict]]:
    ready = [c for c in members if c["status"] in cards.REVIEW_STATUSES and pipeline.current_draft(c).is_savable]
    needs_input = [c for c in members if c["status"] in cards.REVIEW_STATUSES and not pipeline.current_draft(c).is_savable]
    saved = [c for c in members if c["status"] == "saved"]
    failed = [c for c in members if c["status"] == "save_failed"]
    lines = [f"{title} {len(members)}건: 저장 가능 {len(ready)} · 입력 필요 {len(needs_input)} · 저장됨 {len(saved)}"]
    for card in members:
        draft = pipeline.current_draft(card)
        mark = {"saved": "저장됨", "save_failed": "저장 보류", "cancelled": "취소", "processing": "처리 중"}.get(card["status"])
        if card["status"] == "review":
            mark = "확인 필요" if (draft.warnings or not draft.is_savable) else "준비"
        lines.append(f"- {cards.label(card)} {draft.display_name or '(이름 없음)'} · {mark}")
    if failed:
        lines.append(f"\n저장 보류 {len(failed)}건은 자동으로 다시 시도합니다.")
    markup = None
    if ready:
        markup = {"inline_keyboard": [[{"text": f"저장 가능한 {len(ready)}건 모두 저장", "callback_data": f"ba:{scope}"[:64]}]]}
    return "\n".join(lines), markup


def save_result_text(card: dict, outcome: dict, retried: bool = False) -> str:
    name = pipeline.current_draft(card).display_name
    prefix = "자동 재시도 " if retried else ""
    if outcome.get("already"):
        return f"{cards.label(card)} {outcome['message']}"
    if outcome["ok"]:
        lines = [f"{cards.label(card)} {prefix}저장 완료", outcome["message"]]
        if outcome.get("photo") == "failed":
            lines.append("연락처 사진은 올리지 못해 잠시 후 다시 올립니다.")
        elif outcome.get("photo") == "missing":
            lines.append("연락처 사진 파일이 없어 사진 없이 저장했습니다.")
        for warning in outcome.get("warnings") or []:
            if "사진" not in warning:
                lines.append(f"참고: {warning}")
        return "\n".join(lines)
    if outcome.get("stale") or outcome.get("invalid"):
        return f"{cards.label(card)} {outcome['message']}"
    hint = {
        "auth": "Mac mini에서 Google 로그인 후 자동으로 저장합니다.",
        "transient": "초안은 보관했고 잠시 후 자동으로 다시 저장합니다.",
        "unknown_outcome": "저장되었는지 확인한 뒤 다시 시도합니다. 같은 연락처가 중복되지 않도록 기존 연락처를 먼저 찾습니다.",
    }.get(outcome.get("kind"), "초안은 보관했습니다. '지금 다시 저장'을 누르거나 자동 재시도를 기다려주세요.")
    return f"{cards.label(card)} {prefix}저장 보류: {name}\n사유: {outcome['message']}\n{hint}"


def edit_prompt(card: dict) -> str:
    draft = pipeline.current_draft(card)
    current = [
        f"이름: {draft.name}",
        f"직함: {draft.short_title}",
        f"회사: {draft.company}",
        f"휴대전화: {', '.join(draft.mobile)}",
        f"회사전화: {', '.join(draft.work)}",
        f"이메일: {', '.join(draft.email)}",
    ]
    return (
        f"{cards.label(card)} 바꿀 줄만 이 메시지에 답장으로 보내주세요. (AI 없이 그대로 반영)\n"
        "아래를 복사해 고쳐 보내도 됩니다. 비우려면 '팩스: -'\n\n" + "\n".join(current)
    )


def select_card(chat_id, *, message_id=None, seq=None):
    """Resolve only this channel's card; ambiguous selections return None."""
    if seq is not None:
        return next((c for c in cards.list_cards(chat_id, cards.REVIEW_STATUSES) if c.get("seq") == seq), None)
    if message_id is not None:
        card = cards.find_by_message(chat_id, message_id)
        if card is not None:
            return card
    open_cards = cards.list_cards(chat_id, cards.REVIEW_STATUSES)
    return open_cards[0] if len(open_cards) == 1 else None
