"""알림 발송. 설정된 채널만 보낸다. 실패해도 파이프라인은 멈추지 않는다."""
from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

from .config import Settings

log = logging.getLogger(__name__)


TELEGRAM_LIMIT = 4096


def split_telegram(text: str, limit: int = TELEGRAM_LIMIT - 100) -> list[str]:
    """줄 단위로 끊어 한도 안에 들어가게 나눈다. 섹션 제목 바로 뒤에서 끊기지 않게 빈 줄을 우선 경계로 쓴다."""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        piece = para if not cur else "\n\n" + para
        if len(cur) + len(piece) <= limit:
            cur += piece
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        # 한 문단이 그 자체로 너무 길면 줄 단위로
        for line in para.split("\n"):
            add = line if not cur else "\n" + line
            if len(cur) + len(add) > limit:
                chunks.append(cur)
                cur = line
            else:
                cur += add
    if cur:
        chunks.append(cur)
    return chunks


def send_telegram(settings: Settings, text: str, chat_id: str | None = None, parse_mode: str = "HTML") -> bool:
    """chat_id 는 개인 채팅 id, 채널 '@아이디', 또는 비공개 채널 '-100...' 모두 가능.
    봇이 채널에 글을 쓰려면 채널 관리자로 추가돼 있어야 한다."""
    chat_id = chat_id or settings.telegram_chat_id
    if not (settings.telegram_bot_token and chat_id):
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    ok = True
    for chunk in split_telegram(text):
        payload = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 400 and parse_mode:
                # 서식 오류면 평문으로 한 번 더
                log.warning("텔레그램 HTML 파싱 거부, 평문으로 재시도: %s", r.text[:200])
                payload.pop("parse_mode")
                r = requests.post(url, json=payload, timeout=20)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            log.error("텔레그램 발송 실패(%s): %s", chat_id, e)
            ok = False
    return ok


def send_email(settings: Settings, subject: str, markdown_body: str) -> bool:
    if not (settings.smtp_host and settings.mail_to):
        return False
    try:
        import markdown as md  # 선택 의존성. 없으면 평문으로 보낸다.
        html = md.markdown(markdown_body)
    except ImportError:
        html = "<pre>" + markdown_body + "</pre>"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.mail_from or settings.smtp_user
    msg["To"] = ", ".join(settings.mail_to)
    msg.attach(MIMEText(markdown_body, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
            s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.sendmail(msg["From"], settings.mail_to, msg.as_string())
        return True
    except Exception as e:  # noqa: BLE001
        log.error("이메일 발송 실패: %s", e)
        return False
