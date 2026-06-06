"""数据源覆盖率测试。

目的：统计"未来一周 Polymarket 开盘的单场体育赛事"中，有多少能被当前接入的
免费/已配置数据源覆盖（即能拿到对阵 → 才能做赛果套利）。

用法：
    .venv/bin/python -m scripts.coverage_test --config config.london.yaml --days 7

说明：
- PM 侧：拉取 Gamma /events?tag_slug=sports，提取"单场对阵 + moneyline"市场，
  仅保留开赛时间落在 [now, now+days] 的比赛（与 bot 真实可交易窗口一致）。
- 数据源侧：分别按未来 days 天的赛程查询各源（ESPN 足球/NBA、OpenLigaDB、
  TheSportsDB，以及配置了 key 的 football-data / api-football / balldontlie）。
- 匹配：复用 bot 的队名归一化 + 模糊匹配逻辑（normalize_team / teams_match）。
- 输出：总覆盖率、分运动覆盖率、各源贡献、未覆盖样例。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config import load_config
from src.matcher.reverse_matcher import normalize_team, teams_match
from src.net.proxy import ProxyTransport
from src.pm.gamma_sync import NON_MATCH_KEYWORDS
from src.store.sqlite import Store

# ESPN 足球：尽量覆盖当季可能开盘的赛事（含世界杯 fifa.world）
ESPN_SOCCER_LEAGUES = [
    "fifa.world",            # 世界杯
    "fifa.friendly",         # 国家队友谊赛
    "fifa.cwc",              # 世俱杯
    "eng.1", "esp.1", "ger.1", "ita.1", "fra.1", "usa.1",
    "uefa.champions", "uefa.europa", "uefa.nations",
    "conmebol.america",      # 美洲杯
]


def _to_dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _teams_from_text(text: str) -> tuple[str | None, str | None]:
    """从 question / title 中提取两队。"""
    for sep in (" vs. ", " vs ", " v "):
        if sep in text:
            parts = text.split(sep, 1)
            if len(parts) == 2:
                left = re.sub(r"^will\s+", "", parts[0].strip(), flags=re.I)
                right = parts[1].split("?")[0].strip()
                right = re.sub(r"\s+end in a draw$", "", right, flags=re.I)
                return left or None, right or None
    return None, None


class PMMarket:
    def __init__(self, sport: str, team_a: str, team_b: str, when: datetime | None, question: str):
        self.sport = sport
        self.team_a = team_a
        self.team_b = team_b
        self.when = when
        self.question = question
        self.covered_by: list[str] = []


async def fetch_pm_markets(client, gamma_base: str, days: int) -> list[PMMarket]:
    """拉取未来 days 天 PM 开盘的单场对阵市场。"""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    out: list[PMMarket] = []
    seen: set[str] = set()
    offset = 0
    limit = 100
    scanned_events = 0
    while True:
        params = {"closed": "false", "tag_slug": "sports", "limit": limit, "offset": offset}
        resp = await client.get(f"{gamma_base}/events", params=params)
        resp.raise_for_status()
        events = resp.json()
        if not events:
            break
        scanned_events += len(events)
        for ev in events:
            title = str(ev.get("title", ""))
            etext = f"{title} {ev.get('description', '')}".lower()
            tags = {str(t.get("slug", "")).lower() for t in (ev.get("tags") or []) if isinstance(t, dict)}
            if "nba" in tags:
                sport = "nba"
            elif "soccer" in tags or "football" in tags:
                sport = "football"
            else:
                continue
            for m in ev.get("markets") or []:
                q = (m.get("question") or "").lower()
                text = f"{etext} {q} {(m.get('slug') or '').lower()}"
                if any(k in text for k in NON_MATCH_KEYWORDS):
                    continue
                mtype = (m.get("sportsMarketType") or "").lower()
                if mtype and mtype != "moneyline":
                    continue
                ta = m.get("teamA")
                tb = m.get("teamB")
                if not ta or not tb:
                    ta, tb = _teams_from_text(m.get("question", ""))
                if not ta or not tb:
                    ta, tb = _teams_from_text(title)
                if not ta or not tb:
                    continue
                when = _to_dt(m.get("gameStartTime") or m.get("startDate") or ev.get("startDate"))
                # 仅保留未来 days 天内的比赛（无时间则保留，避免漏判）
                if when is not None and not (now - timedelta(hours=6) <= when <= horizon):
                    continue
                mid = str(m.get("id", m.get("conditionId", "")))
                dedup = f"{sport}:{str(ta).lower()}:{str(tb).lower()}"
                if dedup in seen:
                    continue
                seen.add(dedup)
                out.append(PMMarket(sport, str(ta), str(tb), when, m.get("question", "") or title))
        if len(events) < limit or offset >= 8000:
            break
        offset += limit
    print(f"[PM] 扫描事件 {scanned_events} 个，提取未来{days}天单场对阵市场 {len(out)} 场")
    return out


class Fixture:
    __slots__ = ("sport", "home", "away", "when", "source")

    def __init__(self, sport: str, home: str, away: str, when: datetime | None, source: str):
        self.sport = sport
        self.home = home
        self.away = away
        self.when = when
        self.source = source


async def fetch_espn_soccer(client, days: int, leagues: list[str]) -> list[Fixture]:
    out: list[Fixture] = []
    today = datetime.now(timezone.utc).date()
    for league in leagues:
        for i in range(days + 1):
            d = (today + timedelta(days=i)).strftime("%Y%m%d")
            url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard"
            try:
                r = await client.get(url, params={"dates": d})
                if r.status_code != 200:
                    continue
                for ev in r.json().get("events", []):
                    comp = (ev.get("competitions") or [{}])[0]
                    cs = comp.get("competitors", [])
                    if len(cs) < 2:
                        continue
                    h = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
                    a = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
                    out.append(Fixture(
                        "football",
                        h.get("team", {}).get("displayName", ""),
                        a.get("team", {}).get("displayName", ""),
                        _to_dt(ev.get("date")), "espn_soccer",
                    ))
            except Exception:
                continue
    return out


async def fetch_espn_nba(client, days: int) -> list[Fixture]:
    out: list[Fixture] = []
    today = datetime.now(timezone.utc).date()
    # NBA 决赛场次间隔 1~3 天，逐日查询更稳妥
    for i in range(days + 1):
        d = (today + timedelta(days=i)).strftime("%Y%m%d")
        url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
        try:
            r = await client.get(url, params={"dates": d})
            if r.status_code != 200:
                continue
            for ev in r.json().get("events", []):
                comp = (ev.get("competitions") or [{}])[0]
                cs = comp.get("competitors", [])
                if len(cs) < 2:
                    continue
                h = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
                a = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
                out.append(Fixture(
                    "nba",
                    h.get("team", {}).get("displayName", ""),
                    a.get("team", {}).get("displayName", ""),
                    _to_dt(ev.get("date")), "espn_nba",
                ))
        except Exception:
            continue
    return out


async def fetch_thesportsdb(client, key: str, days: int) -> list[Fixture]:
    out: list[Fixture] = []
    today = datetime.now(timezone.utc).date()
    for i in range(days + 1):
        d = (today + timedelta(days=i)).isoformat()
        for sport_name, sport in (("Soccer", "football"), ("Basketball", "nba")):
            url = f"https://www.thesportsdb.com/api/v1/json/{key}/eventsday.php"
            try:
                r = await client.get(url, params={"d": d, "s": sport_name})
                if r.status_code != 200:
                    continue
                for ev in (r.json().get("events") or []):
                    h = ev.get("strHomeTeam") or ""
                    a = ev.get("strAwayTeam") or ""
                    if not h or not a:
                        continue
                    # NBA 只保留联盟为 NBA 的，避免 NCAA 等噪声
                    if sport == "nba" and "nba" not in (ev.get("strLeague") or "").lower():
                        continue
                    out.append(Fixture(sport, h, a, _to_dt(ev.get("dateEvent")), "thesportsdb"))
            except Exception:
                continue
    return out


async def fetch_openligadb(client, leagues: list[str], days: int) -> list[Fixture]:
    out: list[Fixture] = []
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    season = str(now.year if now.month >= 7 else now.year - 1)
    for lg in leagues:
        url = f"https://api.openligadb.de/getmatchdata/{lg}/{season}"
        try:
            r = await client.get(url)
            if r.status_code != 200:
                continue
            for m in r.json():
                when = _to_dt(m.get("matchDateTimeUTC") or m.get("matchDateTime"))
                if when and not (now - timedelta(hours=6) <= when <= horizon):
                    continue
                t1 = (m.get("team1") or {}).get("teamName", "")
                t2 = (m.get("team2") or {}).get("teamName", "")
                if t1 and t2:
                    out.append(Fixture("football", t1, t2, when, "openligadb"))
        except Exception:
            continue
    return out


async def fetch_football_data(client, key: str, days: int) -> list[Fixture]:
    out: list[Fixture] = []
    today = datetime.now(timezone.utc).date()
    params = {"dateFrom": today.isoformat(), "dateTo": (today + timedelta(days=days)).isoformat()}
    try:
        r = await client.get("https://api.football-data.org/v4/matches", params=params,
                             headers={"X-Auth-Token": key})
        if r.status_code == 200:
            for m in r.json().get("matches", []):
                h = (m.get("homeTeam") or {}).get("name") or ""
                a = (m.get("awayTeam") or {}).get("name") or ""
                if h and a:
                    out.append(Fixture("football", h, a, _to_dt(m.get("utcDate")), "football_data"))
    except Exception:
        pass
    return out


async def fetch_api_football(client, key: str, host: str, days: int) -> list[Fixture]:
    out: list[Fixture] = []
    today = datetime.now(timezone.utc).date()
    for i in range(days + 1):
        d = (today + timedelta(days=i)).isoformat()
        try:
            r = await client.get(f"https://{host}/fixtures", params={"date": d},
                                 headers={"x-apisports-key": key})
            if r.status_code != 200:
                continue
            for it in r.json().get("response", []):
                teams = it.get("teams") or {}
                h = (teams.get("home") or {}).get("name") or ""
                a = (teams.get("away") or {}).get("name") or ""
                if h and a:
                    out.append(Fixture("football", h, a, _to_dt((it.get("fixture") or {}).get("date")), "api_football"))
        except Exception:
            continue
    return out


async def fetch_balldontlie(client, key: str, days: int) -> list[Fixture]:
    out: list[Fixture] = []
    today = datetime.now(timezone.utc).date()
    params = [("start_date", today.isoformat()), ("end_date", (today + timedelta(days=days)).isoformat())]
    try:
        r = await client.get("https://api.balldontlie.io/v1/games", params=params,
                             headers={"Authorization": key})
        if r.status_code == 200:
            for g in r.json().get("data", []):
                h = (g.get("home_team") or {}).get("full_name") or ""
                a = (g.get("visitor_team") or {}).get("full_name") or ""
                if h and a:
                    out.append(Fixture("nba", h, a, _to_dt(g.get("date")), "balldontlie"))
    except Exception:
        pass
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", "-c", default="config.london.yaml")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    cfg = load_config(args.config)
    proxy = ProxyTransport(cfg.proxy)
    store = Store(cfg.resolve_path(cfg.db_path)) if cfg.resolve_path(cfg.db_path).exists() else Store(":memory:")
    client = await proxy.get_httpx_client()

    print("=" * 70)
    print(f"数据源覆盖率测试  窗口=未来{args.days}天  时间={datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    # 1) PM 侧
    pm_markets = await fetch_pm_markets(client, cfg.gamma_base_url, args.days)

    # 2) 各数据源赛程
    tasks: dict[str, Any] = {}
    enabled: list[str] = []
    skipped: list[str] = []

    # 用配置里的 ESPN 联赛 slug（与 bot 真实配置一致），叠加测试补充列表去重
    espn_leagues = list(dict.fromkeys(list(cfg.espn_soccer_leagues) + ESPN_SOCCER_LEAGUES))
    tasks["espn_soccer"] = fetch_espn_soccer(client, args.days, espn_leagues)
    tasks["espn_nba"] = fetch_espn_nba(client, args.days)
    tasks["openligadb"] = fetch_openligadb(client, cfg.openligadb_leagues, args.days)
    if cfg.thesportsdb_key:
        tasks["thesportsdb"] = fetch_thesportsdb(client, cfg.thesportsdb_key, args.days)
    else:
        skipped.append("thesportsdb(无key)")
    if cfg.football_data_api_key:
        tasks["football_data"] = fetch_football_data(client, cfg.football_data_api_key, args.days)
    else:
        skipped.append("football_data(无key)")
    if cfg.api_football_key:
        tasks["api_football"] = fetch_api_football(client, cfg.api_football_key, cfg.api_football_host, args.days)
    else:
        skipped.append("api_football(无key)")
    if cfg.balldontlie_key:
        tasks["balldontlie"] = fetch_balldontlie(client, cfg.balldontlie_key, args.days)
    else:
        skipped.append("balldontlie(无key)")

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    fixtures_by_source: dict[str, list[Fixture]] = {}
    all_fixtures: list[Fixture] = []
    for name, res in zip(tasks.keys(), results):
        if isinstance(res, Exception):
            print(f"[源] {name:14s} 异常: {res}")
            fixtures_by_source[name] = []
            continue
        fixtures_by_source[name] = res
        all_fixtures.extend(res)
        enabled.append(name)
        print(f"[源] {name:14s} 拉到赛程 {len(res)} 场")

    if skipped:
        print(f"[源] 未配置 key 跳过: {', '.join(skipped)}")

    # 预归一化 fixtures（按运动分桶）
    norm_fix: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for f in all_fixtures:
        sp = "nba" if f.sport == "nba" else "football"
        fh = normalize_team(f.home, store, sp)
        fa = normalize_team(f.away, store, sp)
        norm_fix[sp].append((fh, fa, f.source))

    # 3) 匹配
    per_source_hits: dict[str, int] = defaultdict(int)
    covered = 0
    by_sport_total: dict[str, int] = defaultdict(int)
    by_sport_cov: dict[str, int] = defaultdict(int)
    uncovered: list[PMMarket] = []

    for pm in pm_markets:
        sp = "nba" if pm.sport == "nba" else "football"
        by_sport_total[sp] += 1
        ta = normalize_team(pm.team_a, store, sp)
        tb = normalize_team(pm.team_b, store, sp)
        hit_sources: set[str] = set()
        for fh, fa, src in norm_fix.get(sp, []):
            if teams_match(ta, tb, fh, fa):
                hit_sources.add(src)
        if hit_sources:
            covered += 1
            by_sport_cov[sp] += 1
            for s in hit_sources:
                per_source_hits[s] += 1
            pm.covered_by = sorted(hit_sources)
        else:
            uncovered.append(pm)

    # 4) 报告
    total = len(pm_markets)
    print("\n" + "=" * 70)
    print("覆盖率汇总")
    print("=" * 70)
    if total == 0:
        print("未来窗口内 PM 没有可识别的单场对阵市场（可能赛季空档）。")
    else:
        print(f"PM 单场对阵市场总数 : {total}")
        print(f"至少 1 源覆盖        : {covered}  ({covered/total*100:.1f}%)")
        print(f"完全未覆盖           : {total - covered}  ({(total-covered)/total*100:.1f}%)")
        print("\n分运动：")
        for sp in ("football", "nba"):
            t = by_sport_total[sp]
            if t:
                print(f"  {sp:9s} {by_sport_cov[sp]}/{t}  ({by_sport_cov[sp]/t*100:.1f}%)")
        print("\n各源命中（去重前，单场可被多源覆盖）：")
        for s in sorted(per_source_hits, key=lambda x: -per_source_hits[x]):
            print(f"  {s:14s} 覆盖 {per_source_hits[s]} 场")

        if uncovered:
            print(f"\n未覆盖样例（最多 25 条，共 {len(uncovered)}）：")
            for pm in uncovered[:25]:
                w = pm.when.strftime("%m-%d %H:%M") if pm.when else "??"
                print(f"  [{pm.sport:8s}] {w}  {pm.team_a}  vs  {pm.team_b}")

    await proxy.aclose()
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
