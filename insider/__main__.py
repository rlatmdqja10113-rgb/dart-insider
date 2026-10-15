"""CLI.

python -m insider run                 # 수집 + 파싱 + 리포트 + 발송 (야간 스케줄용)
python -m insider run --dry-run       # 발송 없이 리포트 파일만
python -m insider backfill --days 365 # 과거 1년치 적재
python -m insider report --lookback 3 # DB에 있는 것으로 리포트만 다시 생성
python -m insider stats
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from .config import settings
from .db import Store
from . import pipeline


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="insider")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="수집·파싱·리포트·발송")
    r.add_argument("--days", type=int, default=7, help="공시 목록 조회 범위(일). 보고 지연을 감안해 여유 있게")
    r.add_argument("--lookback", type=int, default=1, help="리포트에 포함할 접수일 범위(일)")
    r.add_argument("--dry-run", action="store_true", help="알림을 보내지 않음")

    b = sub.add_parser("backfill", help="과거 데이터 적재")
    b.add_argument("--days", type=int, default=365)

    rp = sub.add_parser("report", help="DB 내용으로 리포트만 생성")
    rp.add_argument("--lookback", type=int, default=1)
    rp.add_argument("--date", type=date.fromisoformat, default=date.today())

    sub.add_parser("stats", help="DB 현황")
    sub.add_parser("reparse", help="저장된 원문(data/raw)으로 파서만 다시 실행 (네트워크 불필요)")

    st = sub.add_parser("site", help="DB 내용으로 정적 사이트 생성")
    st.add_argument("--days", type=int, default=settings.site_days)
    st.add_argument("--lookback", type=int, default=1)
    st.add_argument("--out", default=str(settings.site_dir or "site"))

    a = p.parse_args(argv)

    if a.cmd == "run":
        path = pipeline.run_daily(settings, days=a.days, lookback_days=a.lookback, dry_run=a.dry_run)
        print(path)
    elif a.cmd == "backfill":
        pipeline.backfill(settings, days=a.days)
    elif a.cmd == "report":
        settings.ensure_dirs()
        store = Store(settings.db_path)
        from .report import build_markdown, build_telegram
        data = pipeline.build_report_data(settings, store, a.date, a.lookback)
        print(build_markdown(data, settings.top_n))
        print("\n--- telegram (free) ---\n")
        print(build_telegram(data, tier="free", site_url=settings.site_url))
        print("\n--- telegram (full) ---\n")
        print(build_telegram(data, tier="full", site_url=settings.site_url, top_n=settings.top_n))
    elif a.cmd == "stats":
        print(Store(settings.db_path).stats())
    elif a.cmd == "reparse":
        import logging
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        print(pipeline.reparse(settings, Store(settings.db_path)))
    elif a.cmd == "site":
        from pathlib import Path
        from .site import build_site
        settings.ensure_dirs()
        out = build_site(settings, Store(settings.db_path), Path(a.out), days=a.days, lookback_days=a.lookback)
        print(out / "index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
