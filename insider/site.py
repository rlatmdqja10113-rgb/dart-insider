"""정적 사이트 생성. DB 내용으로 최근 N일치 리포트 페이지와 목록 페이지를 만든다.

GitHub Pages 같은 정적 호스팅에 그대로 올리면 되도록 외부 의존성 없는 HTML 을 만든다.
출력 구조:
  site/index.html            최신 리포트 (목록 포함)
  site/reports/YYYY-MM-DD.html  날짜별 리포트
  site/feed.json             최근 리포트 요약 (다른 서비스에서 가져다 쓰기 좋게)
"""
from __future__ import annotations

import html
import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path

from .config import Settings, today_kst
from .db import Store
from .pipeline import build_report_data
from .report import won, shares, headline
from .scoring import Cluster, Scored

log = logging.getLogger(__name__)

SITE_TITLE = os.getenv("SITE_TITLE", "내부자 매매 리포트")
SITE_TAGLINE = os.getenv("SITE_TAGLINE", "상장사 임원·대주주가 자기 회사 주식을 장내에서 사고판 것만, 매일 아침 한 장으로.")
SITE_SUBSCRIBE_URL = os.getenv("SITE_SUBSCRIBE_URL", "")  # 텔레그램 채널이나 뉴스레터 주소
SITE_BASE_PATH = os.getenv("SITE_BASE_PATH", "")  # 예: /dart-insider  (GitHub Pages 프로젝트 사이트일 때)

CSS = """
:root{--bg:#fafaf9;--fg:#1c1917;--muted:#78716c;--line:#e7e5e4;--card:#fff;--buy:#15803d;--sell:#b91c1c;--cluster:#1d4ed8;--accent:#1c1917}
@media(prefers-color-scheme:dark){:root{--bg:#0c0a09;--fg:#f5f5f4;--muted:#a8a29e;--line:#292524;--card:#1c1917;--buy:#4ade80;--sell:#f87171;--cluster:#60a5fa;--accent:#f5f5f4}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 -apple-system,"Pretendard","Apple SD Gothic Neo","Noto Sans KR",system-ui,sans-serif}
a{color:inherit}.wrap{max-width:860px;margin:0 auto;padding:24px 16px 64px}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;justify-content:space-between;border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:24px}
header h1{font-size:20px;margin:0}header h1 a{text-decoration:none}header p{margin:0;color:var(--muted);font-size:14px;flex-basis:100%}
.btn{display:inline-block;padding:8px 14px;border-radius:8px;background:var(--accent);color:var(--bg);text-decoration:none;font-weight:600;font-size:14px}
.meta{color:var(--muted);font-size:14px;margin:0 0 20px}
h2{font-size:17px;margin:28px 0 10px;display:flex;align-items:center;gap:8px}h2 .dot{width:10px;height:10px;border-radius:50%;display:inline-block}
.dot.buy{background:var(--buy)}.dot.sell{background:var(--sell)}.dot.cluster{background:var(--cluster)}.dot.entity{background:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:8px}
.card .top{display:flex;justify-content:space-between;gap:12px;align-items:baseline}
.corp{font-weight:700}.code{color:var(--muted);font-weight:400;font-size:13px;margin-left:4px}
.amt{font-variant-numeric:tabular-nums;font-weight:700;white-space:nowrap}.amt.buy{color:var(--buy)}.amt.sell{color:var(--sell)}.amt.cluster{color:var(--cluster)}
.sub{color:var(--muted);font-size:14px;margin-top:2px}.flag{display:inline-block;font-size:12px;padding:1px 6px;border-radius:4px;border:1px solid var(--line);margin-left:6px;color:var(--muted)}
.empty{color:var(--muted);padding:8px 0}
.list{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px}.list a{display:block;padding:8px 10px;border:1px solid var(--line);border-radius:8px;text-decoration:none;font-size:14px;background:var(--card)}
.list a .n{color:var(--muted);font-size:12px;display:block}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font-size:13px}
"""


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _who(t: dict) -> str:
    pos = t.get("position") or t.get("relation") or ""
    name = t.get("reporter") or "?"
    return f"{name} · {pos}" if pos else name


def _trade_card(s: Scored, kind: str) -> str:
    t = s.trade
    code = f'<span class="code">{_esc(t["stock_code"])}</span>' if t.get("stock_code") else ""
    price = f' @{t["price"]:,}원' if t.get("price") else ""
    flags = "".join(f'<span class="flag">{_esc(f)}</span>' for f in s.flags)
    return (
        f'<div class="card"><div class="top"><span class="corp">{_esc(t["corp_name"])}{code}</span>'
        f'<span class="amt {kind}">{_esc(won(t.get("amount")))}</span></div>'
        f'<div class="sub">{_esc(_who(t))}{flags}</div>'
        f'<div class="sub">{_esc(t.get("event_date") or "?")} · {_esc(t.get("reason") or "")} '
        f'{_esc(shares(t.get("change_cnt")))}{_esc(price)}</div></div>'
    )


def _cluster_card(c: Cluster) -> str:
    code = f'<span class="code">{_esc(c.stock_code)}</span>' if c.stock_code else ""
    return (
        f'<div class="card"><div class="top"><span class="corp">{_esc(c.corp_name)}{code}</span>'
        f'<span class="amt cluster">{_esc(won(c.total_amount))}</span></div>'
        f'<div class="sub">{len(c.reporters)}명 매수 · {_esc(c.first_date)} ~ {_esc(c.last_date)}</div>'
        f'<div class="sub">{_esc(", ".join(c.reporters))}</div></div>'
    )


