"""Shared card-chat wording and selection; Telegram reexports these names."""

from typing import Optional
from datetime import datetime
import re
from . import cards, pipeline
from .contacts import normalize_phone


LIST_WORDS = {"목록", "/list", "list", "대기", "대기목록", "/pending"}


HELP_WORDS = {"/start", "/help", "도움말", "help", "사용법"}


HELP_TEXT = (
    "명함 사진을 보내주세요. 여러 장을 한 번에 보내도 명함마다 따로 초안을 만듭니다.\n\n"
    "- 고치기: 초안 메시지에 답장으로 '항목: 값'을 보내세요. 예) 직함: 과장\n"
    "  (이름, 직함, 직함원문, 회사, 휴대전화, 회사전화, 팩스, 이메일, 웹사이트, 주소, 메모. 비우려면 '팩스: -')\n"
    "- 문장으로 써도 됩니다. 예) 이메일은 kim@acme.com 이야\n"
    "- '목록': 저장 안 된 명함과 일괄 저장 버튼\n"
    "- 사진 캡션: 만남 메모로 추가\n"
    "- CSV 내보내기 버튼 또는 /export: 저장한 명함 파일\n"
    "- /alerts 또는 '알림 설정': 후속 알림 켜기·끄기"
)


def batch_summary(members: list[dict], scope: str, title: str) -> tuple[str, Optional[dict]]:
    ready = [c for c in members if c["status"] in cards.REVIEW_STATUSES and pipeline.current_draft(c).is_savable and not c.get("job_change")]
    needs_input = [c for c in members if c["status"] in cards.REVIEW_STATUSES and not pipeline.current_draft(c).is_savable]
    saved = [c for c in members if c["status"] == "saved"]
    failed = [c for c in members if c["status"] == "save_failed"]
    lines = [f"{title} {len(members)}건: 저장 가능 {len(ready)} · 입력 필요 {len(needs_input)} · 저장됨 {len(saved)}"]
    for card in members:
        draft = pipeline.current_draft(card)
        mark = {"saved": "저장됨", "saving": "저장 중", "save_failed": "저장 보류", "cancelled": "취소", "processing": "처리 중", "needs_confirmation": "저장 확인 필요"}.get(card["status"])
        if card["status"] == "review":
            mark = "갱신 선택 필요" if card.get("job_change") else ("확인 필요" if (draft.warnings or not draft.is_savable) else "준비")
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
        if card.get("note") and (card.get("resource_name") or "").startswith("apple:"):
            try:
                from . import meeting_note
                if meeting_note.enabled() and meeting_note.status().get("automation") != "authorized":
                    lines.append("참고: 메모는 Mac 권한을 허용하면 자동으로 추가합니다.")
            except Exception:
                pass
        return "\n".join(lines)
    if outcome.get("needs_confirmation"):
        return f"{cards.label(card)} 저장 확인 필요: {name}\n{outcome['message']}"
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


def followup_text(card: dict) -> str:
    draft = pipeline.current_draft(card)
    try:
        meeting = cards.followup_localtime(datetime.fromisoformat(card["created_at"]).timestamp()).strftime("%Y-%m-%d")
    except (KeyError, ValueError):
        meeting = cards.followup_localtime(card["followup_saved_at"]).strftime("%Y-%m-%d")
    recipient = " ".join(value.strip() for value in (draft.company, draft.name) if value.strip())
    heading_name = " ".join(value.strip() for value in (draft.name, draft.company) if value.strip())
    heading = f"{heading_name} 님께 연락할까요?"
    if re.search(r"[가-힣ㄱ-ㅎㅏ-ㅣ]", draft.name):
        greeting = (f"안녕하세요, {recipient}님. {meeting}에 인사드렸습니다. "
                    "만나 뵙게 되어 반가웠습니다. 앞으로 좋은 인연으로 이어가면 좋겠습니다. 감사합니다.")
    elif re.search(r"[\u3040-\u30ff\uff66-\uff9f]", draft.name):
        greeting = (f"{recipient}様、こんにちは。{meeting}はありがとうございました。"
                    "今後ともよろしくお願いいたします。")
    elif re.search(r"[\u3400-\u9fff\U00020000-\U0003134f]", draft.name):
        traditional = any(normalize_phone(number).startswith(("886", "852")) for number in draft.mobile + draft.work)
        # Unambiguous traditional forms common in names and organization text.
        traditional = traditional or bool(re.search(r"[陳張劉黃趙吳鄭馮馬羅楊葉謝許鄧蕭賴鍾龔蘇盧蔣賈顧錢孫萬華國臺灣東門廣業貿經會團體製藝學醫龍麗寶聲樂禮偉榮達興愛]", draft.name + draft.company + draft.title))
        if traditional:
            greeting = (f"{recipient}，您好！很高興於{meeting}與您見面。"
                        "期待今後保持聯繫，謝謝！")
        else:
            greeting = (f"{recipient}，您好！很高兴于{meeting}与您见面。"
                        "期待今后保持联系，谢谢！")
    else:
        greeting = (f"Hello {draft.name}, it was a pleasure meeting you on {meeting}. "
                    "I look forward to staying in touch. Best regards.")
    return heading + "\n\n길게 눌러 아래 인사말을 복사하세요.\n\n" + greeting
