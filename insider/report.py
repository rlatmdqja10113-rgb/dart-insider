"""리포트 생성. 텔레그램 메시지가 상품이고, 마크다운은 보관·이메일용이다.

원칙
- 사실(누가, 언제, 얼마나, 어떤 사유로)만 적는다. 매수/매도 추천 문구는 넣지 않는다.
- 맨 위 한 줄로 '오늘 볼 게 있는지' 바로 알게 한다.
- 개인 임원·주요주주와 법인·기관을 섞지 않는다. 그게 이 서비스가 존재하는 이유다.
"""
from __future__ import annotations

import html
from datetime import date

from .scoring import Cluster, Scored

WEEKDAY_KO = "월화수목금토일"

# 무료 티어에 보여줄 건수. 유료는 top_n 전체.
FREE_CLUSTERS = 3
FREE_BUYS = 5
FREE_SELLS = 3


def won(n: int | None) -> str:
    if not n:
        return "-"
    if n >= 100_000_000:
        v = n / 100_000_000
        return f"{v:.0f}억" if v >= 10 else f"{v:.1f}억"
    if n >= 10_000:
        return f"{n // 10_000:,}만"
    return f"{n:,}"


def shares(n: int | None) -> str:
    return "-" if n is None else f"{abs(n):,}주"


def _md(d: date | str | None) -> str:
    """'10/08' 형태."""
    if not d:
        return "?"
    s = d.isoformat() if isinstance(d, date) else str(d)
    return f"{int(s[5:7])}/{int(s[8:10])}" if len(s) >= 10 else s


def who(t: dict) -> str:
    pos = t.get("position") or t.get("relation") or ""
    name = t.get("reporter") or "?"
    return f"{name}·{pos}" if pos else name


def ticker(t: dict) -> str:
    code = t.get("stock_code")
    return f"{t['corp_name']}({code})" if code else t["corp_name"]


def headline(data: dict) -> str:
    """맨 위 한 줄. 오늘 볼 게 있는지 없는지."""
    buys, sells, clusters = data["buys"], data["sells"], data["clusters"]
    st = data["stats"]
    parts = []
    if clusters:
        names = "·".join(c.corp_name for c in clusters[:2])
        more = f" 외 {len(clusters) - 2}곳" if len(clusters) > 2 else ""
        parts.append(f"동반매수 {names}{more}")
    ceo_buys = [s for s in buys if "대표이사" in (s.trade.get("position") or "") or "회장" in (s.trade.get("position") or "")]
    if ceo_buys:
        parts.append(f"대표이사·회장 매수 {len(ceo_buys)}명")
    elif buys:
        parts.append(f"임원 매수 {len(buys)}명")
    if sells:
        parts.append(f"매도 {len(sells)}명")
    base = f"공시 {st.get('filings_in_period', 0)}건 중 "
    return base + (" · ".join(parts) if parts else "주목할 개인 임원 매매 없음")


