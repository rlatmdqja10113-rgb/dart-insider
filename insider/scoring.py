"""거래에 '주목도' 점수를 매기고, 노이즈를 걸러내고, 클러스터 매수를 찾는다.

점수는 절대적인 의미가 있는 숫자가 아니라 리포트 정렬용이다. 투자 판단이 아니라 '먼저 볼 것' 순서다.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

# 직위가 높을수록 자기 회사 사정을 잘 안다고 보고 가중치를 준다.
POSITION_WEIGHTS = [
    (re.compile(r"대표이사|회장|CEO|최고경영자"), 3.0),
    (re.compile(r"부회장|사장|부사장|CFO|최고재무"), 2.5),
    (re.compile(r"전무|상무|본부장|사내이사|등기임원"), 2.0),
    (re.compile(r"최대주주|주요주주|특수관계"), 1.8),
    (re.compile(r"사외이사|감사|이사"), 1.5),
]


# 보고자가 법인·기관인지 판별하는 단서. 하나라도 걸리면 '개인 임원이 자기 돈으로 산 것'이 아니라고 본다.
ENTITY_RE = re.compile(
    r"주식회사|\(주\)|㈜|\(유\)|유한회사|유한책임|합자회사|합명회사|농업회사|"
    r"자산운용|자산관리|투자자문|투자조합|투자신탁|신기술|사모|조합|공단|공사|재단|법인|기금|"
    r"증권|은행|보험|캐피탈|캐피털|인베스트|파트너스|홀딩스|리츠|연금|금고|협회|신탁|벤처|창업|"
    r"\bInc\b|\bLtd\b|\bLLC\b|\bL\.?P\.?\b|\bCo\b|\bCorp|\bPLC\b|Limited|Company|Capital|Partners|"
    r"Holdings|Securities|\bBank\b|\bTrust\b|\bFund\b|Management|Investment|Asset|Group|S\.?A\.?\b|GmbH|"
    r"\bAG\b|\bN\.?V\.?\b|\bB\.?V\.?\b|Pte|\bSPC\b|유동화",
    re.IGNORECASE,
)
# 직위·관계 칸에 업종이 적혀 있으면 법인이다 (예: '금융투자업', 'Investment Bank', '주택담보대출업')
ENTITY_POSITION_RE = re.compile(r"업$|업\(|업체|Bank|Fund|Manager|Advisor", re.IGNORECASE)


def reporter_kind(
    reporter: str | None, position: str | None = None, relation: str | None = None, reporter_type: str | None = None
) -> str:
    """'person' 또는 'entity'. DART 가 적어준 보고자 구분(개인/법인)을 최우선으로 쓰고,
    없으면 이름·직위 패턴으로 추정한다. 단서가 없으면 개인으로 본다."""
    rt = (reporter_type or "").replace(" ", "")
    # 실제 값: 개인(국내), 개인(외국), 국내법인, 외국법인, 금융기관, 연기금등 전문투자자, 기타단체(국내) ...
    if rt.startswith("개인"):
        return "person"
    if any(k in rt for k in ("법인", "기관", "연기금", "단체", "조합", "기타", "펀드", "투자자")):
        return "entity"
    name = (reporter or "").strip()
    if not name:
        return "person"
    if ENTITY_RE.search(name):
        return "entity"
    if position and ENTITY_POSITION_RE.search(position.strip()):
        return "entity"
    # 한글 이름은 보통 2~4자. 공백 없는 한글이 6자 이상이면 회사명일 가능성이 높다.
    if re.fullmatch(r"[가-힣]{6,}", name):
        return "entity"
    return "person"


def position_weight(position: str | None, relation: str | None) -> float:
    text = f"{position or ''} {relation or ''}"
    for pat, w in POSITION_WEIGHTS:
        if pat.search(text):
            return w
    return 1.0


def amount_weight(amount: int | None) -> float:
    """금액 구간별 가중치. 억 단위로 올라갈수록 신호가 세다고 본다."""
    if not amount:
        return 0.5  # 단가가 없어 금액을 모르는 거래
    if amount >= 1_000_000_000:
        return 3.0
    if amount >= 300_000_000:
        return 2.2
    if amount >= 100_000_000:
        return 1.6
    if amount >= 30_000_000:
        return 1.2
    return 1.0


@dataclass
class Scored:
    trade: dict
    score: float
    flags: list[str] = field(default_factory=list)
    reporter_kind: str = "person"  # person / entity


def group_trades(scored: list[Scored]) -> list[Scored]:
    """같은 회사·같은 보고자·같은 방향(buy/sell)의 거래를 한 줄로 합친다.
    합친 trade 에는 n_trades, date_from, date_to, avg_price 가 추가되고 amount/change_cnt 는 합계다.
    점수는 묶음 안 최고 점수에 횟수 보너스(회당 +8%, 최대 +40%)를 더한다."""
    groups: dict[tuple, list[Scored]] = {}
    for sc in scored:
        t = sc.trade
        groups.setdefault((t["corp_code"], t.get("reporter"), t["kind"]), []).append(sc)

    out: list[Scored] = []
    for items in groups.values():
        items.sort(key=lambda x: x.trade.get("event_date") or "")
        first, last = items[0].trade, items[-1].trade
        amount = sum(x.trade.get("amount") or 0 for x in items) or None
        change = sum(x.trade.get("change_cnt") or 0 for x in items) or None
        priced = [x.trade for x in items if x.trade.get("price") and x.trade.get("change_cnt")]
        avg_price = (
            round(sum(abs(t["change_cnt"]) * t["price"] for t in priced) / sum(abs(t["change_cnt"]) for t in priced))
            if priced else None
        )
        merged = dict(max(items, key=lambda x: x.score).trade)
        merged.update({
            "amount": amount,
            "change_cnt": change,
            "before_cnt": first.get("before_cnt"),
            "after_cnt": last.get("after_cnt"),
            "price": avg_price,
            "event_date": first.get("event_date"),
            "date_from": first.get("event_date"),
            "date_to": last.get("event_date"),
            "n_trades": len(items),
        })
        flags: list[str] = []
        for x in items:
            for f in x.flags:
                if f not in flags:
                    flags.append(f)
        score = max(x.score for x in items) * (1 + min(0.08 * (len(items) - 1), 0.4))
        out.append(Scored(trade=merged, score=round(score, 2), flags=flags, reporter_kind=items[0].reporter_kind))
    out.sort(key=lambda s: (s.score, s.trade.get("amount") or 0), reverse=True)
    return out


def split_by_reporter(scored: list[Scored]) -> tuple[list[Scored], list[Scored]]:
    """(개인 임원·주요주주, 법인·기관) 으로 나눈다. 순서는 유지."""
    persons = [s for s in scored if s.reporter_kind == "person"]
    entities = [s for s in scored if s.reporter_kind == "entity"]
    return persons, entities


@dataclass
class Cluster:
    corp_code: str
    corp_name: str
    stock_code: str | None
    reporters: list[str]
    total_amount: int
    first_date: str
    last_date: str
    trades: list[dict]


def _d(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _is_option_dump(trade: dict, history: list[dict], window_days: int = 30) -> bool:
    """매도 거래가 최근 스톡옵션 행사 직후인지. 그러면 '현금화'라 신호가 약하다."""
    if trade["kind"] != "sell":
        return False
    td = _d(trade.get("event_date"))
    if not td:
        return False
    for h in history:
        if h["kind"] != "other" or h.get("reporter") != trade.get("reporter"):
            continue
        reason = (h.get("reason") or "").replace(" ", "")
        if "매수선택권" in reason or "스톡옵션" in reason:
            hd = _d(h.get("event_date"))
            if hd and 0 <= (td - hd).days <= window_days:
                return True
    return False


def score_trades(trades: list[dict], history_by_corp: dict[str, list[dict]], min_amount: int) -> list[Scored]:
    """buy/sell 거래에 점수를 매긴다. other 는 리포트 대상이 아니므로 제외한다."""
    out: list[Scored] = []
    for t in trades:
        if t["kind"] not in ("buy", "sell"):
            continue
        amount = t.get("amount") or 0
        if amount and amount < min_amount:
            continue  # 소액 거래 제외

        flags: list[str] = []
        kind = reporter_kind(t.get("reporter"), t.get("position"), t.get("relation"), t.get("reporter_type"))
        pw = position_weight(t.get("position"), t.get("relation"))
        aw = amount_weight(t.get("amount"))
        score = pw * aw
        if kind == "entity":
            score *= 0.4  # 법인·기관의 포트폴리오 조정은 개인 임원 매수보다 신호가 약하다

        if t["kind"] == "buy":
            score *= 1.3  # 매수는 매도보다 해석이 단순해서(이유가 하나뿐) 가중
        else:
            if _is_option_dump(t, history_by_corp.get(t["corp_code"], [])):
                flags.append("옵션행사 후 매도")
                score *= 0.3

        # 보유 수량 대비 큰 변동이면 가중
        before, change = t.get("before_cnt"), t.get("change_cnt")
        if before and change:
            ratio = abs(change) / before
            if t["kind"] == "sell" and ratio >= 0.99:
                flags.append("전량 매도")
                score *= 1.4
            elif ratio >= 0.5:
                flags.append(f"보유분 {ratio:.0%} 변동")
                score *= 1.4
            elif before == 0 or ratio >= 0.2:
                score *= 1.15
        elif (before == 0 or before is None) and t["kind"] == "buy" and change:
            flags.append("신규 매수")
            score *= 1.2

        out.append(Scored(trade=t, score=round(score, 2), flags=flags, reporter_kind=kind))
    out.sort(key=lambda s: s.score, reverse=True)
    return out


def find_clusters(trades: list[dict], window_days: int, min_reporters: int = 2) -> list[Cluster]:
    """같은 회사에서 window_days 안에 서로 다른 '개인' 보고자 둘 이상이 장내매수한 경우를 찾는다.
    법인은 세지 않는다 — 모회사와 그 대표가 같이 사는 건 한 주체로 본다."""
    by_corp: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        if t["kind"] != "buy" or not t.get("event_date"):
            continue
        if reporter_kind(t.get("reporter"), t.get("position"), t.get("relation"), t.get("reporter_type")) != "person":
            continue
        by_corp[t["corp_code"]].append(t)

    clusters: list[Cluster] = []
    for corp_code, ts in by_corp.items():
        ts.sort(key=lambda x: x["event_date"])
        dates = [_d(t["event_date"]) for t in ts]
        best: list[dict] = []
        for i in range(len(ts)):
            group = [ts[j] for j in range(i, len(ts)) if (dates[j] - dates[i]).days <= window_days]
            if len({g.get("reporter") for g in group}) > len({g.get("reporter") for g in best}):
                best = group
        reporters = sorted({g.get("reporter") or "?" for g in best})
        if len(reporters) >= min_reporters:
            clusters.append(
                Cluster(
                    corp_code=corp_code,
                    corp_name=best[0]["corp_name"],
                    stock_code=best[0].get("stock_code"),
                    reporters=reporters,
                    total_amount=sum(g.get("amount") or 0 for g in best),
                    first_date=best[0]["event_date"],
                    last_date=best[-1]["event_date"],
                    trades=best,
                )
            )
    clusters.sort(key=lambda c: (len(c.reporters), c.total_amount), reverse=True)
    return clusters
