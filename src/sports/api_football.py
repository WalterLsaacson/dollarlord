"""API-Football (api-sports.io) 足球数据源。

PRO 层：7500 次/天、300 次/分钟；提供 status.elapsed + extra（补时）与开球时间，
用于「开赛 80 分钟后 / 大比分提前」的直播早进场判断。
鉴权：请求头 x-apisports-key。未配置 key 时该源自动禁用。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from src.config import AppConfig
from src.net.proxy import ProxyTransport
from src.net.rate_limit import AsyncRateLimiter
from src.sports.base import FixtureStatus, FixtureUpdate, SportType
from src.store.sqlite import Store

logger = logging.getLogger("arb.sports.api_football")

# API-Football 状态码
_LIVE_CODES = {"1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT"}
_FINAL_CODES = {"FT", "AET", "PEN"}


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_kickoff(fixture: dict) -> datetime | None:
    """开球时间：优先 ISO date，其次 Unix timestamp（API 两种都给）。"""
    ko = _parse_dt(fixture.get("date"))
    if ko is not None:
        return ko
    ts = fixture.get("timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return None


def _parse_elapsed_minute(status_obj: dict, short: str) -> int | None:
    """解析 API-Football 比赛分钟（含伤停补时 extra）。

    示例：elapsed=90, extra=3 → 93'；中场 HT 且无 elapsed 时视为 45'。
    """
    elapsed = status_obj.get("elapsed")
    extra = status_obj.get("extra")
    minute: int | None = None

    if isinstance(elapsed, bool):
        elapsed = None
    if isinstance(elapsed, (int, float)):
        minute = int(elapsed)
    elif isinstance(elapsed, str) and elapsed.strip().isdigit():
        minute = int(elapsed.strip())

    if minute is not None and isinstance(extra, (int, float)) and int(extra) > 0:
        minute += int(extra)

    if minute is None and short == "HT":
        return 45
    return minute


def _api_errors(data: dict) -> str:
    """解析 API 返回的 errors 字段（账号暂停/超额时 response 为空但 HTTP 200）。"""
    err = data.get("errors")
    if not err:
        return ""
    if isinstance(err, dict):
        return "; ".join(f"{k}: {v}" for k, v in err.items() if v)
    return str(err)


def _log_quota_headers(resp, source_id: str) -> None:
    """DEBUG 记录 PRO 配额余量（响应头）。"""
    daily_rem = resp.headers.get("x-ratelimit-requests-remaining")
    min_rem = resp.headers.get("X-RateLimit-Remaining")
    if daily_rem is not None or min_rem is not None:
        logger.debug(
            "%s 配额 daily_remaining=%s min_remaining=%s",
            source_id,
            daily_rem,
            min_rem,
        )


class ApiFootballProvider:
    """API-Football 适配器。"""

    def __init__(
        self,
        cfg: AppConfig,
        proxy: ProxyTransport,
        store: Store,
        limiter: AsyncRateLimiter,
    ) -> None:
        self.cfg = cfg
        self.proxy = proxy
        self.store = store
        self.limiter = limiter
        self.source_id = "api_football"
        self.enabled = bool(cfg.api_football_key)
        self._last: list[FixtureUpdate] = []
        self._suspended_logged = False
        self._last_date_fetch_mono = 0.0

    async def fetch_updates(self) -> list[FixtureUpdate]:
        if not self.enabled:
            return []
        if not self.limiter.try_acquire():
            return self._last

        client = await self.proxy.get_httpx_client()
        host = self.cfg.api_football_host
        url = f"https://{host}/fixtures"
        headers = {"x-apisports-key": self.cfg.api_football_key}

        by_key: dict[str, FixtureUpdate] = {}

        # 1) 正在进行中的比赛（精确分钟，每次轮询都拉）
        try:
            resp = await client.get(url, params={"live": "all"}, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            _log_quota_headers(resp, self.source_id)
        except Exception as e:
            logger.warning("api_football 拉取失败: %s", e)
            self.store.touch_source(self.source_id, str(e))
            return self._last

        err = _api_errors(data)
        if err:
            if not self._suspended_logged:
                logger.error(
                    "api_football API 不可用: %s（请在 dashboard.api-football.com 检查账号/配额）",
                    err,
                )
                self._suspended_logged = True
            self.store.touch_source(self.source_id, err)
            return self._last

        self._suspended_logged = False
        for item in data.get("response", []):
            u = self._parse_fixture(item)
            if u:
                by_key[u.fixture_key] = u

        # 2) 当日 LIVE/FINAL 补充（live=all 不含刚结束场次；按间隔拉取以省 PRO 日配额）
        now_mono = time.monotonic()
        date_due = (
            now_mono - self._last_date_fetch_mono
        ) >= float(self.cfg.api_football_date_interval_sec)
        if date_due and self.limiter.try_acquire():
            today = datetime.now(timezone.utc).date().isoformat()
            try:
                resp2 = await client.get(url, params={"date": today}, headers=headers)
                resp2.raise_for_status()
                data2 = resp2.json()
                _log_quota_headers(resp2, self.source_id)
                err2 = _api_errors(data2)
                if err2:
                    logger.warning("api_football 当日赛程 API 错误: %s", err2)
                else:
                    self._last_date_fetch_mono = now_mono
                    for item in data2.get("response", []):
                        u = self._parse_fixture(item)
                        if u and u.status in (FixtureStatus.LIVE, FixtureStatus.FINAL):
                            # 同日接口与 live 重复时，保留 elapsed 更完整的一条
                            prev = by_key.get(u.fixture_key)
                            if prev is None or (
                                u.elapsed_minute is not None
                                and (
                                    prev.elapsed_minute is None
                                    or u.elapsed_minute >= prev.elapsed_minute
                                )
                            ):
                                by_key[u.fixture_key] = u
                            elif prev is not None and u.status == FixtureStatus.FINAL:
                                by_key[u.fixture_key] = u
            except Exception as e:
                logger.debug("api_football 当日赛程拉取失败: %s", e)

        updates = list(by_key.values())
        self.store.touch_source(self.source_id)
        self._last = updates
        return updates

    def _parse_fixture(self, item: dict) -> FixtureUpdate | None:
        fixture = item.get("fixture") or {}
        teams = item.get("teams") or {}
        goals = item.get("goals") or {}
        home = (teams.get("home") or {}).get("name") or ""
        away = (teams.get("away") or {}).get("name") or ""
        if not home or not away:
            return None
        status_obj = fixture.get("status") or {}
        short = (status_obj.get("short") or "").upper()

        if short in _FINAL_CODES:
            st = FixtureStatus.FINAL
        elif short in _LIVE_CODES:
            st = FixtureStatus.LIVE
        else:
            st = FixtureStatus.SCHEDULED

        hs = goals.get("home")
        as_ = goals.get("away")
        winner = None
        if st == FixtureStatus.FINAL and hs is not None and as_ is not None:
            if hs > as_:
                winner = "home"
            elif as_ > hs:
                winner = "away"
            else:
                winner = "draw"

        elapsed = _parse_elapsed_minute(status_obj, short) if st == FixtureStatus.LIVE else None

        return FixtureUpdate(
            fixture_key=f"fb:apif:{fixture.get('id', '')}",
            sport=SportType.FOOTBALL,
            source_id=self.source_id,
            home_team=str(home),
            away_team=str(away),
            status=st,
            home_score=int(hs) if hs is not None else None,
            away_score=int(as_) if as_ is not None else None,
            winner=winner,
            observed_at=datetime.now(timezone.utc),
            league=str((item.get("league") or {}).get("name") or ""),
            external_id=str(fixture.get("id", "")),
            elapsed_minute=elapsed,
            kickoff_time=_parse_kickoff(fixture),
        )