# ---------------------------------------------------------------------------
# 텔레그램 (HTML parse_mode)
# ---------------------------------------------------------------------------
def _e(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def _amount_or_shares(t: dict) -> str:
    return won(t["amount"]) if t.get("amount") else shares(t.get("change_cnt")) + "(단가 미기재)"


def _when(t: dict) -> str:
    a, b = t.get("date_from") or t.get("event_date"), t.get("date_to") or t.get("event_date")
    n = t.get("n_trades") or 1
    s = _md(a) if a == b else f"{_md(a)}~{_md(b)}"
    return f"{s} · {n}회" if n > 1 else s


def _tg_trade(s: Scored, detail: bool) -> str:
    t = s.trade
    line = f"• <b>{_e(t['corp_name'])}</b> {_e(who(t))} <b>{_e(_amount_or_shares(t))}</b>"
    bits = [_when(t)]
    if detail:
        px = f" @{t['price']:,}" if t.get("price") else ""
        if t.get("n_trades", 1) > 1 and px:
            px = f" 평균{px}"
        bits.append(shares(t.get("change_cnt")) + px)
    for f in s.flags:
        bits.append(("⚠" if "옵션" in f else "✦") + f)  # 옵션행사 후 매도는 주의, 나머지는 참고
    return line + f"\n   <i>{_e(' · '.join(bits))}</i>"


def _tg_cluster(c: Cluster) -> str:
    positions = []
    for t in c.trades:
        p = t.get("position") or t.get("relation")
        if p and p not in positions:
            positions.append(p)
    pos = f"({'·'.join(positions[:3])})" if positions else ""
    return (
        f"• <b>{_e(c.corp_name)}</b> {len(c.reporters)}명{_e(pos)} <b>{_e(won(c.total_amount))}</b>"
        f"\n   <i>{_md(c.first_date) if c.first_date == c.last_date else f'{_md(c.first_date)}~{_md(c.last_date)}'} · {_e(', '.join(c.reporters))}</i>"
    )


def build_telegram(data: dict, tier: str = "free", site_url: str = "", top_n: int = 15) -> str:
    """tier: 'free' 는 상위 몇 건만, 'full' 은 top_n 까지 + 법인·기관 목록.
    Telegram 한도(4096자)는 notify 쪽에서 나눠 보낸다."""
    d: date = data["report_date"]
    buys, sells, clusters = data["buys"], data["sells"], data["clusters"]
    ebuys, esells = data.get("entity_buys", []), data.get("entity_sells", [])
    full = tier == "full"
    n_c = top_n if full else FREE_CLUSTERS
    n_b = top_n if full else FREE_BUYS
    n_s = top_n if full else FREE_SELLS

    out = [f"📊 <b>내부자 매매 브리핑</b> {d:%m/%d}({WEEKDAY_KO[d.weekday()]})", _e(headline(data)), ""]

    if clusters:
        out.append("🔵 <b>동반 매수</b> — 같은 회사, 임원 여러 명")
        out += [_tg_cluster(c) for c in clusters[:n_c]]
        if len(clusters) > n_c:
            out.append(f"   <i>… 외 {len(clusters) - n_c}곳</i>")
        out.append("")

    out.append("🟢 <b>임원·주요주주 매수</b>")
    out += [_tg_trade(s, full) for s in buys[:n_b]] or ["• 없음"]
    if len(buys) > n_b:
        out.append(f"   <i>… 외 {len(buys) - n_b}명</i>")
    out.append("")

    out.append("🔴 <b>임원·주요주주 매도</b>")
    out += [_tg_trade(s, full) for s in sells[:n_s]] or ["• 없음"]
    if len(sells) > n_s:
        out.append(f"   <i>… 외 {len(sells) - n_s}명</i>")
    out.append("")

    n_ent = len(ebuys) + len(esells)
    if n_ent:
        if full:
            out.append(f"🏢 <b>법인·기관 지분 변동</b> {n_ent}건")
            for s in (ebuys + esells)[: max(5, top_n // 2)]:
                t = s.trade
                arrow = "▲" if t["kind"] == "buy" else "▼"
                out.append(f"• {arrow} {_e(t['corp_name'])} {_e(who(t))} {_e(won(t.get('amount')))}")
        else:
            out.append(f"🏢 법인·기관 지분 변동 {n_ent}건 (최대주주·자산운용·연기금 등) — 유료 채널에서 전체 제공")
        out.append("")

    noise = data["stats"].get("noise_excluded", 0)
    tail = f"스톡옵션·증여·상속 등 {noise}건 제외" if noise else ""
    if site_url:
        tail = (tail + " · " if tail else "") + f'<a href="{_e(site_url)}">전체 보기</a>'
    if tail:
        out.append(f"<i>{tail}</i>")
    out.append("<i>DART 공시 자동 정리. 매수·매도 권유가 아닙니다.</i>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 마크다운 (보관·이메일)
# ---------------------------------------------------------------------------
def _md_line(s: Scored) -> str:
    t = s.trade
    flag = f" ⚠{'/'.join(s.flags)}" if s.flags else ""
    price = f" @{t['price']:,}원" if t.get("price") else ""
    return (
        f"- **{ticker(t)}** {who(t)} · {_when(t)} · "
        f"{t.get('reason') or ''} {shares(t.get('change_cnt'))}{price} · {_amount_or_shares(t)}{flag}"
    )


def build_markdown(data: dict, top_n: int) -> str:
    d: date = data["report_date"]
    buys, sells, clusters = data["buys"], data["sells"], data["clusters"]
    ebuys, esells = data.get("entity_buys", []), data.get("entity_sells", [])
    md: list[str] = []
    md.append(f"# 내부자 매매 리포트 — {d:%Y-%m-%d}")
    md.append(f"_집계 대상: {data['period']} 접수된 '임원ㆍ주요주주 특정증권등 소유상황보고서'_\n")
    md.append(headline(data) + "\n")

    md.append("## 🔵 동반 매수 (같은 회사, 복수 임원)")
    if clusters:
        for c in clusters[:top_n]:
            md.append(
                f"- **{c.corp_name}**{f'({c.stock_code})' if c.stock_code else ''} · "
                f"{len(c.reporters)}명 · {c.first_date}~{c.last_date} · 합계 {won(c.total_amount)} · "
                f"{', '.join(c.reporters)}"
            )
    else:
        md.append("- 해당 없음")
    md.append("")

    md.append(f"## 🟢 임원·주요주주 매수 (상위 {min(top_n, len(buys))})")
    md += [_md_line(s) for s in buys[:top_n]] or ["- 해당 없음"]
    md.append("")
    md.append(f"## 🔴 임원·주요주주 매도 (상위 {min(top_n, len(sells))})")
    md += [_md_line(s) for s in sells[:top_n]] or ["- 해당 없음"]
    md.append("")

    if ebuys or esells:
        md.append(f"## 🏢 법인·기관 지분 변동 ({len(ebuys) + len(esells)}건)")
        md += [_md_line(s) for s in (ebuys + esells)[:top_n]]
        md.append("")

    md.append("---")
    md.append(
        "본 리포트는 금융감독원 DART 공시를 자동 수집·정리한 것으로, 특정 종목의 매수·매도를 권유하지 않습니다. "
        "보고 기한(5영업일) 때문에 실제 거래일과 접수일은 차이가 있을 수 있습니다."
    )
    return "\n".join(md)
