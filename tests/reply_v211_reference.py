"""Frozen v2.1.1 reply reference, copied via git show (1672e14).

Only test-injected I/O and synthetic cards are used. Keep resolver bodies unchanged.
"""

from typing import Optional

from pathlib import Path

def find_by_message(chat_id, message_id) -> Optional[dict]:
    if message_id is None:
        return None
    for card in reversed(list_cards(chat_id)):
        if message_id in card.get("message_ids", []):
            return card
    return None

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

def handle_photo(token: str, chat_id: int, message: dict, photos: list, data_dir: Path) -> Optional[tuple[int, str]]:
    media_group_id = message.get("media_group_id")
    reply_card = cards.find_by_message(chat_id, (message.get("reply_to_message") or {}).get("message_id"))
    back_target = None
    if not media_group_id:
        if reply_card and reply_card.get("awaiting_back"):
            back_target = reply_card
        else:
            waiting = [c for c in cards.list_cards(chat_id, cards.REVIEW_STATUSES) if c.get("awaiting_back")]
            back_target = waiting[-1] if len(waiting) == 1 else None
    if back_target:
        back_target["awaiting_back"] = False  # the next photo must not also go to this card
        cards.save_card(back_target)
        submit(process_back_photo, token, chat_id, photos, data_dir, back_target)
        return None
    # Card creation happens here, in update order, so card numbers follow the
    # album; image prep, OCR and the model calls run on the worker pool.
    group = (chat_id, str(media_group_id)) if media_group_id else None
    card = cards.new_card(chat_id, media_group_id=group[1] if group else None)
    if group:
        _ensure_group_progress(token, group)
        submit(process_front_photo, token, chat_id, photos, data_dir, card, None, True, group=group)
        return group
    status_id = send_status(token, chat_id, f"{cards.label(card)} 처리 중: 이미지 보정 · OCR")
    cards.remember_message(card, status_id)
    cards.save_card(card)
    submit(process_front_photo, token, chat_id, photos, data_dir, card, status_id)
    return None

def handle_text(token: str, chat_id: int, message: dict) -> None:
    text = (message.get("text") or message.get("caption") or "").strip()
    if not text:
        send_message(token, chat_id, "명함 사진을 보내주세요.")
        return
    if text.lower() in HELP_WORDS:
        send_message(token, chat_id, HELP_TEXT)
        return
    if text.lower() in LIST_WORDS:
        send_open_list(token, chat_id)
        return

    card = select_card(chat_id, message_id=(message.get("reply_to_message") or {}).get("message_id"))
    if card is None:
        open_cards = cards.list_cards(chat_id, cards.REVIEW_STATUSES)
        if len(open_cards) == 1:
            card = open_cards[0]
        elif not open_cards:
            send_message(token, chat_id, "명함 사진을 보내주세요. (사용법: 도움말)")
            return
        else:
            send_message(token, chat_id, f"검토 중인 명함이 {len(open_cards)}건입니다. 고칠 명함의 초안 메시지에 답장으로 보내주세요.")
            send_open_list(token, chat_id)
            return
    if card["status"] not in cards.REVIEW_STATUSES:
        send_message(token, chat_id, f"{cards.label(card)}은(는) 이미 {'저장' if card['status'] == 'saved' else '종료'}된 명함입니다.")
        return
    status_id = send_status(token, chat_id, f"{cards.label(card)} 처리 중: 초안 수정")
    ok, note = pipeline.edit(card, text)
    if not ok:
        edit_message(token, chat_id, status_id, f"{cards.label(card)} {note}")
        _remember_card_message(card, status_id)
        return
    show_draft(token, chat_id, card, status_id, prefix=f"{cards.label(card)} {note}")
