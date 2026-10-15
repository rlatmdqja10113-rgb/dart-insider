"""환경변수 기반 설정. .env 파일이 있으면 읽어들인다."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def today_kst() -> date:
    """서버가 UTC(GitHub Actions 등)여도 한국 날짜로 집계한다."""
    return datetime.now(KST).date()

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


@dataclass
class Settings:
    dart_api_key: str = os.getenv("DART_API_KEY", "")
    db_path: Path = Path(os.getenv("INSIDER_DB", str(ROOT / "data" / "insider.sqlite")))
    report_dir: Path = Path(os.getenv("INSIDER_REPORT_DIR", str(ROOT / "data" / "reports")))
    raw_dir: Path = Path(os.getenv("INSIDER_RAW_DIR", str(ROOT / "data" / "raw")))  # 공시 원문 캐시

    # 알림 채널 (비워두면 해당 채널은 건너뜀)
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")            # 무료 채널 (@채널아이디 또는 -100...)
    telegram_paid_chat_id: str = os.getenv("TELEGRAM_PAID_CHAT_ID", "")  # 유료 채널. 비우면 발송 안 함
    site_url: str = os.getenv("SITE_URL", "")  # 메시지 하단 '전체 보기' 링크. 비우면 생략
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    mail_from: str = os.getenv("MAIL_FROM", "")
    mail_to: list[str] = field(
        default_factory=lambda: [x.strip() for x in os.getenv("MAIL_TO", "").split(",") if x.strip()]
    )

    # 리포트 기준값
    min_amount_krw: int = int(os.getenv("MIN_AMOUNT_KRW", "10000000"))  # 1천만 원 미만 거래는 리포트에서 제외
    cluster_window_days: int = int(os.getenv("CLUSTER_WINDOW_DAYS", "7"))
    top_n: int = int(os.getenv("TOP_N", "15"))
    request_sleep: float = float(os.getenv("REQUEST_SLEEP", "0.3"))  # DART 호출 간격(초)

    # 정적 사이트. 비우면 생성하지 않는다. 기본은 ./site (GitHub Pages 로 배포)
    site_dir: Path | None = Path(os.getenv("SITE_DIR", str(ROOT / "site"))) if os.getenv("SITE_DIR", "site") else None
    site_days: int = int(os.getenv("SITE_DAYS", "30"))  # 사이트에 남길 지난 리포트 일수

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
