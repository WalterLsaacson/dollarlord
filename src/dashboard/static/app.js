/**
 * Dashboard 前端：WebSocket 事件驱动更新，REST 用于结算与重启。
 */
(function () {
  const BJ_TZ = "Asia/Shanghai";
  const LOG_MAX_LINES = 2000;

  const state = {
    watchlist: { items: [], total: 0 },
    history: { items: [], total: 0 },
    positions: { items: [], enabled: false, auto_redeem: false, threshold_pct: 100, chain_ok: true },
    health: [],
    focus: null,
    status: {},
    logs: [],
    logSource: "logs/arb.jsonl",
    geoblocked: false,
  };

  let ws = null;
  let reconnectTimer = null;

  const $ = (id) => document.getElementById(id);

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
      const statusTags = [];
      if (row.armed) statusTags.push('<span class="tag armed">ARMED</span>');
      if (row.has_trade) statusTags.push('<span class="tag success">已成交</span>');
      tr.innerHTML = `
        <td>${fmtStart(row.game_start_time)}</td>
        <td>${row.team_a} vs ${row.team_b}</td>
        <td class="col-score">${score}</td>
        <td class="col-prog">${prog}</td>
        <td class="col-yes">${row.yes_ask ?? "—"}</td>
        <td class="col-no">${row.no_ask ?? "—"}</td>
        <td>${statusTags.length ? statusTags.join(" ") : "—"}</td>`;
      tbody.appendChild(tr);
    }
    $("watchlist-total").textContent = state.watchlist.total;
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

  function historyMatchup(row) {
    if (row.question) return row.question;
    if (row.team_a || row.team_b) return `${row.team_a || ""} vs ${row.team_b || ""}`.trim();
    return "—";
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
        <td>${historyMatchup(row)}</td>
        <td title="${row.detail || ""}">${row.reason || row.event_type || ""}</td>
        <td>${row.price != null && row.price ? row.price : row.notional_usd || "—"}</td>`;
      tbody.appendChild(tr);
    }
    $("history-total").textContent = state.history.total;
  }

  function prependHistory(item) {
    state.history.items.unshift(item);
    if (state.history.items.length > 10) {
      state.history.items.length = 10;
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
    let hintText = `自动结算：${p.auto_redeem ? "开" : "关"} · 触发阈值 ${p.threshold_pct}%`;
    if (p.chain_ok === false) {
      hintText += " · Polygon RPC 不可用，份额未链上确认（结算需 RPC 恢复）";
    }
    hint.textContent = hintText;
    if (!(p.items || []).length) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="5" style="text-align:center;opacity:.6">暂无持仓</td>`;
      tbody.appendChild(tr);
      return;
    }
    for (const row of p.items || []) {
      const tr = document.createElement("tr");
      const label = row.question || row.title || "—";
      const status = row.already_redeemed
        ? "已结算"
        : row.can_settle
          ? "可结算"
          : row.is_winner
            ? "胜方"
            : row.redeemable
              ? "待定价"
              : "—";
      const chainMark = row.chain_confirmed === false ? " *" : "";
      const canRedeem = row.can_settle && !row.already_redeemed;
      tr.innerHTML = `
        <td title="${escapeHtml(label)}">${escapeHtml(label.slice(0, 48))}</td>
        <td>${row.size ?? "—"}${chainMark}</td>
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
      ? "结算全部胜方持仓（价格 = 100%）？"
      : "结算全部可结算持仓（价格 = 100%）？";
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

  /** 格式化 arb.jsonl 单行 */
  function formatLogLine(l) {
    const ts = fmtBeijing(l.ts, true);
    const { level = "INFO", logger = "arb", msg = "" } = l;
    const extra = { ...l };
    delete extra.ts;
    delete extra.level;
    delete extra.logger;
    delete extra.msg;
    delete extra.exc;
    const keys = Object.keys(extra);
    const extraStr = keys.length ? " " + JSON.stringify(extra) : "";
    return `[${ts}] ${level} ${logger} ${msg}${extraStr}`;
  }

  function renderLogs() {
    const filter = ($("log-search").value || "").toLowerCase();
    const lines = state.logs.filter((l) => {
      if (!filter) return true;
      return formatLogLine(l).toLowerCase().includes(filter);
    });
    const view = $("log-view");
    view.innerHTML = lines
      .map((l) => {
        const cls = l.level === "ERROR" ? "error" : l.level === "WARNING" ? "warning" : "";
        return `<div class="log-line ${cls}">${escapeHtml(formatLogLine(l))}</div>`;
      })
      .join("");
    if ($("log-autoscroll").checked) {
      view.scrollTop = view.scrollHeight;
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function appendLog(line) {
    state.logs.push(line);
    if (state.logs.length > LOG_MAX_LINES) {
      state.logs.splice(0, state.logs.length - LOG_MAX_LINES);
    }
    renderLogs();
  }

  function handleMessage(msg) {
    switch (msg.type) {
      case "snapshot.full":
        state.health = msg.health || [];
        state.watchlist = msg.watchlist || state.watchlist;
        state.history = msg.history || state.history;
        state.focus = msg.focus;
        state.status = msg.status || {};
        state.logs = msg.logs || [];
        state.logSource = msg.log_source || state.logSource;
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
      case "watchlist.full":
        state.watchlist = msg.data;
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
