/**
 * Dashboard 前端：WebSocket 事件驱动更新，REST 仅用于翻页与重启。
 */
(function () {
  const WATCHLIST_PAGE_SIZE = 10;
  const BJ_TZ = "Asia/Shanghai";

  const state = {
    watchlist: { items: [], total: 0, page: 1, page_size: WATCHLIST_PAGE_SIZE },
    history: { items: [], total: 0, page: 1, page_size: 5 },
    positions: { items: [], enabled: false, auto_redeem: false, threshold_pct: 99.8 },
    health: [],
    focus: null,
    status: {},
    logs: [],
    geoblocked: false,
  };

  let ws = null;
  let reconnectTimer = null;
  let wlLoading = false;
  let histLoading = false;

  const $ = (id) => document.getElementById(id);

  function setWatchlistLoading(loading) {
    wlLoading = loading;
    $("wl-prev").disabled = loading || state.watchlist.page <= 1;
    const totalPages = Math.max(1, Math.ceil(state.watchlist.total / state.watchlist.page_size));
    $("wl-next").disabled = loading || state.watchlist.page >= totalPages;
    $("wl-page-info").classList.toggle("loading", loading);
  }

  function setHistoryLoading(loading) {
    histLoading = loading;
    $("hist-prev").disabled = loading || state.history.page <= 1;
    const totalPages = Math.max(1, Math.ceil(state.history.total / state.history.page_size));
    $("hist-next").disabled = loading || state.history.page >= totalPages;
    $("hist-page-info").classList.toggle("loading", loading);
  }

  function healthDotClass(item) {
    if (item.status === "disabled" || item.status === "pending") return "warn";
    if (item.ok === true) return "ok";
    if (item.ok === false) return "fail";
    return "unknown";
  }

  /** 解析 Unix 秒 / ISO / 数字字符串 */
  function toDate(ts) {
    if (ts == null || ts === "") return null;
    if (typeof ts === "number") {
      return new Date(ts > 1e12 ? ts : ts * 1000);
    }
    if (typeof ts === "string") {
      const s = ts.trim();
      if (/^\d+(\.\d+)?$/.test(s)) {
        const n = parseFloat(s);
        return new Date(n > 1e12 ? n : n * 1000);
      }
      const d = new Date(s);
      return Number.isNaN(d.getTime()) ? null : d;
    }
    const d = new Date(ts);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  /** 统一格式化为北京时间（UTC+8） */
  function fmtBeijing(ts, withSeconds = false) {
    const d = toDate(ts);
    if (!d) return ts == null || ts === "" ? "—" : String(ts);
    const opts = {
      timeZone: BJ_TZ,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    };
    if (withSeconds) opts.second = "2-digit";
    return d.toLocaleString("zh-CN", opts);
  }

  function fmtHistoryTime(row) {
    if (row.created_at_display) return row.created_at_display;
    return fmtBeijing(row.created_at, true);
  }

  function fmtTime(ts) {
    return fmtBeijing(ts, true);
  }

  function fmtStart(iso) {
    return fmtBeijing(iso, false);
  }

  function renderHealth() {
    const grid = $("health-grid");
    grid.innerHTML = "";
    for (const item of state.health) {
      const label = item.label || item.id;
      const card = document.createElement("div");
      card.className = "health-card";
      card.title = item.error || "";
      card.dataset.id = item.id;
      card.innerHTML = `
        <span class="dot ${healthDotClass(item)}"></span>
        <div>
          <div>${label}</div>
          <div class="muted" style="font-size:0.7rem">${healthSubtext(item)}</div>
        </div>`;
      grid.appendChild(card);
    }
  }

  function healthSubtext(item) {
    if (item.error) return item.error.slice(0, 48);
    if (item.last_ts) return fmtTime(item.last_ts);
    return "—";
  }

  function patchHealthItem(item) {
    const idx = state.health.findIndex((h) => h.id === item.id);
    if (idx >= 0) state.health[idx] = { ...state.health[idx], ...item };
    else state.health.push(item);
    const card = document.querySelector(`.health-card[data-id="${item.id}"]`);
    if (card) {
      card.querySelector(".dot").className = `dot ${healthDotClass(item)}`;
      const sub = card.querySelector(".muted");
      if (sub) sub.textContent = healthSubtext(item);
      card.title = item.error || "";
    } else {
      renderHealth();
    }
  }

  function renderFocus() {
    const el = $("focus-content");
    const f = state.focus;
    if (!f) {
      el.className = "focus-content muted";
      el.textContent = "暂无监听中的比赛";
      return;
    }
    el.className = "focus-content";
    const fx = f.fixture || {};
    const score =
      fx.home_score != null && fx.away_score != null
        ? `${fx.home_score} - ${fx.away_score}`
        : "—";
    const progress =
      fx.elapsed_minute != null
        ? `${fx.elapsed_minute}'`
        : fx.period != null
          ? `第 ${fx.period} 节`
          : fx.status || "";
    const startBj = fmtStart(f.game_start_time);
    const focusHint = f.armed
      ? "已进入买入监控（ARMED），价格进入窗口即下单"
      : fx.status === "live"
        ? "比赛进行中，等待终局信号"
        : "最近开赛监听场次";
    el.innerHTML = `
      <div class="focus-team">${f.team_a || "?"}</div>
      <div class="focus-score">${score}</div>
      <div class="focus-team">${f.team_b || "?"}</div>
      <div class="focus-meta">
        开赛(北京) ${startBj} · ${focusHint}
      </div>
      <div class="focus-meta muted">
        ${f.question || ""} · ${progress} · Yes ${f.yes_ask ?? "—"} / No ${f.no_ask ?? "—"}
        ${f.armed ? ' · <span class="tag armed">ARMED</span>' : ""}
      </div>`;
  }

  function renderWatchlist() {
    const tbody = $("watchlist-body");
    tbody.innerHTML = "";
    for (const row of state.watchlist.items) {
      const tr = document.createElement("tr");
      tr.dataset.marketId = row.market_id;
      const fx = row.fixture || {};
      const score =
        fx.home_score != null && fx.away_score != null
          ? `${fx.home_score}-${fx.away_score}`
          : "—";
      const prog =
        fx.elapsed_minute != null
          ? `${fx.elapsed_minute}'`
          : fx.period != null
            ? `P${fx.period}`
            : "—";
      tr.innerHTML = `
        <td>${fmtStart(row.game_start_time)}</td>
        <td>${row.team_a} vs ${row.team_b}</td>
        <td class="col-score">${score}</td>
        <td class="col-prog">${prog}</td>
        <td class="col-yes">${row.yes_ask ?? "—"}</td>
        <td class="col-no">${row.no_ask ?? "—"}</td>
        <td>${row.armed ? '<span class="tag armed">ARMED</span>' : "—"}</td>`;
      tbody.appendChild(tr);
    }
    const totalPages = Math.max(1, Math.ceil(state.watchlist.total / state.watchlist.page_size));
    $("watchlist-total").textContent = state.watchlist.total;
    $("wl-page-info").textContent = `${state.watchlist.page} / ${totalPages}`;
    $("wl-prev").disabled = wlLoading || state.watchlist.page <= 1;
    $("wl-next").disabled = wlLoading || state.watchlist.page >= totalPages;
  }

  function patchWatchlistRows(patches) {
    for (const p of patches) {
      const tr = document.querySelector(`#watchlist-body tr[data-market-id="${p.market_id}"]`);
      if (!tr) continue;
      if (p.fixture) {
        const fx = p.fixture;
        const score =
          fx.home_score != null && fx.away_score != null
            ? `${fx.home_score}-${fx.away_score}`
            : "—";
        const prog =
          fx.elapsed_minute != null
            ? `${fx.elapsed_minute}'`
            : fx.period != null
              ? `P${fx.period}`
              : "—";
        tr.querySelector(".col-score").textContent = score;
        tr.querySelector(".col-prog").textContent = prog;
      }
      if (p.yes_ask !== undefined) tr.querySelector(".col-yes").textContent = p.yes_ask ?? "—";
      if (p.no_ask !== undefined) tr.querySelector(".col-no").textContent = p.no_ask ?? "—";
    }
  }

  function renderHistory() {
    const tbody = $("history-body");
    tbody.innerHTML = "";
    for (const row of state.history.items) {
      const tr = document.createElement("tr");
      const kind =
        row.kind === "success" ? "成交" : row.kind === "redeem" ? "结算" : "错过";
      const tagClass =
        row.kind === "success" ? "success" : row.kind === "redeem" ? "redeem" : "missed";
      tr.innerHTML = `
        <td>${fmtHistoryTime(row)}</td>
        <td><span class="tag ${tagClass}">${kind}</span></td>
        <td>${row.team_a || ""} vs ${row.team_b || ""}</td>
        <td title="${row.detail || ""}">${row.reason || row.event_type || ""}</td>
        <td>${row.price != null && row.price ? row.price : row.notional_usd || "—"}</td>`;
      tbody.appendChild(tr);
    }
    const totalPages = Math.max(1, Math.ceil(state.history.total / state.history.page_size));
    $("history-total").textContent = state.history.total;
    $("hist-page-info").textContent = `${state.history.page} / ${totalPages}`;
    $("hist-prev").disabled = state.history.page <= 1;
    $("hist-next").disabled = state.history.page >= totalPages;
  }

  function prependHistory(item) {
    if (state.history.page !== 1) {
      state.history.total += 1;
      $("history-total").textContent = state.history.total;
      return;
    }
    state.history.items.unshift(item);
    if (state.history.items.length > state.history.page_size) {
      state.history.items.pop();
    }
    state.history.total += 1;
    renderHistory();
  }

  function renderPositions() {
    const tbody = $("positions-body");
    const hint = $("positions-hint");
    tbody.innerHTML = "";
    const p = state.positions;
    if (!p.enabled) {
      hint.textContent = "结算未启用：需 live 模式 + polymarket-arb.env 中 FUNDER/PK，并 pip install -e \".[live]\"";
      return;
    }
    hint.textContent = `自动结算：${p.auto_redeem ? "开" : "关"} · 触发阈值 ${p.threshold_pct}% · EOA 需有 POL 付 gas`;
    for (const row of p.items || []) {
      const tr = document.createElement("tr");
      const status = row.already_redeemed
        ? "已结算"
        : row.is_winner
          ? "胜方"
          : row.redeemable
            ? "可赎回"
            : "—";
      const canRedeem = row.redeemable && !row.already_redeemed;
      tr.innerHTML = `
        <td title="${escapeHtml(row.title || "")}">${escapeHtml((row.title || "").slice(0, 36))}</td>
        <td>${row.size ?? "—"}</td>
        <td>${row.cur_price_pct ?? "—"}</td>
        <td>${status}</td>
        <td>${
          canRedeem
            ? `<button class="btn-mini btn-redeem-one" data-cid="${escapeHtml(row.condition_id)}">结算</button>`
            : "—"
        }</td>`;
      tbody.appendChild(tr);
    }
    tbody.querySelectorAll(".btn-redeem-one").forEach((btn) => {
      btn.addEventListener("click", () => redeemOne(btn.dataset.cid));
    });
  }

  async function loadPositions() {
    try {
      const r = await fetch("/api/positions");
      const data = await r.json();
      state.positions = data;
      renderPositions();
    } catch (e) {
      $("positions-hint").textContent = "持仓加载失败";
    }
  }

  async function redeemOne(conditionId) {
    if (!conditionId || !confirm("确认链上结算该持仓？需消耗少量 POL gas")) return;
    const r = await fetch("/api/redeem", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ condition_id: conditionId }),
    });
    const data = await r.json();
    alert(data.ok ? "结算已提交" : data.error || data.results?.[0]?.detail || "结算失败");
    await loadPositions();
  }

  async function redeemBatch(winnersOnly) {
    const msg = winnersOnly
      ? "结算全部胜方持仓（价格≈100%）？"
      : "结算全部可赎回持仓（含输家清零）？";
    if (!confirm(msg)) return;
    const r = await fetch("/api/redeem", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(
        winnersOnly ? { winners_only: true } : { all_redeemable: true }
      ),
    });
    const data = await r.json();
    const okN = (data.results || []).filter((x) => x.ok).length;
    alert(data.ok ? `完成 ${okN} 笔结算` : data.error || "无持仓可结算");
    await loadPositions();
  }

  function renderStatus() {
    const s = state.status;
    $("runtime-info").innerHTML = `
      <div><span>PID</span> ${s.pid ?? "—"}</div>
      <div><span>运行</span> ${s.uptime_sec != null ? Math.floor(s.uptime_sec / 60) + " 分钟" : "—"}</div>
      <div><span>模式</span> ${s.mode ?? "—"}</div>
      <div><span>代理</span> ${s.proxy_enabled ? "开" : "关"}</div>
      <div><span>Geoblock</span> ${s.geoblocked ? "是" : "否"}</div>
      <div><span>暂停</span> ${s.live_paused ? "是" : "否"}</div>
      <div><span>Armed</span> ${s.armed_count ?? 0}</div>`;
    $("status-bar").textContent = `WS 已连接 · ${s.mode || ""} · armed ${s.armed_count ?? 0} · 自动结算 ${s.auto_redeem_enabled ? "开" : "关"}`;

    const bar = $("alert-bar");
    if (s.geoblocked) {
      bar.textContent = "⚠ 当前被 Geoblock，下单已暂停";
      bar.classList.remove("hidden");
    } else {
      bar.classList.add("hidden");
    }
  }

  function renderLogs() {
    const filter = ($("log-search").value || "").toLowerCase();
    const lines = state.logs.filter((l) => {
      if (!filter) return true;
      return (l.msg || "").toLowerCase().includes(filter);
    });
    const view = $("log-view");
    view.innerHTML = lines
      .map((l) => {
        const cls = l.level === "ERROR" ? "error" : l.level === "WARNING" ? "warning" : "";
        const text = `[${fmtBeijing(l.ts, true)}] ${l.level} ${l.msg || ""}`;
        return `<div class="log-line ${cls}">${escapeHtml(text)}</div>`;
      })
      .join("");
    if ($("log-autoscroll").checked) {
      view.scrollTop = view.scrollHeight;
    }
  }

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function appendLog(line) {
    state.logs.push(line);
    if (state.logs.length > 500) state.logs.shift();
    renderLogs();
  }

  function handleMessage(msg) {
    switch (msg.type) {
      case "snapshot.full":
        state.health = msg.health || [];
        if (msg.watchlist) {
          state.watchlist = msg.watchlist;
          // 服务端 page_size 与前端不一致时，主动拉取正确页
          if (
            (state.watchlist.page_size || 0) < WATCHLIST_PAGE_SIZE &&
            state.watchlist.page === 1
          ) {
            requestWatchlistPage(1);
          }
        }
        state.history = msg.history || state.history;
        state.focus = msg.focus;
        state.status = msg.status || {};
        state.logs = msg.logs || [];
        state.geoblocked = !!state.status.geoblocked;
        renderHealth();
        renderFocus();
        renderWatchlist();
        renderHistory();
        renderStatus();
        renderLogs();
        loadPositions();
        break;
      case "health.update":
        patchHealthItem(msg.item);
        break;
      case "health.critical":
        for (const item of msg.items || []) patchHealthItem(item);
        break;
      case "payment.api":
        patchHealthItem({
          id: "payment_api",
          ok: msg.data?.ok,
          last_ts: msg.data?.last_ts,
          error: msg.data?.ok ? "" : msg.data?.detail,
          label: "CLOB 支付 API",
        });
        break;
      case "risk.updated":
        state.status = msg.data || state.status;
        state.geoblocked = !!state.status.geoblocked;
        renderStatus();
        break;
      case "status.updated":
        state.status = { ...state.status, ...msg.data };
        renderStatus();
        break;
      case "focus.updated":
        state.focus = msg.data;
        renderFocus();
        break;
      case "watchlist.page":
        state.watchlist = msg.data;
        setWatchlistLoading(false);
        renderWatchlist();
        break;
      case "watchlist.patch":
        patchWatchlistRows(msg.items || []);
        break;
      case "watchlist.armed": {
        const tr = document.querySelector(
          `#watchlist-body tr[data-market-id="${msg.data.market_id}"]`
        );
        if (tr) {
          tr.cells[6].innerHTML = msg.data.armed
            ? '<span class="tag armed">ARMED</span>'
            : "—";
        }
        if (state.focus && state.focus.market_id === msg.data.market_id) {
          state.focus.armed = msg.data.armed;
          renderFocus();
        }
        break;
      }
      case "history.new":
        prependHistory(msg.item);
        break;
      case "history.page":
        state.history = msg.data;
        setHistoryLoading(false);
        renderHistory();
        break;
      case "positions.changed":
        loadPositions();
        break;
      case "log.append":
        appendLog(msg.line);
        break;
      default:
        break;
    }
  }

  function connectWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.onmessage = (ev) => {
      try {
        handleMessage(JSON.parse(ev.data));
      } catch (e) {
        console.error(e);
      }
    };
    ws.onclose = () => {
      $("status-bar").textContent = "WS 断开，重连中…";
      reconnectTimer = setTimeout(connectWs, 2000);
    };
    ws.onerror = () => ws.close();
  }

  function wsSend(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
    }
  }

  function requestWatchlistPage(page) {
    const totalPages = Math.max(1, Math.ceil(state.watchlist.total / state.watchlist.page_size));
    if (wlLoading || page < 1 || page > totalPages) return;
    setWatchlistLoading(true);
    wsSend({ type: "watchlist.page", page, page_size: WATCHLIST_PAGE_SIZE });
  }

  function requestHistoryPage(page) {
    const totalPages = Math.max(1, Math.ceil(state.history.total / state.history.page_size));
    if (histLoading || page < 1 || page > totalPages) return;
    setHistoryLoading(true);
    wsSend({ type: "history.page", page });
  }

  $("wl-prev").addEventListener("click", () => {
    requestWatchlistPage(state.watchlist.page - 1);
  });
  $("wl-next").addEventListener("click", () => {
    requestWatchlistPage(state.watchlist.page + 1);
  });
  $("hist-prev").addEventListener("click", () => {
    requestHistoryPage(state.history.page - 1);
  });
  $("hist-next").addEventListener("click", () => {
    requestHistoryPage(state.history.page + 1);
  });

  $("btn-start").addEventListener("click", async () => {
    if (!confirm("确认启动 Bot？")) return;
    $("btn-start").disabled = true;
    try {
      const r = await fetch("/api/start", { method: "POST" });
      const data = await r.json();
      alert(
        data.status === "starting"
          ? "启动指令已发送。若页面仍无法连接，请稍等 20 秒后刷新，或在 Terminal 执行 ./scripts/start_bot.sh"
          : "启动请求已发送"
      );
    } finally {
      $("btn-start").disabled = false;
    }
  });

  $("btn-stop").addEventListener("click", async () => {
    if (
      !confirm(
        "确认停止 Bot？\n\n停止后 Dashboard 将断开，10 小时后再启动请在本页刷新前先在 Terminal 运行 ./scripts/start_bot.sh，或稍后重新打开 Dashboard 并点击启动。"
      )
    ) {
      return;
    }
    $("btn-stop").disabled = true;
    await fetch("/api/stop", { method: "POST" });
    $("status-bar").textContent = "正在停止服务…";
    setTimeout(() => {
      alert("Bot 已停止。需要时再执行 ./scripts/start_bot.sh 或重新打开 Dashboard 后点击「启动 Bot」。");
    }, 800);
  });

  $("btn-restart").addEventListener("click", async () => {
    if (!confirm("确认重启 Bot？")) return;
    await fetch("/api/restart", { method: "POST" });
    alert("重启指令已发送，请稍候刷新页面");
  });

  $("btn-redeem-winners")?.addEventListener("click", () => redeemBatch(true));
  $("btn-redeem-all")?.addEventListener("click", () => redeemBatch(false));
  $("btn-positions-refresh")?.addEventListener("click", () => loadPositions());

  $("log-search").addEventListener("input", renderLogs);

  connectWs();
})();