def _section(title: str, kind: str, cards: list[str]) -> str:
    body = "".join(cards) if cards else '<div class="empty">해당 없음</div>'
    return f'<h2><span class="dot {kind}"></span>{_esc(title)}</h2>{body}'


def render_page(data: dict, top_n: int, all_dates: list[date], is_index: bool) -> str:
    report_date: date = data["report_date"]
    period_label = data["period"]
    buys, sells, clusters, stats = data["buys"], data["sells"], data["clusters"], data["stats"]
    entities = data.get("entity_buys", []) + data.get("entity_sells", [])
    base = SITE_BASE_PATH.rstrip("/")
    rel = "" if is_index else "../"
    subscribe = (
        f'<a class="btn" href="{_esc(SITE_SUBSCRIBE_URL)}" target="_blank" rel="noopener">아침마다 받기</a>'
        if SITE_SUBSCRIBE_URL
        else ""
    )
    archive = "".join(
        f'<a href="{rel}reports/{d:%Y-%m-%d}.html"><span class="n">{d:%a}</span>{d:%Y-%m-%d}</a>'
        for d in all_dates
    )
    summary = headline(data)
    entity_section = ""
    if entities:
        entity_section = _section(
            f"법인·기관 지분 변동 ({len(entities)}건) — 최대주주·자산운용·연기금 등",
            "entity",
            [_trade_card(s, s.trade["kind"]) for s in entities[:top_n]],
        )
    canonical = f"{base}/" if is_index else f"{base}/reports/{report_date:%Y-%m-%d}.html"
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(SITE_TITLE)} — {report_date:%Y-%m-%d}</title>
<meta name="description" content="{_esc(SITE_TAGLINE)}">
<link rel="canonical" href="{_esc(canonical)}">
<style>{CSS}</style></head>
<body><div class="wrap">
<header><h1><a href="{rel}index.html">{_esc(SITE_TITLE)}</a></h1>{subscribe}<p>{_esc(SITE_TAGLINE)}</p></header>
<p class="meta"><strong>{report_date:%Y-%m-%d (%a)}</strong> · 집계 대상 접수일 {_esc(period_label)}<br>{_esc(summary)}</p>
{_section("동반 매수 — 같은 회사, 임원 여러 명", "cluster", [_cluster_card(c) for c in clusters[:top_n]])}
{_section(f"임원·주요주주 매수 (상위 {min(top_n, len(buys))})", "buy", [_trade_card(s, "buy") for s in buys[:top_n]])}
{_section(f"임원·주요주주 매도 (상위 {min(top_n, len(sells))})", "sell", [_trade_card(s, "sell") for s in sells[:top_n]])}
{entity_section}
<h2>지난 리포트</h2><ul class="list">{archive}</ul>
<footer>금융감독원 DART 공시를 자동 수집·정리한 자료이며, 특정 종목의 매수·매도를 권유하지 않습니다.
보고 기한(5영업일) 때문에 실제 거래일과 접수일은 차이가 있을 수 있습니다. 투자 판단과 책임은 이용자 본인에게 있습니다.</footer>
</div></body></html>"""


def build_site(settings: Settings, store: Store, out_dir: Path, days: int = 30, lookback_days: int = 1) -> Path:
    """최근 days 일 중 거래가 있는 날짜마다 페이지를 만들고, 가장 최근 것을 index 로 쓴다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "reports").mkdir(exist_ok=True)

    today = today_kst()
    pages: list[tuple[date, dict]] = []
    for i in range(days):
        d = today - timedelta(days=i)
        data = build_report_data(settings, store, d, lookback_days)
        if data["buys"] or data["sells"] or data["clusters"] or data["entity_buys"] or data["entity_sells"]:
            pages.append((d, data))
    if not pages:  # 아직 데이터가 없어도 빈 페이지는 만든다
        pages.append((today, build_report_data(settings, store, today, lookback_days)))

    all_dates = [d for d, _ in pages]
    feed = []
    for idx, (d, data) in enumerate(pages):
        page = render_page(data, settings.top_n, all_dates, is_index=False)
        (out_dir / "reports" / f"{d:%Y-%m-%d}.html").write_text(page, encoding="utf-8")
        if idx == 0:
            index = render_page(data, settings.top_n, all_dates, is_index=True)
            (out_dir / "index.html").write_text(index, encoding="utf-8")
        feed.append({
            "date": d.isoformat(),
            "buys": [{"corp": s.trade["corp_name"], "code": s.trade.get("stock_code"), "who": _who(s.trade),
                      "amount": s.trade.get("amount"), "event_date": s.trade.get("event_date")} for s in data["buys"][:settings.top_n]],
            "sells": [{"corp": s.trade["corp_name"], "code": s.trade.get("stock_code"), "who": _who(s.trade),
                       "amount": s.trade.get("amount"), "flags": s.flags} for s in data["sells"][:settings.top_n]],
            "clusters": [{"corp": c.corp_name, "code": c.stock_code, "reporters": c.reporters,
                          "amount": c.total_amount} for c in data["clusters"][:settings.top_n]],
        })
    (out_dir / "feed.json").write_text(json.dumps(feed, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")  # GitHub Pages 가 파일을 그대로 서빙하도록
    log.info("사이트 생성: %s (%s일치)", out_dir, len(pages))
    return out_dir
