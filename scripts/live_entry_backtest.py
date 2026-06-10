#!/usr/bin/env python3
"""直播早进场策略回归：80 分钟时领先方最终胜率。

用 API-Football 终局比赛的进球事件重建第 80 分钟比分，评估
「80' 买领先方 Will X win? YES」在不同分差下的命中率。

用法:
    .venv/bin/python -m scripts.live_entry_backtest --config config.yaml --days 7
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from src.config import load_config
from src.net.proxy import ProxyTransport

_FINAL = {"FT", "AET", "PEN"}


@dataclass
class MatchAt80:
    home: str
    away: str
    league: str
    h80: int
    a80: int
    hf: int
    af: int
    leader_at_80: str  # home | away | draw
    final_winner: str  # home | away | draw
    leader_wins: bool | None  # None if draw at 80


@dataclass
class BucketStats:
    total: int = 0
    leader_wins: int = 0
    comebacks: list[str] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.leader_wins / self.total if self.total else 0.0


def _score_at(events: list[dict], minute: int, home_id: int, away_id: int) -> tuple[int, int]:
    """按进球事件重建 minute 时刻比分（含常规时间进球）。"""
    h, a = 0, 0
    for ev in events:
        if (ev.get("type") or "").lower() != "goal":
            continue
        t = ev.get("time") or {}
        elapsed = t.get("elapsed")
        if elapsed is None:
            continue
        if int(elapsed) > minute:
            continue
        detail = (ev.get("detail") or "").lower()
        if "missed penalty" in detail:
            continue
        team = (ev.get("team") or {}).get("id")
        if team == home_id:
            if "own goal" in detail:
                a += 1
            else:
                h += 1
        elif team == away_id:
            if "own goal" in detail:
                h += 1
            else:
                a += 1
    return h, a


def _winner(h: int, a: int) -> str:
    if h > a:
        return "home"
    if a > h:
        return "away"
    return "draw"


async def fetch_day_fixtures(client, host: str, headers: dict, day: str) -> list[dict]:
    r = await client.get(
        f"https://{host}/fixtures",
        params={"date": day},
        headers=headers,
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json()
    err = data.get("errors") or {}
    if err:
        raise RuntimeError(f"API errors: {err}")
    out = []
    for item in data.get("response", []):
        st = ((item.get("fixture") or {}).get("status") or {}).get("short", "")
        if st.upper() not in _FINAL:
            continue
        out.append(item)
    return out


async def fetch_events(client, host: str, headers: dict, fixture_id: int) -> list[dict]:
    r = await client.get(
        f"https://{host}/fixtures/events",
        params={"fixture": fixture_id},
        headers=headers,
        timeout=20.0,
    )
    r.raise_for_status()
    return r.json().get("response") or []


async def main_async(config_path: str, days: int, minute: int, max_fixtures: int) -> int:
    cfg = load_config(config_path)
    if not cfg.api_football_key:
        print("未配置 API_FOOTBALL_KEY，无法回归")
        return 1

    proxy = ProxyTransport(cfg.proxy)
    client = await proxy.get_httpx_client()
    headers = {"x-apisports-key": cfg.api_football_key}
    host = cfg.api_football_host

    today = datetime.now(timezone.utc).date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(days)]

    fixtures: list[dict] = []
    for d in dates:
        try:
            fixtures.extend(await fetch_day_fixtures(client, host, headers, d))
        except Exception as e:
            print(f"日期 {d} 拉取失败: {e}")

    if max_fixtures > 0:
        fixtures = fixtures[:max_fixtures]

    print(f"样本：近 {days} 天已结束比赛 {len(fixtures)} 场（80' 快照）")
    print(f"配置对照: football_min_elapsed_min={cfg.football_min_elapsed_min}, "
          f"live_min_lead>={cfg.football_blowout_lead}（须同时满足分钟+分差，方案 B）, "
          f"price=[{cfg.early_entry_price}, {cfg.entry_max_price}]")
    print()

    results: list[MatchAt80] = []
    api_calls = len(dates)

    for i, item in enumerate(fixtures):
        fix = item.get("fixture") or {}
        teams = item.get("teams") or {}
        goals = item.get("goals") or {}
        home = (teams.get("home") or {}).get("name", "")
        away = (teams.get("away") or {}).get("name", "")
        home_id = (teams.get("home") or {}).get("id")
        away_id = (teams.get("away") or {}).get("id")
        league = (item.get("league") or {}).get("name", "")
        fid = fix.get("id")
        if not fid or home_id is None or away_id is None:
            continue

        hf = int(goals.get("home") or 0)
        af = int(goals.get("away") or 0)
        final_w = _winner(hf, af)

        try:
            events = await fetch_events(client, host, headers, int(fid))
            api_calls += 1
        except Exception:
            continue

        h80, a80 = _score_at(events, minute, int(home_id), int(away_id))
        l80 = _winner(h80, a80)
        lw = None
        if l80 != "draw":
            lw = l80 == final_w

        results.append(
            MatchAt80(
                home=home,
                away=away,
                league=league,
                h80=h80,
                a80=a80,
                hf=hf,
                af=af,
                leader_at_80=l80,
                final_winner=final_w,
                leader_wins=lw,
            )
        )
        if (i + 1) % 50 == 0:
            print(f"  已处理 {i + 1}/{len(fixtures)} …")

    await proxy.aclose()

    # ---- 汇总 ----
    by_margin: dict[str, BucketStats] = defaultdict(BucketStats)
    all_decisive = BucketStats()
    draws_at_80 = 0

    for m in results:
        if m.leader_at_80 == "draw":
            draws_at_80 += 1
            continue
        margin = abs(m.h80 - m.a80)
        if margin >= 3:
            key = "3+"
        else:
            key = str(margin)
        for bucket in (key, "all"):
            b = by_margin[bucket]
            b.total += 1
            if m.leader_wins:
                b.leader_wins += 1
            elif m.leader_wins is False:
                label = f"{m.home} vs {m.away} ({m.h80}-{m.a80}@{minute}' → {m.hf}-{m.af}) [{m.league}]"
                b.comebacks.append(label)
        all_decisive.total += 1
        if m.leader_wins:
            all_decisive.leader_wins += 1
        elif m.leader_wins is False:
            all_decisive.comebacks.append(
                f"{m.home} vs {m.away} ({m.h80}-{m.a80}@{minute}' → {m.hf}-{m.af})"
            )

    print(f"\n=== {minute}' 时有领先方的比赛 ===")
    print(f"有效样本: {all_decisive.total} 场（{minute}' 平局 {draws_at_80} 场，策略本就不买）")
    print(f"领先方最终获胜: {all_decisive.leader_wins}/{all_decisive.total} "
          f"= {100*all_decisive.win_rate:.1f}%")
    print(f"被逆转: {len(all_decisive.comebacks)} 场 ({100*len(all_decisive.comebacks)/max(all_decisive.total,1):.1f}%)")

    print(f"\n=== 按 {minute}' 分差 ===")
    for key in ("1", "2", "3+"):
        b = by_margin.get(key, BucketStats())
        if not b.total:
            continue
        print(f"  分差 {key}: {b.leader_wins}/{b.total} = {100*b.win_rate:.1f}%  "
              f"(逆转 {len(b.comebacks)})")

    print(f"\n=== 盈亏平衡所需胜率（买领先方 YES @ 价格 P）===")
    for p in (0.60, 0.66, 0.75, 0.85, 0.90, 0.95, 0.996):
        print(f"  P={p:.3f} → 需胜率 ≥ {100*p:.1f}%", end="")
        if all_decisive.total:
            ok = all_decisive.win_rate >= p
            print(f"  | 全样本 {'✓' if ok else '✗'} ({100*all_decisive.win_rate:.1f}%)")
        else:
            print()

    margin1 = by_margin.get("1", BucketStats())
    if margin1.total:
        print(f"\n  仅 1 球领先 @ {minute}': 需各价位胜率 vs 实际 {100*margin1.win_rate:.1f}%")
        for p in (0.66, 0.85, 0.90):
            print(f"    P={p}: {'✓' if margin1.win_rate >= p else '✗'} (1球领先)")

    print(f"\n=== 逆转样例（最多 15 场）===")
    for line in all_decisive.comebacks[:15]:
        print(f"  {line}")

    # 当前 bot 方案 B：仅 ≥80' 且分差≥2 才直播买；1 球领先等终局
    plan_b = BucketStats()
    one_goal_only = BucketStats()
    for m in results:
        if m.leader_at_80 == "draw":
            continue
        margin = abs(m.h80 - m.a80)
        if margin >= cfg.football_blowout_lead:
            plan_b.total += 1
            if m.leader_wins:
                plan_b.leader_wins += 1
        elif margin == 1:
            one_goal_only.total += 1
            if m.leader_wins:
                one_goal_only.leader_wins += 1

    print(f"\n=== 对照当前 bot 规则（方案 B）@ {minute}' ===")
    print(f"  分差≥{cfg.football_blowout_lead} 且 ≥{minute}' 才直播 ARM: "
          f"{plan_b.leader_wins}/{plan_b.total} = {100*plan_b.win_rate:.1f}%")
    print(f"  分差=1 @ {minute}'（策略不买，等终局）: "
          f"{one_goal_only.leader_wins}/{one_goal_only.total} = "
          f"{100*one_goal_only.win_rate:.1f}% 仅供参考")

    print(f"\nAPI 调用约 {api_calls} 次")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", "-c", default="config.yaml")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--minute", type=int, default=80)
    ap.add_argument("--max-fixtures", type=int, default=350, help="限制样本量以省 API 配额")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main_async(args.config, args.days, args.minute, args.max_fixtures)))


if __name__ == "__main__":
    main()
