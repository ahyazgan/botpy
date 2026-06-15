import { useCallback, useEffect, useMemo, useRef, useState } from "react";

function beep(): void {
  try {
    const AC = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AC) return;
    const ctx = new AC();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.type = "sine";
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.15, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.3);
    osc.start();
    osc.stop(ctx.currentTime + 0.3);
  } catch {
    /* ses çalınamadı — sorun değil */
  }
}

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";
const POLL_MS = 15_000;

type Direction = "bullish" | "bearish" | "neutral";

type NewsItem = {
  id: string;
  source: string;
  title: string;
  url: string;
  published: string | null;
  fetched_at: string;
  coins: string[];
  impact: number;
  direction: Direction;
  reason: string;
  scorer: string;
  symbol: string | null;
  price_24h_pct: number | null;
  price_15m_pct: number | null;
  price_60m_pct: number | null;
  volume_usd: number | null;
  confirmed: boolean;
  price_note: string;
};

type NewsPayload = {
  news: NewsItem[];
  updated_at: string | null;
  error: string | null;
  total_seen: number;
  alert_threshold: number;
};

type Settings = {
  paper_trading: boolean;
  auto_trade: boolean;
  market: "spot" | "futures";
  trade_usdt: number;
  leverage: number;
  max_positions: number;
  auto_min_impact: number;
  auto_require_confirm: boolean;
  cooldown_sec: number;
  use_sl_tp: boolean;
  stop_loss_pct: number;
  take_profit_pct: number;
  trailing_stop_pct: number;
  daily_loss_limit_usdt: number;
  max_total_exposure_usdt: number;
  max_per_coin_usdt: number;
  order_type: "market" | "limit";
  slippage_guard_pct: number;
  min_orderbook_usd: number;
  size_by_impact: boolean;
  time_stop_min: number;
  breakeven_pct: number;
  partial_tp_pct: number;
  partial_tp_frac: number;
  max_open_risk_usdt: number;
  reduce_after_losses: number;
  suppress_losing_sources: boolean;
  min_source_samples: number;
  skip_already_priced_pct: number;
  has_live_keys: boolean;
  open_exposure_usdt: number;
  realized_today: number;
};

type Performance = {
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_pnl: number;
  avg_pnl: number;
  best: number;
  worst: number;
  realized_today: number;
  by_source: Record<string, { count: number; pnl: number; wins: number }>;
  by_news_source: Record<string, { count: number; pnl: number; wins: number }>;
  by_impact: Record<string, { count: number; pnl: number; wins: number }>;
  by_symbol: Record<string, { count: number; pnl: number; wins: number }>;
  recent: Array<{ symbol: string; side: string; pnl: number | null; pnl_pct: number | null; close_reason?: string; source: string }>;
  equity: Array<{ closed_at: string | null; pnl: number; cumulative: number }>;
  max_drawdown: number;
  profit_factor: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  payoff_ratio: number | null;
  sharpe: number | null;
};

type Position = {
  id: string;
  symbol: string;
  side: "long" | "short";
  market: string;
  mode: string;
  usdt: number;
  entry_price: number;
  current_price: number | null;
  pnl: number | null;
  pnl_pct: number | null;
  leverage: number;
  source: string;
  opened_at: string;
  sl_price: number | null;
  tp_price: number | null;
};

type SignalSpan = {
  count: number;
  first_ts: string | null;
  last_ts: string | null;
};

type ArchivedSignal = NewsItem & { ts: string };

type NewsSettings = {
  alert_threshold: number;
  remote_notify: boolean;
  remote_channels_available: boolean;
};

type Risk = {
  open_positions: number;
  max_positions: number;
  total_exposure_usdt: number;
  max_total_exposure_usdt: number;
  per_coin_exposure: Record<string, number>;
  max_per_coin_usdt: number;
  realized_today: number;
  daily_loss_limit_usdt: number;
  trading_halted: boolean;
  paper_trading: boolean;
  auto_trade: boolean;
};

type ScoreStat = { n: number; hit_rate: number; avg_move_pct: number };
type Scorecard = {
  ok: boolean;
  reason?: string;
  n?: number;
  overall?: ScoreStat;
  by_source?: Record<string, ScoreStat>;
  by_impact?: Record<string, ScoreStat>;
};

type AutoPreviewRow = {
  id: string;
  title: string;
  symbol: string | null;
  impact: number;
  direction: Direction;
  would_trade: boolean;
  reason: string;
  side: string | null;
  usdt: number | null;
};

type DailySummary = {
  date: string;
  trades: number;
  wins: number;
  losses: number;
  realized: number;
  best: number;
  worst: number;
  open_positions: number;
  open_exposure_usdt: number;
};

type Health = {
  ok: boolean;
  uptime_sec: number;
  scorer: string;
  treenews: boolean;
  signals_archived: number | null;
  updated_at: string | null;
};

type ClosedTrade = {
  closed_at: string | null;
  symbol: string;
  side: string;
  mode: string;
  usdt: number;
  entry_price: number;
  close_price: number | null;
  pnl: number | null;
  pnl_pct: number | null;
  close_reason?: string;
  source: string;
};

type BacktestResult = {
  ok: boolean;
  mode?: "simple" | "grid" | "walk";
  reason?: string;
  n?: number;
  tested?: number;
  candidates?: number;
  win_rate?: number;
  tp?: number;
  sl?: number;
  timeout?: number;
  avg_net_pct?: number;
  total_pnl_usdt?: number;
  // walk-forward
  params?: { sl: number; tp: number };
  in_sample?: { n: number; win_rate: number; avg_net_pct: number };
  out_of_sample?: { n: number; win_rate: number; avg_net_pct: number };
  degradation?: number | null;
  verdict?: string;
  // grid
  rows?: Array<{ sl: number; tp: number; n: number; win_rate: number; avg_net_pct: number; total_pnl_usdt: number }>;
  best?: { sl: number; tp: number; total_pnl_usdt: number } | null;
  // simple breakdown (edge kalibrasyonu)
  breakdown?: {
    by_impact: Record<string, BucketStat>;
    by_direction: Record<string, BucketStat>;
    by_source: Record<string, BucketStat>;
  };
};

type BucketStat = { n: number; win_rate: number; avg_net_pct: number; total_pnl_usdt: number };

type BacktestRun = {
  id: number;
  ts: string;
  mode: string;
  sl: number | null;
  tp: number | null;
  n: number | null;
  win_rate: number | null;
  total_pnl_usdt: number | null;
  note: string | null;
};

type BacktestMode = "simple" | "grid" | "walk";

function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const sec = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (sec < 60) return `${sec} sn önce`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} dk önce`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} sa önce`;
  return `${Math.floor(hr / 24)} gün önce`;
}

function fmtUptime(sec: number): string {
  if (sec < 60) return `${sec}sn`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}dk`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}sa ${m % 60}dk`;
  return `${Math.floor(h / 24)}g ${h % 24}sa`;
}

function ConnDot({ ok, label, offLabel }: { ok: boolean; label: string; offLabel: string }) {
  return (
    <span title={`${label}: ${ok ? "bağlı" : "yapılandırılmamış"}`} className="inline-flex items-center gap-1">
      <span className={ok ? "text-emerald-400" : "text-zinc-600"}>●</span>
      <span className={ok ? "text-zinc-400" : "text-zinc-600"}>{ok ? label : offLabel}</span>
    </span>
  );
}

function RiskMeter({ label, used, cap, suffix = "USDT" }: { label: string; used: number; cap: number; suffix?: string }) {
  const pct = cap > 0 ? Math.min(100, (used / cap) * 100) : 0;
  const color = pct >= 90 ? "bg-red-500" : pct >= 70 ? "bg-amber-500" : "bg-emerald-500";
  return (
    <div className="rounded-xl border border-white/10 bg-zinc-900/40 p-3">
      <div className="flex items-baseline justify-between text-xs">
        <span className="uppercase text-zinc-500">{label}</span>
        <span className="tabular-nums text-zinc-300">{used.toLocaleString()} / {cap > 0 ? `${cap.toLocaleString()} ${suffix}` : "∞"}</span>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-zinc-800">
        <div className={`h-full ${color}`} style={{ width: `${cap > 0 ? pct : 0}%` }} />
      </div>
    </div>
  );
}

function spanDays(first: string | null, last: string | null): number | null {
  if (!first || !last) return null;
  const a = new Date(first).getTime();
  const b = new Date(last).getTime();
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return Math.max(0, (b - a) / 86_400_000);
}

const DIR_LABEL: Record<Direction, string> = {
  bullish: "🟢 Yükseliş",
  bearish: "🔴 Düşüş",
  neutral: "⚪ Nötr",
};

function fmtUsd(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return "$" + n.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

function ImpactBadge({ impact, direction }: { impact: number; direction: Direction }) {
  const color =
    direction === "bullish"
      ? "bg-emerald-950/60 text-emerald-300 border-emerald-600/40"
      : direction === "bearish"
      ? "bg-red-950/60 text-red-300 border-red-600/40"
      : "bg-zinc-800/60 text-zinc-300 border-zinc-600/40";
  return (
    <span className={`inline-flex items-center gap-1 rounded-lg border px-2.5 py-1 text-sm font-bold tabular-nums ${color}`}>
      ⚡ {impact}/10
    </span>
  );
}

function NumField({ label, value, onSave }: { label: string; value: number; onSave: (v: number) => void }) {
  return (
    <label className="flex items-center justify-between gap-2 text-xs text-zinc-400">
      <span>{label}</span>
      <input
        type="number"
        defaultValue={value}
        key={value}
        onBlur={(e) => {
          const v = parseFloat(e.target.value);
          if (Number.isFinite(v) && v !== value) onSave(v);
        }}
        className="h-7 w-20 rounded-md border border-zinc-700 bg-zinc-800/80 px-2 text-right text-xs tabular-nums text-zinc-200 outline-none focus:border-emerald-500/50"
      />
    </label>
  );
}

export default function App() {
  const [news, setNews] = useState<NewsItem[]>([]);
  const [meta, setMeta] = useState({ total_seen: 0, alert_threshold: 7, updated_at: null as string | null });
  const [settings, setSettings] = useState<Settings | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [totalPnl, setTotalPnl] = useState(0);
  const [perf, setPerf] = useState<Performance | null>(null);
  const [signalSpan, setSignalSpan] = useState<SignalSpan>({ count: 0, first_ts: null, last_ts: null });
  const [archive, setArchive] = useState<ArchivedSignal[]>([]);
  const [showArchive, setShowArchive] = useState(false);
  const [newsSettings, setNewsSettings] = useState<NewsSettings | null>(null);
  const [preview, setPreview] = useState<AutoPreviewRow[] | null>(null);
  const [previewOn, setPreviewOn] = useState(false);
  const [scorecard, setScorecard] = useState<Scorecard | null>(null);
  const [scorecardOn, setScorecardOn] = useState(false);
  const [risk, setRisk] = useState<Risk | null>(null);
  const [daily, setDaily] = useState<DailySummary | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [closed, setClosed] = useState<ClosedTrade[]>([]);
  const [showJournal, setShowJournal] = useState(false);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const [search, setSearch] = useState("");
  const [minImpact, setMinImpact] = useState(0);
  const [onlyAlerts, setOnlyAlerts] = useState(false);
  const [onlyConfirmed, setOnlyConfirmed] = useState(false);
  const [notifyBrowser, setNotifyBrowser] = useState(() =>
    typeof localStorage !== "undefined" && localStorage.getItem("notifyBrowser") === "1");
  const notifiedRef = useRef<Set<string>>(new Set());
  const notifyPrimedRef = useRef(false);
  const [expandedNews, setExpandedNews] = useState<string | null>(null);
  const [showTradeBar, setShowTradeBar] = useState(false);   // mobilde ayar çubuğu drawer'ı
  const [showAdvanced, setShowAdvanced] = useState(false);

  // Backtest paneli (talep üzerine; 15s polling'e dahil DEĞİL — Binance'i yormamak için)
  const [btSl, setBtSl] = useState(3);
  const [btTp, setBtTp] = useState(6);
  const [btMode, setBtMode] = useState<BacktestMode>("simple");
  const [btResult, setBtResult] = useState<BacktestResult | null>(null);
  const [btRunning, setBtRunning] = useState(false);
  const [btRuns, setBtRuns] = useState<BacktestRun[]>([]);

  const load = useCallback(async () => {
    setErr(null);
    try {
      const [nRes, sRes, pRes, perfRes, sigRes, nsRes, riskRes, healthRes, closedRes, sumRes] = await Promise.all([
        fetch(`${API_BASE}/news?limit=200`),
        fetch(`${API_BASE}/settings`),
        fetch(`${API_BASE}/positions`),
        fetch(`${API_BASE}/performance`),
        fetch(`${API_BASE}/signals?limit=50`),
        fetch(`${API_BASE}/news-settings`),
        fetch(`${API_BASE}/risk`),
        fetch(`${API_BASE}/health`),
        fetch(`${API_BASE}/trades/closed?limit=100`),
        fetch(`${API_BASE}/summary`),
      ]);
      if (!nRes.ok) throw new Error(`news ${nRes.status}`);
      const nData: NewsPayload = await nRes.json();
      setNews(nData.news);
      setMeta({ total_seen: nData.total_seen, alert_threshold: nData.alert_threshold, updated_at: nData.updated_at });
      // Tarayıcı bildirimi: panel açıkken gelen YENİ güçlü sinyalleri haber ver
      {
        const strong = nData.news.filter((n) => n.impact >= nData.alert_threshold);
        if (!notifyPrimedRef.current) {
          strong.forEach((n) => notifiedRef.current.add(n.id));   // ilk yük: tohumla, bildirme
          notifyPrimedRef.current = true;
        } else {
          const fresh = strong.filter((n) => !notifiedRef.current.has(n.id));
          fresh.forEach((n) => notifiedRef.current.add(n.id));
          const enabled = localStorage.getItem("notifyBrowser") === "1";
          if (fresh.length > 0 && enabled && "Notification" in window && Notification.permission === "granted") {
            const top = fresh[0];
            new Notification(`⚡ Güç ${top.impact}/10 · ${top.coins.join(", ") || "Genel"}`, {
              body: top.title.slice(0, 140),
            });
            beep();
          }
        }
      }
      if (nData.error) setErr(nData.error);
      if (sRes.ok) setSettings(await sRes.json());
      if (pRes.ok) {
        const pData = await pRes.json();
        setPositions(pData.positions);
        setTotalPnl(pData.total_pnl);
      }
      if (perfRes.ok) setPerf(await perfRes.json());
      if (sigRes.ok) {
        const sig = await sigRes.json();
        setSignalSpan({ count: sig.count ?? 0, first_ts: sig.first_ts ?? null, last_ts: sig.last_ts ?? null });
        setArchive(sig.signals ?? []);
      }
      if (nsRes.ok) setNewsSettings(await nsRes.json());
      if (riskRes.ok) setRisk(await riskRes.json());
      if (healthRes.ok) setHealth(await healthRes.json());
      if (closedRes.ok) setClosed((await closedRes.json()).trades ?? []);
      if (sumRes.ok) setDaily(await sumRes.json());
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Yükleme hatası");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(id);
  }, [load]);

  // Gerçek zamanlıya yakın haber akışı (SSE). 15s poll diğer verileri (pozisyon/
  // ayar/performans) tazeler; haberler buradan ~2s'de gelir. EventSource oto-reconnect.
  useEffect(() => {
    const es = new EventSource(`${API_BASE}/stream`);
    es.onmessage = (e) => {
      try {
        const item = JSON.parse(e.data) as NewsItem;
        setNews((prev) => (prev.some((n) => n.id === item.id) ? prev : [item, ...prev].slice(0, 200)));
      } catch {
        /* bozuk olay — yoksay */
      }
    };
    return () => es.close();
  }, []);

  const patchSettings = async (patch: Partial<Settings>) => {
    try {
      const r = await fetch(`${API_BASE}/settings`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!r.ok) {
        const b = await r.json().catch(() => ({}));
        throw new Error(b.detail ?? String(r.status));
      }
      setSettings(await r.json());
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Ayar değişmedi");
    }
  };

  const patchNewsSettings = async (patch: Partial<NewsSettings>) => {
    try {
      const r = await fetch(`${API_BASE}/news-settings`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!r.ok) throw new Error(String(r.status));
      const ns: NewsSettings = await r.json();
      setNewsSettings(ns);
      setMeta((m) => ({ ...m, alert_threshold: ns.alert_threshold }));
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Haber ayarı değişmedi");
    }
  };

  const trade = async (item: NewsItem, side: "long" | "short") => {
    setBusy(`${item.id}-${side}`);
    try {
      const body = item.symbol ? { symbol: item.symbol, side } : { coin: item.coins[0], side };
      const r = await fetch(`${API_BASE}/trade`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const b = await r.json().catch(() => ({}));
        throw new Error(b.detail ?? String(r.status));
      }
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "İşlem açılamadı");
    } finally {
      setBusy(null);
    }
  };

  const closePos = async (id: string) => {
    setBusy(id);
    try {
      await fetch(`${API_BASE}/positions/${id}`, { method: "DELETE" });
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Kapatılamadı");
    } finally {
      setBusy(null);
    }
  };

  const patchPos = async (id: string, patch: { sl_price?: number; tp_price?: number }) => {
    try {
      const r = await fetch(`${API_BASE}/positions/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!r.ok) throw new Error(String(r.status));
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "SL/TP güncellenemedi");
    }
  };

  const runPreview = async () => {
    setPreviewOn((v) => !v);
    if (preview === null) {
      try {
        const r = await fetch(`${API_BASE}/auto-preview`);
        if (r.ok) setPreview((await r.json()).preview ?? []);
      } catch {
        setPreview([]);
      }
    }
  };

  const toggleNotify = async () => {
    if (!notifyBrowser) {
      if (!("Notification" in window)) {
        setErr("Tarayıcı bildirim desteklemiyor");
        return;
      }
      if (Notification.permission !== "granted") {
        const perm = await Notification.requestPermission();
        if (perm !== "granted") {
          setErr("Bildirim izni verilmedi");
          return;
        }
      }
      localStorage.setItem("notifyBrowser", "1");
      setNotifyBrowser(true);
    } else {
      localStorage.setItem("notifyBrowser", "0");
      setNotifyBrowser(false);
    }
  };

  const runScorecard = async () => {
    setScorecardOn((v) => !v);
    if (scorecard === null) {
      try {
        const r = await fetch(`${API_BASE}/scorecard`);
        setScorecard(r.ok ? await r.json() : { ok: false, reason: `scorecard ${r.status}` });
      } catch {
        setScorecard({ ok: false, reason: "bağlanılamadı" });
      }
    }
  };

  const runBacktest = async () => {
    setBtRunning(true);
    setBtResult(null);
    try {
      const qs = new URLSearchParams({
        sl: String(btSl),
        tp: String(btTp),
        mode: btMode,
        min_impact: String(meta.alert_threshold),
      });
      const r = await fetch(`${API_BASE}/backtest?${qs.toString()}`);
      if (!r.ok) throw new Error(`backtest ${r.status}`);
      setBtResult(await r.json());
      const rr = await fetch(`${API_BASE}/backtest/runs?limit=10`);
      if (rr.ok) setBtRuns((await rr.json()).runs ?? []);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Backtest hatası");
    } finally {
      setBtRunning(false);
    }
  };

  const displayed = useMemo(() => {
    const q = search.trim().toLowerCase();
    const floor = onlyAlerts ? Math.max(minImpact, meta.alert_threshold) : minImpact;
    return news.filter((n) => {
      if (n.impact < floor) return false;
      if (onlyConfirmed && !n.confirmed) return false;
      if (q === "") return true;
      return (
        n.title.toLowerCase().includes(q) ||
        n.coins.some((c) => c.toLowerCase().includes(q)) ||
        n.source.toLowerCase().includes(q)
      );
    });
  }, [news, search, minImpact, onlyAlerts, onlyConfirmed, meta.alert_threshold]);

  const alertCount = useMemo(
    () => news.filter((n) => n.impact >= meta.alert_threshold).length,
    [news, meta.alert_threshold]
  );

  const canShort = settings?.market === "futures";
  const live = settings && !settings.paper_trading;

  return (
    <div className="min-h-screen px-4 pb-16 pt-10 sm:px-8">
      <header className="mx-auto max-w-5xl">
        <div className="flex flex-col gap-6 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <p className="font-display text-xs font-semibold uppercase tracking-[0.2em] text-emerald-400/90">
              Kripto Haber Trade
            </p>
            <h1 className="font-display mt-1 text-3xl font-bold tracking-tight text-white sm:text-4xl">
              Canlı haber radarı
            </h1>
            <p className="mt-2 max-w-xl text-sm text-zinc-400">
              Haberler puanlanır + Binance fiyatıyla teyit edilir. Güç{" "}
              <span className="text-zinc-200">≥ {meta.alert_threshold}</span> olanlar uyarı/işlem olur.
            </p>
          </div>
          <div className="flex flex-col gap-3 sm:items-end">
            <div className="rounded-2xl border border-white/10 bg-zinc-900/80 px-5 py-4 shadow-glow backdrop-blur">
              <p className="text-xs font-medium uppercase tracking-wider text-zinc-500">Güçlü uyarı</p>
              <p className="font-display mt-1 text-3xl font-semibold tabular-nums text-amber-300">{alertCount}</p>
            </div>
            <button
              type="button"
              onClick={() => void load()}
              className="rounded-xl border border-zinc-700 bg-zinc-800/80 px-4 py-2 text-sm font-medium text-zinc-200 transition hover:border-emerald-500/40 hover:bg-zinc-800"
            >
              Şimdi yenile
            </button>
          </div>
        </div>

        {/* İşlem ayar çubuğu — mobilde drawer (toggle), sm+ her zaman açık */}
        {settings && (
          <button
            type="button"
            onClick={() => setShowTradeBar((v) => !v)}
            className="mt-6 w-full rounded-xl border border-zinc-700 bg-zinc-800/80 px-4 py-2 text-sm font-semibold text-zinc-300 sm:hidden"
          >
            ⚙ İşlem ayarları {showTradeBar ? "▴" : "▾"}
          </button>
        )}
        {settings && (
          <div className={`mt-3 ${showTradeBar ? "flex" : "hidden"} flex-wrap items-center gap-3 rounded-2xl border border-white/10 bg-zinc-900/60 p-3 sm:mt-6 sm:flex`}>
            <button
              type="button"
              onClick={() => void patchSettings({ paper_trading: !settings.paper_trading })}
              className={`h-9 rounded-lg border px-3 text-sm font-bold transition ${
                live
                  ? "border-red-500/50 bg-red-950/50 text-red-200"
                  : "border-emerald-500/40 bg-emerald-950/50 text-emerald-200"
              }`}
              title="Paper = simülasyon (risksiz). CANLI = gerçek para."
            >
              {live ? "🔴 CANLI (gerçek para)" : "🟢 PAPER (simülasyon)"}
            </button>
            <button
              type="button"
              onClick={() => void patchSettings({ auto_trade: !settings.auto_trade })}
              className={`h-9 rounded-lg border px-3 text-sm font-semibold transition ${
                settings.auto_trade
                  ? "border-amber-500/50 bg-amber-950/50 text-amber-200"
                  : "border-zinc-700 bg-zinc-800/80 text-zinc-300"
              }`}
            >
              Oto-işlem: {settings.auto_trade ? "AÇIK" : "kapalı"}
            </button>
            <button
              type="button"
              onClick={() => void patchSettings({ size_by_impact: !settings.size_by_impact })}
              title="Conviction sizing: güç 8'de taban, 10'da 1.5x, 7'de 0.75x (oto-işlem boyutu güce göre)"
              className={`h-9 rounded-lg border px-3 text-sm font-semibold transition ${
                settings.size_by_impact
                  ? "border-emerald-500/40 bg-emerald-950/50 text-emerald-200"
                  : "border-zinc-700 bg-zinc-800/80 text-zinc-300"
              }`}
            >
              📊 Güce göre boyut: {settings.size_by_impact ? "AÇIK" : "kapalı"}
            </button>
            <div className="flex items-center gap-1 rounded-lg border border-zinc-700 bg-zinc-800/80 px-1">
              {(["spot", "futures"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => void patchSettings({ market: m })}
                  className={`h-7 rounded-md px-2.5 text-xs font-semibold transition ${
                    settings.market === m ? "bg-emerald-700/60 text-white" : "text-zinc-400"
                  }`}
                >
                  {m === "spot" ? "Spot" : "Futures"}
                </button>
              ))}
            </div>
            <label className="flex items-center gap-2 text-sm text-zinc-400">
              Tutar
              <input
                type="number"
                defaultValue={settings.trade_usdt}
                onBlur={(e) => {
                  const v = parseFloat(e.target.value);
                  if (Number.isFinite(v) && v > 0 && v !== settings.trade_usdt) void patchSettings({ trade_usdt: v });
                }}
                className="h-8 w-20 rounded-md border border-zinc-700 bg-zinc-800/80 px-2 text-sm tabular-nums text-zinc-200 outline-none focus:border-emerald-500/50"
              />
              USDT
            </label>
            {settings.market === "futures" && (
              <span className="text-xs text-zinc-500">kaldıraç {settings.leverage}x</span>
            )}
            {live && !settings.has_live_keys && (
              <span className="text-xs font-semibold text-red-300">⚠ Binance anahtarı yok (.env)</span>
            )}
            <div className="ml-auto flex items-center gap-3 text-xs text-zinc-500">
              <span>Maruziyet: <strong className="text-zinc-300">${settings.open_exposure_usdt}</strong>/{settings.max_total_exposure_usdt || "∞"}</span>
              <span>
                Bugün:{" "}
                <strong className={settings.realized_today >= 0 ? "text-emerald-400" : "text-red-400"}>
                  {settings.realized_today >= 0 ? "+" : ""}{settings.realized_today} USDT
                </strong>
              </span>
              <button
                type="button"
                onClick={() => setShowAdvanced((v) => !v)}
                className="rounded-md border border-zinc-700 px-2 py-1 text-zinc-300 hover:border-emerald-500/40"
              >
                {showAdvanced ? "Gelişmiş ▲" : "Gelişmiş ▼"}
              </button>
            </div>
          </div>
        )}

        {/* Gelişmiş ayarlar: SL/TP + risk + emir kalitesi */}
        {settings && showAdvanced && (
          <div className="mt-3 grid grid-cols-1 gap-4 rounded-2xl border border-white/10 bg-zinc-900/60 p-4 sm:grid-cols-3">
            <div className="space-y-2">
              <p className="text-xs font-semibold uppercase tracking-wider text-emerald-400/80">Otomatik çıkış</p>
              <button
                type="button"
                onClick={() => void patchSettings({ use_sl_tp: !settings.use_sl_tp })}
                className={`w-full rounded-md border px-2 py-1 text-xs font-semibold ${settings.use_sl_tp ? "border-emerald-500/40 bg-emerald-950/40 text-emerald-200" : "border-zinc-700 text-zinc-400"}`}
              >
                SL/TP: {settings.use_sl_tp ? "AÇIK" : "kapalı"}
              </button>
              <NumField label="Stop-loss %" value={settings.stop_loss_pct} onSave={(v) => patchSettings({ stop_loss_pct: v })} />
              <NumField label="Take-profit %" value={settings.take_profit_pct} onSave={(v) => patchSettings({ take_profit_pct: v })} />
              <NumField label="Trailing stop % (0=kapalı)" value={settings.trailing_stop_pct} onSave={(v) => patchSettings({ trailing_stop_pct: v })} />
              <NumField label="Time-stop dk (0=kapalı)" value={settings.time_stop_min} onSave={(v) => patchSettings({ time_stop_min: v })} />
              <NumField label="Breakeven % (0=kapalı)" value={settings.breakeven_pct} onSave={(v) => patchSettings({ breakeven_pct: v })} />
              <NumField label="Kısmi TP % (0=kapalı)" value={settings.partial_tp_pct} onSave={(v) => patchSettings({ partial_tp_pct: v })} />
              <NumField label="Kısmi TP oranı (0-1)" value={settings.partial_tp_frac} onSave={(v) => patchSettings({ partial_tp_frac: v })} />
            </div>
            <div className="space-y-2">
              <p className="text-xs font-semibold uppercase tracking-wider text-amber-400/80">Risk limitleri</p>
              <NumField label="Günlük zarar limiti USDT (0=kapalı)" value={settings.daily_loss_limit_usdt} onSave={(v) => patchSettings({ daily_loss_limit_usdt: v })} />
              <NumField label="Toplam maruziyet USDT" value={settings.max_total_exposure_usdt} onSave={(v) => patchSettings({ max_total_exposure_usdt: v })} />
              <NumField label="Coin başına maruziyet USDT" value={settings.max_per_coin_usdt} onSave={(v) => patchSettings({ max_per_coin_usdt: v })} />
              <NumField label="Max açık pozisyon" value={settings.max_positions} onSave={(v) => patchSettings({ max_positions: v })} />
              <NumField label="Max açık risk USDT (0=kapalı)" value={settings.max_open_risk_usdt} onSave={(v) => patchSettings({ max_open_risk_usdt: v })} />
              <NumField label="Kayıp serisi freni (0=kapalı)" value={settings.reduce_after_losses} onSave={(v) => patchSettings({ reduce_after_losses: v })} />
            </div>
            <div className="space-y-2">
              <p className="text-xs font-semibold uppercase tracking-wider text-sky-400/80">Emir kalitesi</p>
              <div className="flex items-center gap-1 rounded-md border border-zinc-700 bg-zinc-800/80 p-1">
                {(["market", "limit"] as const).map((o) => (
                  <button key={o} type="button" onClick={() => void patchSettings({ order_type: o })}
                    className={`flex-1 rounded px-2 py-0.5 text-xs font-semibold ${settings.order_type === o ? "bg-emerald-700/60 text-white" : "text-zinc-400"}`}>
                    {o === "market" ? "Market" : "Limit"}
                  </button>
                ))}
              </div>
              <NumField label="Slippage koruması % (0=kapalı)" value={settings.slippage_guard_pct} onSave={(v) => patchSettings({ slippage_guard_pct: v })} />
              <NumField label="Min. orderbook likidite USDT" value={settings.min_orderbook_usd} onSave={(v) => patchSettings({ min_orderbook_usd: v })} />
              <NumField label="Oto min. güç (1-10)" value={settings.auto_min_impact} onSave={(v) => patchSettings({ auto_min_impact: v })} />
              <p className="pt-2 text-xs font-semibold uppercase tracking-wider text-violet-400/80">Sinyal kalitesi</p>
              <NumField label="Zaten-fiyatlanmış atla % (0=kapalı)" value={settings.skip_already_priced_pct} onSave={(v) => patchSettings({ skip_already_priced_pct: v })} />
              <button
                type="button"
                onClick={() => void patchSettings({ suppress_losing_sources: !settings.suppress_losing_sources })}
                title="Yeterli örnekte negatif beklentili haber kaynağını oto-işlemde sustur"
                className={`w-full rounded-md border px-2 py-1 text-xs font-semibold ${settings.suppress_losing_sources ? "border-emerald-500/40 bg-emerald-950/40 text-emerald-200" : "border-zinc-700 text-zinc-400"}`}
              >
                Kaybeden kaynağı sustur: {settings.suppress_losing_sources ? "AÇIK" : "kapalı"}
              </button>
              <NumField label="Min. kaynak örneği" value={settings.min_source_samples} onSave={(v) => patchSettings({ min_source_samples: v })} />
            </div>
          </div>
        )}

        {/* Haber ayarları (uyarı eşiği + uzak bildirim) */}
        {newsSettings && (
          <div className="mt-3 flex flex-wrap items-center gap-4 rounded-2xl border border-white/10 bg-zinc-900/60 p-3 text-sm">
            <label className="flex items-center gap-2 text-zinc-400">
              <span>Uyarı eşiği</span>
              <input
                type="range" min={1} max={10} value={newsSettings.alert_threshold}
                onChange={(e) => void patchNewsSettings({ alert_threshold: Number(e.target.value) })}
                className="accent-amber-500"
              />
              <span className="w-6 text-center font-semibold tabular-nums text-amber-300">{newsSettings.alert_threshold}</span>
            </label>
            <button
              type="button"
              disabled={!newsSettings.remote_channels_available}
              onClick={() => void patchNewsSettings({ remote_notify: !newsSettings.remote_notify })}
              title={newsSettings.remote_channels_available
                ? "Telegram/Discord'a güçlü haber + işlem bildirimi gönder"
                : "Uzak kanal yok — .env'de TELEGRAM_BOT_TOKEN/CHAT_ID veya DISCORD_WEBHOOK_URL tanımla"}
              className={`h-8 rounded-lg border px-3 text-xs font-semibold transition disabled:opacity-40 ${
                newsSettings.remote_notify && newsSettings.remote_channels_available
                  ? "border-emerald-500/40 bg-emerald-950/50 text-emerald-200"
                  : "border-zinc-700 bg-zinc-800/80 text-zinc-400"
              }`}
            >
              {newsSettings.remote_channels_available
                ? `📲 Uzak bildirim ${newsSettings.remote_notify ? "açık" : "kapalı"}`
                : "📲 Uzak bildirim (env yok)"}
            </button>
          </div>
        )}

        <div className="mt-4 flex flex-wrap gap-4 text-sm text-zinc-500">
          <span>Taranan: <strong className="text-zinc-300">{meta.total_seen}</strong></span>
          <span className="text-zinc-700">|</span>
          <span>Görüntülenen: <strong className="text-zinc-300">{displayed.length}</strong></span>
          <span className="text-zinc-700">|</span>
          <button
            type="button"
            onClick={() => setShowArchive((v) => !v)}
            title="Kalıcı arşivde biriken güçlü sinyaller (restart'a dayanıklı) — listeyi aç/kapat"
            className="text-zinc-500 transition hover:text-zinc-300"
          >
            Arşiv: <strong className="text-zinc-300">{signalSpan.count}</strong> sinyal
            {(() => {
              const d = spanDays(signalSpan.first_ts, signalSpan.last_ts);
              return d !== null && d >= 0.1 ? (
                <span className="text-zinc-600"> · {d < 1 ? `${(d * 24).toFixed(0)} sa` : `${d.toFixed(1)} gün`}</span>
              ) : null;
            })()}
            <span className="ml-1 text-zinc-600">{showArchive ? "▾" : "▸"}</span>
          </button>
          {meta.updated_at && (
            <>
              <span className="text-zinc-700">|</span>
              <span>Son tarama: <time className="text-zinc-400">{timeAgo(meta.updated_at)}</time></span>
            </>
          )}
          {health && (
            <>
              <span className="text-zinc-700">|</span>
              <span title="Motor sağlığı / uptime / puanlayıcı / kaynak">
                <span className={health.ok ? "text-emerald-400" : "text-red-400"}>●</span>{" "}
                {fmtUptime(health.uptime_sec)} · {health.scorer === "claude" ? "Claude" : "kural"}
                {health.treenews ? " · TreeNews" : ""}
              </span>
            </>
          )}
          <span className="text-zinc-700">|</span>
          <span className="flex items-center gap-1.5" title="Bağlantı durumu: yeşil = yapılandırılmış">
            <ConnDot ok={health?.scorer === "claude"} label="Claude" offLabel="kural" />
            <ConnDot ok={!!newsSettings?.remote_channels_available} label="Telegram/Discord" offLabel="uzak yok" />
            <ConnDot ok={!!settings?.has_live_keys} label="Binance canlı" offLabel="paper" />
          </span>
        </div>

        {err && (
          <div className="mt-4 rounded-xl border border-red-500/30 bg-red-950/40 px-4 py-3 text-sm text-red-200" role="alert">
            {err}
          </div>
        )}

        <div className="mt-6 flex flex-wrap items-center gap-3">
          <input
            type="text"
            placeholder="Coin / kaynak / başlık ara..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="h-10 min-w-[220px] flex-1 rounded-xl border border-zinc-700 bg-zinc-800/80 px-4 text-sm text-zinc-200 placeholder-zinc-600 outline-none focus:border-emerald-500/50"
          />
          <div className="flex items-center gap-2">
            <label className="text-sm text-zinc-500 whitespace-nowrap">
              Min. güç: <span className="text-zinc-300 tabular-nums">{minImpact}</span>
            </label>
            <input type="range" min={0} max={10} value={minImpact} onChange={(e) => setMinImpact(Number(e.target.value))} className="accent-emerald-500" />
          </div>
          <button
            type="button"
            onClick={() => setOnlyAlerts((v) => !v)}
            className={`h-10 rounded-xl border px-4 text-sm font-semibold transition ${
              onlyAlerts ? "border-amber-500/40 bg-amber-950/40 text-amber-200" : "border-zinc-700 bg-zinc-800/80 text-zinc-300"
            }`}
          >
            Sadece güçlü uyarılar
          </button>
          <button
            type="button"
            onClick={() => setOnlyConfirmed((v) => !v)}
            title="Yalnızca fiyatla teyitli (15dk+1s uyumlu) haberler"
            className={`h-10 rounded-xl border px-4 text-sm font-semibold transition ${
              onlyConfirmed ? "border-emerald-500/40 bg-emerald-950/40 text-emerald-200" : "border-zinc-700 bg-zinc-800/80 text-zinc-300"
            }`}
          >
            Sadece teyitli
          </button>
          <button
            type="button"
            onClick={() => void toggleNotify()}
            title="Panel açıkken güçlü sinyal gelince tarayıcı bildirimi + ses"
            className={`h-10 rounded-xl border px-4 text-sm font-semibold transition ${
              notifyBrowser ? "border-emerald-500/40 bg-emerald-950/40 text-emerald-200" : "border-zinc-700 bg-zinc-800/80 text-zinc-300"
            }`}
          >
            {notifyBrowser ? "🔔 Bildirim açık" : "🔕 Bildirim"}
          </button>
        </div>
      </header>

      <main className="mx-auto mt-6 max-w-5xl space-y-3">
        {loading && news.length === 0 ? (
          <div className="rounded-2xl border border-white/10 bg-zinc-900/40 px-4 py-12 text-center text-zinc-500">Yükleniyor…</div>
        ) : displayed.length === 0 ? (
          <div className="rounded-2xl border border-white/10 bg-zinc-900/40 px-4 py-12 text-center text-zinc-500">Haber yok veya filtre sonucu boş.</div>
        ) : (
          displayed.map((n) => {
            const strong = n.impact >= meta.alert_threshold;
            const tradeable = n.coins.length > 0 && n.direction !== "neutral";
            return (
              <article
                key={n.id}
                className={`rounded-2xl border bg-zinc-900/40 p-4 shadow-lg backdrop-blur transition hover:bg-white/[0.03] ${
                  strong ? "border-amber-500/40 shadow-glow" : "border-white/10"
                }`}
              >
                <div className="flex items-start gap-4">
                  <ImpactBadge impact={n.impact} direction={n.direction} />
                  <div className="min-w-0 flex-1">
                    <a href={n.url || "#"} target="_blank" rel="noreferrer" className="block text-zinc-100 hover:text-emerald-300">
                      {n.title}
                    </a>
                    <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
                      <span className="rounded-md bg-zinc-800/80 px-2 py-0.5 text-zinc-300">{n.source}</span>
                      <span>{DIR_LABEL[n.direction]}</span>
                      {n.coins.map((c) => (
                        <span key={c} className="rounded-md border border-emerald-600/30 bg-emerald-950/40 px-1.5 py-0.5 font-semibold text-emerald-300">
                          {c}
                        </span>
                      ))}
                      <span className="text-zinc-700">·</span>
                      <span>{timeAgo(n.published ?? n.fetched_at)}</span>
                      {n.reason && (<><span className="text-zinc-700">·</span><span className="italic text-zinc-500">{n.reason}</span></>)}
                      <button
                        type="button"
                        onClick={() => setExpandedNews((v) => (v === n.id ? null : n.id))}
                        className="text-zinc-500 underline-offset-2 hover:text-zinc-300 hover:underline"
                      >
                        {expandedNews === n.id ? "detayı gizle ▴" : "detay ▾"}
                      </button>
                    </div>

                    {expandedNews === n.id && (
                      <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 rounded-lg border border-white/10 bg-zinc-800/40 p-3 text-xs sm:grid-cols-3">
                        <div><span className="text-zinc-500">Puanlayıcı:</span> <span className="text-zinc-300">{n.scorer === "claude" ? "Claude" : "kural"}</span></div>
                        <div><span className="text-zinc-500">Parite:</span> <span className="text-zinc-300">{n.symbol ?? "—"}</span></div>
                        <div><span className="text-zinc-500">Teyit:</span> <span className={n.confirmed ? "text-emerald-400" : "text-zinc-400"}>{n.confirmed ? "✅ teyitli" : "⏳ yok"}</span></div>
                        <div><span className="text-zinc-500">24s:</span> <span className="text-zinc-300">{n.price_24h_pct !== null ? `${n.price_24h_pct > 0 ? "+" : ""}${n.price_24h_pct}%` : "—"}</span></div>
                        <div><span className="text-zinc-500">15dk:</span> <span className="text-zinc-300">{n.price_15m_pct !== null ? `${n.price_15m_pct > 0 ? "+" : ""}${n.price_15m_pct}%` : "—"}</span></div>
                        <div><span className="text-zinc-500">1s:</span> <span className="text-zinc-300">{n.price_60m_pct !== null ? `${n.price_60m_pct > 0 ? "+" : ""}${n.price_60m_pct}%` : "—"}</span></div>
                        <div className="col-span-2 sm:col-span-3"><span className="text-zinc-500">Hacim:</span> <span className="text-zinc-300">{fmtUsd(n.volume_usd)}</span></div>
                        {n.price_note && <div className="col-span-2 italic text-zinc-400 sm:col-span-3">{n.price_note}</div>}
                        {n.reason && <div className="col-span-2 text-zinc-400 sm:col-span-3"><span className="text-zinc-500">Gerekçe:</span> {n.reason}</div>}
                      </div>
                    )}

                    {/* Fiyat teyidi + işlem (yalnızca güçlü haberlerde) */}
                    {strong && (
                      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-white/5 pt-3">
                        {n.confirmed ? (
                          <span className="rounded-md border border-emerald-500/40 bg-emerald-950/50 px-2 py-0.5 text-xs font-bold text-emerald-300">✅ TEYİTLİ</span>
                        ) : (
                          <span className="rounded-md border border-zinc-600/40 bg-zinc-800/60 px-2 py-0.5 text-xs text-zinc-400">⏳ teyit yok</span>
                        )}
                        {n.price_24h_pct !== null && (
                          <span className="text-xs text-zinc-500">
                            24s <span className={n.price_24h_pct >= 0 ? "text-emerald-400" : "text-red-400"}>{n.price_24h_pct > 0 ? "+" : ""}{n.price_24h_pct}%</span>
                          </span>
                        )}
                        {n.price_15m_pct !== null && (
                          <span className="text-xs text-zinc-500">
                            15dk <span className={n.price_15m_pct >= 0 ? "text-emerald-400" : "text-red-400"}>{n.price_15m_pct > 0 ? "+" : ""}{n.price_15m_pct}%</span>
                          </span>
                        )}
                        {n.price_60m_pct !== null && (
                          <span className="text-xs text-zinc-500" title="Çoklu zaman dilimi: ~1 saatlik hareket">
                            1s <span className={n.price_60m_pct >= 0 ? "text-emerald-400" : "text-red-400"}>{n.price_60m_pct > 0 ? "+" : ""}{n.price_60m_pct}%</span>
                          </span>
                        )}
                        {n.price_15m_pct !== null && n.price_60m_pct !== null && n.direction !== "neutral" && (() => {
                          const aligned = n.direction === "bullish"
                            ? n.price_15m_pct >= 0 && n.price_60m_pct >= 0
                            : n.price_15m_pct <= 0 && n.price_60m_pct <= 0;
                          return (
                            <span className={`text-xs ${aligned ? "text-emerald-400" : "text-amber-400"}`} title="15dk ve 1s yön uyumu">
                              {aligned ? "✓ tf uyumlu" : "⚠ tf ayrık"}
                            </span>
                          );
                        })()}
                        {n.volume_usd !== null && <span className="text-xs text-zinc-600">hacim {fmtUsd(n.volume_usd)}</span>}
                        {n.price_note && <span className="text-xs italic text-zinc-500">· {n.price_note}</span>}

                        {tradeable && (
                          <div className="ml-auto flex gap-1.5">
                            <button
                              type="button"
                              disabled={busy === `${n.id}-long`}
                              onClick={() => void trade(n, "long")}
                              className="rounded-lg border border-emerald-600/40 bg-emerald-950/50 px-3 py-1 text-xs font-semibold text-emerald-300 transition hover:bg-emerald-900/60 disabled:opacity-40"
                            >
                              {busy === `${n.id}-long` ? "…" : "AL (Long)"}
                            </button>
                            {canShort && (
                              <button
                                type="button"
                                disabled={busy === `${n.id}-short`}
                                onClick={() => void trade(n, "short")}
                                className="rounded-lg border border-red-600/40 bg-red-950/50 px-3 py-1 text-xs font-semibold text-red-300 transition hover:bg-red-900/60 disabled:opacity-40"
                              >
                                {busy === `${n.id}-short` ? "…" : "SAT (Short)"}
                              </button>
                            )}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </article>
            );
          })
        )}
      </main>

      {/* Açık pozisyonlar */}
      {positions.length > 0 && (
        <section className="mx-auto mt-10 max-w-5xl">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-lg font-semibold text-white">
              Açık pozisyonlar <span className="ml-2 text-sm font-normal text-zinc-500">({positions.length})</span>
            </h2>
            <span className={`text-sm font-semibold ${totalPnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
              Toplam P&L: {totalPnl >= 0 ? "+" : ""}{totalPnl.toFixed(2)} USDT
            </span>
          </div>
          <div className="overflow-hidden rounded-2xl border border-white/10 bg-zinc-900/40 shadow-xl backdrop-blur">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-left text-sm">
                <thead>
                  <tr className="border-b border-white/10 bg-zinc-900/90 text-xs uppercase text-zinc-500">
                    <th className="px-4 py-3">Coin</th>
                    <th className="px-4 py-3">Yön</th>
                    <th className="px-4 py-3">Mod</th>
                    <th className="px-4 py-3">Tutar</th>
                    <th className="px-4 py-3">Giriş</th>
                    <th className="px-4 py-3">SL / TP</th>
                    <th className="px-4 py-3">Şimdi</th>
                    <th className="px-4 py-3">P&L</th>
                    <th className="px-4 py-3 text-center">Kapat</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((p) => (
                    <tr key={p.id} className="border-b border-white/5 hover:bg-white/[0.03]">
                      <td className="px-4 py-3 font-semibold text-zinc-200">{p.symbol}</td>
                      <td className="px-4 py-3">
                        <span className={`rounded-md px-2 py-0.5 text-xs font-bold ${p.side === "long" ? "bg-emerald-950/60 text-emerald-300" : "bg-red-950/60 text-red-300"}`}>
                          {p.side === "long" ? "LONG" : "SHORT"}{p.leverage > 1 ? ` ${p.leverage}x` : ""}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <span className={`text-xs ${p.mode === "live" ? "text-red-300" : "text-emerald-300"}`}>
                          {p.mode === "live" ? "CANLI" : "paper"}{p.source === "auto" ? " · oto" : ""}
                        </span>
                      </td>
                      <td className="px-4 py-3 tabular-nums text-zinc-300">${p.usdt}</td>
                      <td className="px-4 py-3 tabular-nums text-zinc-400">{p.entry_price}</td>
                      <td className="px-4 py-3 text-xs tabular-nums">
                        <div className="flex items-center gap-1">
                          <input
                            type="number" step="any" defaultValue={p.sl_price ?? ""} key={`sl-${p.id}-${p.sl_price}`}
                            title="Stop-loss fiyatı (0 = kaldır, Enter/blur ile kaydet)"
                            onBlur={(e) => { const v = parseFloat(e.target.value); if (!Number.isNaN(v) && v !== (p.sl_price ?? NaN)) void patchPos(p.id, { sl_price: v }); }}
                            onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                            className="h-6 w-16 rounded border border-zinc-700 bg-zinc-800/80 px-1 text-right text-red-300 outline-none focus:border-red-500/50"
                          />
                          <span className="text-zinc-600">/</span>
                          <input
                            type="number" step="any" defaultValue={p.tp_price ?? ""} key={`tp-${p.id}-${p.tp_price}`}
                            title="Take-profit fiyatı (0 = kaldır, Enter/blur ile kaydet)"
                            onBlur={(e) => { const v = parseFloat(e.target.value); if (!Number.isNaN(v) && v !== (p.tp_price ?? NaN)) void patchPos(p.id, { tp_price: v }); }}
                            onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                            className="h-6 w-16 rounded border border-zinc-700 bg-zinc-800/80 px-1 text-right text-emerald-300 outline-none focus:border-emerald-500/50"
                          />
                        </div>
                      </td>
                      <td className="px-4 py-3 tabular-nums text-zinc-300">{p.current_price ?? "—"}</td>
                      <td className="px-4 py-3 tabular-nums">
                        {p.pnl === null ? (
                          <span className="text-zinc-500">—</span>
                        ) : (
                          <span className={p.pnl >= 0 ? "text-emerald-400" : "text-red-400"}>
                            {p.pnl >= 0 ? "+" : ""}{p.pnl} USDT
                            {p.pnl_pct !== null && <span className="ml-1 text-xs opacity-70">({p.pnl_pct >= 0 ? "+" : ""}{p.pnl_pct}%)</span>}
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-center">
                        <button
                          type="button"
                          disabled={busy === p.id}
                          onClick={() => void closePos(p.id)}
                          className="rounded-lg border border-zinc-600/40 bg-zinc-800/60 px-3 py-1 text-xs font-medium text-zinc-300 transition hover:border-red-500/40 hover:text-red-300 disabled:opacity-40"
                        >
                          {busy === p.id ? "…" : "Kapat"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      )}

      {/* Risk / maruziyet */}
      {risk && (
        <section className="mx-auto mt-10 max-w-5xl">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-lg font-semibold text-white">Risk &amp; maruziyet</h2>
            <div className="flex items-center gap-3">
              {daily && (
                <span className="text-xs text-zinc-400" title={`Günün özeti (${daily.date})`}>
                  Bugün: <strong className="text-zinc-200">{daily.trades}</strong> işlem ·{" "}
                  <span className={daily.realized >= 0 ? "text-emerald-400" : "text-red-400"}>
                    {daily.realized >= 0 ? "+" : ""}{daily.realized} USDT
                  </span>
                </span>
              )}
              {risk.trading_halted && (
                <span className="rounded-lg border border-red-500/50 bg-red-950/50 px-3 py-1 text-xs font-bold text-red-200">
                  ⛔ İŞLEM DURDURULDU (günlük zarar limiti)
                </span>
              )}
            </div>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <RiskMeter label="Toplam maruziyet" used={risk.total_exposure_usdt} cap={risk.max_total_exposure_usdt} />
            <RiskMeter label="Açık pozisyon" used={risk.open_positions} cap={risk.max_positions} suffix="adet" />
            <div className="rounded-xl border border-white/10 bg-zinc-900/40 p-3">
              <div className="flex items-baseline justify-between text-xs">
                <span className="uppercase text-zinc-500">Bugünkü P&L / limit</span>
                <span className={`tabular-nums ${risk.realized_today >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                  {risk.realized_today >= 0 ? "+" : ""}{risk.realized_today} / -{risk.daily_loss_limit_usdt || "∞"}
                </span>
              </div>
              <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-zinc-800">
                <div
                  className="h-full bg-red-500"
                  style={{ width: `${risk.daily_loss_limit_usdt > 0 && risk.realized_today < 0 ? Math.min(100, (-risk.realized_today / risk.daily_loss_limit_usdt) * 100) : 0}%` }}
                />
              </div>
            </div>
          </div>
          {Object.keys(risk.per_coin_exposure).length > 0 && (
            <div className="mt-3 flex flex-wrap gap-2">
              {Object.entries(risk.per_coin_exposure).map(([sym, v]) => (
                <span key={sym} className="rounded-lg border border-white/10 bg-zinc-800/60 px-2 py-1 text-xs text-zinc-300">
                  {sym}: <span className="tabular-nums">{v}</span>
                  {risk.max_per_coin_usdt > 0 && <span className="text-zinc-600"> / {risk.max_per_coin_usdt}</span>}
                </span>
              ))}
            </div>
          )}
        </section>
      )}

      {/* Performans */}
      {perf && perf.total_trades > 0 && (
        <section className="mx-auto mt-10 max-w-5xl">
          <h2 className="mb-3 text-lg font-semibold text-white">Performans <span className="ml-1 text-sm font-normal text-zinc-500">(kapanmış {perf.total_trades} işlem)</span></h2>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div className="rounded-2xl border border-white/10 bg-zinc-900/40 p-4">
              <p className="text-xs uppercase text-zinc-500">Kazanma oranı</p>
              <p className="font-display mt-1 text-2xl font-semibold tabular-nums text-white">%{perf.win_rate}</p>
              <p className="text-xs text-zinc-500">{perf.wins}K / {perf.losses}Z</p>
            </div>
            <div className="rounded-2xl border border-white/10 bg-zinc-900/40 p-4">
              <p className="text-xs uppercase text-zinc-500">Toplam P&L</p>
              <p className={`font-display mt-1 text-2xl font-semibold tabular-nums ${perf.total_pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                {perf.total_pnl >= 0 ? "+" : ""}{perf.total_pnl}
              </p>
              <p className="text-xs text-zinc-500">ort. {perf.avg_pnl} USDT/işlem</p>
            </div>
            <div className="rounded-2xl border border-white/10 bg-zinc-900/40 p-4">
              <p className="text-xs uppercase text-zinc-500">En iyi / en kötü</p>
              <p className="mt-1 text-sm tabular-nums"><span className="text-emerald-400">+{perf.best}</span> / <span className="text-red-400">{perf.worst}</span></p>
            </div>
            <div className="rounded-2xl border border-white/10 bg-zinc-900/40 p-4">
              <p className="text-xs uppercase text-zinc-500">Bugünkü P&L</p>
              <p className={`font-display mt-1 text-2xl font-semibold tabular-nums ${perf.realized_today >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                {perf.realized_today >= 0 ? "+" : ""}{perf.realized_today}
              </p>
            </div>
            <div className="rounded-2xl border border-white/10 bg-zinc-900/40 p-4" title="En büyük tepe-dip düşüş (kümülatif P&L)">
              <p className="text-xs uppercase text-zinc-500">Max düşüş</p>
              <p className="font-display mt-1 text-2xl font-semibold tabular-nums text-red-400">{perf.max_drawdown}</p>
            </div>
            <div className="rounded-2xl border border-white/10 bg-zinc-900/40 p-4" title="Brüt kâr / brüt zarar — &gt;1 kârlı, profesyonel eşik ~1.5+">
              <p className="text-xs uppercase text-zinc-500">Profit factor</p>
              <p className={`font-display mt-1 text-2xl font-semibold tabular-nums ${perf.profit_factor === null ? "text-zinc-400" : perf.profit_factor >= 1 ? "text-emerald-400" : "text-red-400"}`}>
                {perf.profit_factor === null ? "∞" : perf.profit_factor}
              </p>
            </div>
            <div className="rounded-2xl border border-white/10 bg-zinc-900/40 p-4" title="Ort. kazanç / ort. kayıp — &gt;1 ise kazançlar kayıpları geçer">
              <p className="text-xs uppercase text-zinc-500">Payoff oranı</p>
              <p className={`font-display mt-1 text-2xl font-semibold tabular-nums ${perf.payoff_ratio === null ? "text-zinc-400" : perf.payoff_ratio >= 1 ? "text-emerald-400" : "text-red-400"}`}>
                {perf.payoff_ratio ?? "—"}
              </p>
              {perf.avg_win !== null && perf.avg_loss !== null && (
                <p className="text-xs text-zinc-500"><span className="text-emerald-400">+{perf.avg_win}</span> / <span className="text-red-400">{perf.avg_loss}</span></p>
              )}
            </div>
            <div className="rounded-2xl border border-white/10 bg-zinc-900/40 p-4" title="İşlem başına P&L tutarlılığı (ortalama/std); yüksek = istikrarlı">
              <p className="text-xs uppercase text-zinc-500">Sharpe (işlem)</p>
              <p className={`font-display mt-1 text-2xl font-semibold tabular-nums ${perf.sharpe === null ? "text-zinc-400" : perf.sharpe >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                {perf.sharpe ?? "—"}
              </p>
            </div>
          </div>
          {perf.equity.length >= 2 && (
            <div className="mt-3 rounded-2xl border border-white/10 bg-zinc-900/40 p-4">
              <div className="mb-2 flex items-center justify-between">
                <p className="text-xs uppercase text-zinc-500">Kümülatif P&L eğrisi ({perf.equity.length} işlem)</p>
                <p className={`text-sm font-semibold tabular-nums ${perf.total_pnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                  {perf.total_pnl >= 0 ? "+" : ""}{perf.total_pnl} USDT
                </p>
              </div>
              <EquityChart points={perf.equity} />
            </div>
          )}
          {Object.keys(perf.by_source).length > 0 && (
            <div className="mt-3 rounded-2xl border border-white/10 bg-zinc-900/40 p-4">
              <p className="mb-2 text-xs uppercase text-zinc-500">İşlem türüne göre (oto/manuel)</p>
              <div className="flex flex-wrap gap-2">
                {Object.entries(perf.by_source).map(([k, v]) => (
                  <span key={k} className="rounded-lg border border-white/10 bg-zinc-800/60 px-2 py-1 text-xs text-zinc-300">
                    {k}: <span className={v.pnl >= 0 ? "text-emerald-400" : "text-red-400"}>{v.pnl >= 0 ? "+" : ""}{v.pnl}</span>
                    <span className="text-zinc-500"> ({v.wins}/{v.count})</span>
                  </span>
                ))}
              </div>
            </div>
          )}
          {Object.keys(perf.by_impact).filter((k) => k !== "?").length > 0 && (
            <div className="mt-3 rounded-2xl border border-white/10 bg-zinc-900/40 p-4">
              <p className="mb-2 text-xs uppercase text-zinc-500">Güce göre (hangi güç dilimi kazandırıyor?)</p>
              <div className="flex flex-wrap gap-2">
                {Object.entries(perf.by_impact).filter(([k]) => k !== "?").map(([k, v]) => (
                  <span key={k} className="rounded-lg border border-white/10 bg-zinc-800/60 px-2 py-1 text-xs text-zinc-300">
                    güç {k}: <span className={v.pnl >= 0 ? "text-emerald-400" : "text-red-400"}>{v.pnl >= 0 ? "+" : ""}{v.pnl}</span>
                    <span className="text-zinc-500"> ({v.wins}/{v.count})</span>
                  </span>
                ))}
              </div>
            </div>
          )}
          {Object.keys(perf.by_news_source).length > 0 && (
            <div className="mt-3 rounded-2xl border border-white/10 bg-zinc-900/40 p-4">
              <p className="mb-2 text-xs uppercase text-zinc-500">Haber kaynağına göre (hangi kaynak kazandırıyor?)</p>
              <div className="flex flex-wrap gap-2">
                {Object.entries(perf.by_news_source).filter(([k]) => k !== "?").map(([k, v]) => (
                  <span key={k} className="rounded-lg border border-white/10 bg-zinc-800/60 px-2 py-1 text-xs text-zinc-300">
                    {k}: <span className={v.pnl >= 0 ? "text-emerald-400" : "text-red-400"}>{v.pnl >= 0 ? "+" : ""}{v.pnl}</span>
                    <span className="text-zinc-500"> ({v.wins}/{v.count})</span>
                  </span>
                ))}
              </div>
            </div>
          )}
        </section>
      )}

      {/* Backtest / Walk-forward */}
      <section className="mx-auto mt-10 max-w-5xl">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-white">
            Backtest <span className="ml-2 text-sm font-normal text-zinc-500">(arşivdeki güç ≥ {meta.alert_threshold} sinyaller)</span>
          </h2>
          <span className="text-xs text-zinc-500">{signalSpan.count} arşiv sinyali</span>
        </div>
        <div className="rounded-2xl border border-white/10 bg-zinc-900/40 p-4 shadow-xl backdrop-blur">
          <div className="flex flex-wrap items-end gap-4">
            <label className="flex flex-col gap-1 text-xs text-zinc-400">
              <span>Stop-loss %</span>
              <input
                type="number" value={btSl} step={0.5} min={0.5} disabled={btMode !== "simple"}
                onChange={(e) => setBtSl(Number(e.target.value))}
                className="h-9 w-24 rounded-md border border-zinc-700 bg-zinc-800/80 px-2 text-right text-sm tabular-nums text-zinc-200 outline-none focus:border-emerald-500/50 disabled:opacity-40"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-zinc-400">
              <span>Take-profit %</span>
              <input
                type="number" value={btTp} step={0.5} min={0.5} disabled={btMode !== "simple"}
                onChange={(e) => setBtTp(Number(e.target.value))}
                className="h-9 w-24 rounded-md border border-zinc-700 bg-zinc-800/80 px-2 text-right text-sm tabular-nums text-zinc-200 outline-none focus:border-emerald-500/50 disabled:opacity-40"
              />
            </label>
            <div className="flex flex-col gap-1 text-xs text-zinc-400">
              <span>Mod</span>
              <div className="flex overflow-hidden rounded-lg border border-zinc-700">
                {([["simple", "Basit"], ["grid", "Grid"], ["walk", "Walk-forward"]] as [BacktestMode, string][]).map(([m, label]) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => setBtMode(m)}
                    title={m === "grid" ? "Tüm SL/TP kombinasyonlarını dene, en kârlıyı bul" : m === "walk" ? "İlk %70'te optimize, son %30'da test (overfit ölçer)" : "Tek SL/TP ile backtest"}
                    className={`h-9 px-3 text-sm font-semibold transition ${
                      btMode === m ? "bg-emerald-900/50 text-emerald-200" : "bg-zinc-800/80 text-zinc-400 hover:text-zinc-200"
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
            <button
              type="button"
              onClick={() => void runBacktest()}
              disabled={btRunning}
              className="h-9 rounded-lg border border-emerald-500/40 bg-emerald-900/40 px-4 text-sm font-bold text-emerald-200 transition hover:bg-emerald-900/60 disabled:opacity-40"
            >
              {btRunning ? "Çalışıyor…" : "Çalıştır"}
            </button>
            <span className="text-xs text-zinc-600">Binance'ten fiyat indirir, birkaç saniye sürebilir.</span>
          </div>

          {btResult && (
            <div className="mt-4 border-t border-white/10 pt-4">
              {!btResult.ok ? (
                <p className="text-sm text-amber-300">{btResult.reason ?? "Sonuç yok"}</p>
              ) : btResult.mode === "grid" ? (
                <div className="space-y-3">
                  {btResult.best && (
                    <p className="flex flex-wrap items-center gap-2 text-sm text-zinc-300">
                      <span>En kârlı: <strong className="text-emerald-300">SL {btResult.best.sl}% · TP {btResult.best.tp}%</strong>
                        <span className="ml-2 text-emerald-400">{btResult.best.total_pnl_usdt >= 0 ? "+" : ""}{btResult.best.total_pnl_usdt.toFixed(2)} USDT</span>
                        <span className="ml-2 text-zinc-500">({btResult.tested} sinyal)</span>
                      </span>
                      <button
                        type="button"
                        onClick={() => void patchSettings({ stop_loss_pct: btResult.best!.sl, take_profit_pct: btResult.best!.tp })}
                        className="rounded-md border border-emerald-500/40 bg-emerald-950/40 px-2 py-0.5 text-xs font-semibold text-emerald-200 transition hover:bg-emerald-900/60"
                      >
                        Bu SL/TP'yi uygula
                      </button>
                    </p>
                  )}
                  <div className="overflow-x-auto rounded-lg border border-white/10">
                    <table className="w-full min-w-[420px] text-left text-sm">
                      <thead>
                        <tr className="border-b border-white/10 bg-zinc-900/90 text-xs uppercase text-zinc-500">
                          <th className="px-3 py-2">SL %</th>
                          <th className="px-3 py-2">TP %</th>
                          <th className="px-3 py-2">n</th>
                          <th className="px-3 py-2">Kazanma</th>
                          <th className="px-3 py-2">Ort. net %</th>
                          <th className="px-3 py-2">P&L USDT</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(btResult.rows ?? []).map((r, i) => (
                          <tr key={`${r.sl}-${r.tp}`} className={`border-b border-white/5 ${i === 0 ? "bg-emerald-950/30" : "hover:bg-white/[0.03]"}`}>
                            <td className="px-3 py-2 tabular-nums text-zinc-300">{r.sl}</td>
                            <td className="px-3 py-2 tabular-nums text-zinc-300">{r.tp}</td>
                            <td className="px-3 py-2 tabular-nums text-zinc-400">{r.n}</td>
                            <td className="px-3 py-2 tabular-nums text-zinc-400">%{r.win_rate}</td>
                            <td className="px-3 py-2 tabular-nums text-zinc-400">{r.avg_net_pct}</td>
                            <td className={`px-3 py-2 tabular-nums font-semibold ${r.total_pnl_usdt >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                              {r.total_pnl_usdt >= 0 ? "+" : ""}{r.total_pnl_usdt.toFixed(2)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              ) : btResult.mode === "walk" ? (
                <div className="space-y-3">
                  {btResult.in_sample ? (
                    <>
                      <p className="flex flex-wrap items-center gap-2 text-sm text-zinc-300">
                        <span>En iyi (in-sample): <strong className="text-emerald-300">SL {btResult.params?.sl}% · TP {btResult.params?.tp}%</strong>
                          <span className="ml-2 text-zinc-500">({btResult.tested} sinyal)</span>
                        </span>
                        {btResult.params && (
                          <button
                            type="button"
                            onClick={() => void patchSettings({ stop_loss_pct: btResult.params!.sl, take_profit_pct: btResult.params!.tp })}
                            className="rounded-md border border-emerald-500/40 bg-emerald-950/40 px-2 py-0.5 text-xs font-semibold text-emerald-200 transition hover:bg-emerald-900/60"
                          >
                            Bu SL/TP'yi uygula
                          </button>
                        )}
                      </p>
                      <div className="grid grid-cols-2 gap-3 text-sm">
                        <div className="rounded-lg bg-zinc-800/50 p-3">
                          <p className="text-xs uppercase text-zinc-500">In-sample (eğitim)</p>
                          <p className="mt-1 text-zinc-300">n={btResult.in_sample.n} · kazanma %{btResult.in_sample.win_rate} · ort.net %{btResult.in_sample.avg_net_pct}</p>
                        </div>
                        <div className="rounded-lg bg-zinc-800/50 p-3">
                          <p className="text-xs uppercase text-zinc-500">Out-of-sample (test)</p>
                          <p className="mt-1 text-zinc-300">
                            {btResult.out_of_sample && btResult.out_of_sample.n > 0
                              ? `n=${btResult.out_of_sample.n} · kazanma %${btResult.out_of_sample.win_rate} · ort.net %${btResult.out_of_sample.avg_net_pct}`
                              : "işlem yok"}
                          </p>
                        </div>
                      </div>
                      <p className="text-sm">
                        <span className="text-zinc-500">Karar: </span>
                        <span className="font-semibold text-amber-300">{btResult.verdict}</span>
                        {btResult.degradation != null && (
                          <span className="ml-2 text-zinc-500">(zayıflama %{Math.round(btResult.degradation * 100)})</span>
                        )}
                      </p>
                    </>
                  ) : (
                    <p className="text-sm text-amber-300">{btResult.reason ?? "in-sample'da yeterli işlem yok"}</p>
                  )}
                </div>
              ) : (
                <div className="space-y-3">
                  <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
                    <Stat label="Sinyal" value={String(btResult.n ?? 0)} />
                    <Stat label="Kazanma" value={`%${btResult.win_rate ?? 0}`} />
                    <Stat label="TP / SL / timeout" value={`${btResult.tp ?? 0} / ${btResult.sl ?? 0} / ${btResult.timeout ?? 0}`} />
                    <Stat
                      label="Toplam P&L"
                      value={`${(btResult.total_pnl_usdt ?? 0) >= 0 ? "+" : ""}${(btResult.total_pnl_usdt ?? 0).toFixed(2)} USDT`}
                      accent={(btResult.total_pnl_usdt ?? 0) >= 0 ? "pos" : "neg"}
                    />
                  </div>
                  {btResult.breakdown && (btResult.n ?? 0) > 0 && (
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                      <BreakdownTable title="Güce göre" rows={btResult.breakdown.by_impact} />
                      <BreakdownTable title="Yöne göre" rows={btResult.breakdown.by_direction} />
                      <BreakdownTable title="Kaynağa göre" rows={btResult.breakdown.by_source} />
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
          {btRuns.length > 0 && (
            <div className="mt-4 border-t border-white/10 pt-4">
              <p className="mb-2 text-xs uppercase text-zinc-500">Geçmiş çalıştırmalar (karşılaştır)</p>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[480px] text-left text-xs">
                  <thead>
                    <tr className="text-zinc-600">
                      <th className="pb-1 font-normal">zaman</th>
                      <th className="pb-1 font-normal">mod</th>
                      <th className="pb-1 text-right font-normal">SL/TP</th>
                      <th className="pb-1 text-right font-normal">n</th>
                      <th className="pb-1 text-right font-normal">kazanma</th>
                      <th className="pb-1 text-right font-normal">P&L</th>
                    </tr>
                  </thead>
                  <tbody>
                    {btRuns.map((r) => (
                      <tr key={r.id} className="text-zinc-300">
                        <td className="py-0.5 text-zinc-500">{timeAgo(r.ts)}</td>
                        <td className="py-0.5">{r.mode}</td>
                        <td className="py-0.5 text-right tabular-nums text-zinc-400">{r.sl ?? "—"}/{r.tp ?? "—"}</td>
                        <td className="py-0.5 text-right tabular-nums text-zinc-400">{r.n ?? "—"}</td>
                        <td className="py-0.5 text-right tabular-nums">%{r.win_rate ?? "—"}</td>
                        <td className={`py-0.5 text-right tabular-nums ${(r.total_pnl_usdt ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                          {(r.total_pnl_usdt ?? 0) >= 0 ? "+" : ""}{(r.total_pnl_usdt ?? 0).toFixed(1)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      </section>

      {/* Oto-işlem önizleme (dry-run) */}
      <section className="mx-auto mt-10 max-w-5xl">
        <button
          type="button"
          onClick={() => void runPreview()}
          className="mb-3 text-lg font-semibold text-white transition hover:text-zinc-300"
        >
          Oto-işlem önizleme <span className="ml-1 text-sm font-normal text-zinc-500">(dry-run) {previewOn ? "▾" : "▸"}</span>
        </button>
        {previewOn && (
          preview === null ? (
            <p className="rounded-2xl border border-white/10 bg-zinc-900/40 p-4 text-sm text-zinc-500">Yükleniyor…</p>
          ) : preview.length === 0 ? (
            <p className="rounded-2xl border border-white/10 bg-zinc-900/40 p-4 text-sm text-zinc-500">Eşik üstü güçlü haber yok.</p>
          ) : (
            <div className="overflow-hidden rounded-2xl border border-white/10 bg-zinc-900/40 shadow-xl backdrop-blur">
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px] text-left text-sm">
                  <thead>
                    <tr className="border-b border-white/10 bg-zinc-900/90 text-xs uppercase text-zinc-500">
                      <th className="px-4 py-3">Karar</th>
                      <th className="px-4 py-3">Güç</th>
                      <th className="px-4 py-3">Coin</th>
                      <th className="px-4 py-3">Boyut</th>
                      <th className="px-4 py-3">Gerekçe</th>
                      <th className="px-4 py-3">Başlık</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.map((p) => (
                      <tr key={p.id} className="border-b border-white/5 hover:bg-white/[0.03]">
                        <td className="px-4 py-3">
                          <span className={`rounded-md px-2 py-0.5 text-xs font-bold ${p.would_trade ? "bg-emerald-950/60 text-emerald-300" : "bg-zinc-800/60 text-zinc-500"}`}>
                            {p.would_trade ? `${p.side === "long" ? "LONG" : "SHORT"} açar` : "atlar"}
                          </span>
                        </td>
                        <td className="px-4 py-3 tabular-nums text-zinc-400">{p.impact}/10</td>
                        <td className="px-4 py-3 font-semibold text-zinc-200">{p.symbol ?? "—"}</td>
                        <td className="px-4 py-3 tabular-nums text-zinc-400">{p.usdt !== null ? `$${p.usdt}` : "—"}</td>
                        <td className="px-4 py-3 text-xs text-zinc-400">{p.reason}</td>
                        <td className="px-4 py-3 max-w-xs truncate text-xs text-zinc-500" title={p.title}>{p.title}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )
        )}
      </section>

      {/* Sinyal kalitesi (scorecard — ham yön isabeti) */}
      <section className="mx-auto mt-10 max-w-5xl">
        <button
          type="button"
          onClick={() => void runScorecard()}
          className="mb-3 text-lg font-semibold text-white transition hover:text-zinc-300"
        >
          Sinyal kalitesi <span className="ml-1 text-sm font-normal text-zinc-500">(ham yön isabeti) {scorecardOn ? "▾" : "▸"}</span>
        </button>
        {scorecardOn && (
          scorecard === null ? (
            <p className="rounded-2xl border border-white/10 bg-zinc-900/40 p-4 text-sm text-zinc-500">Binance'ten veri indiriliyor…</p>
          ) : !scorecard.ok ? (
            <p className="rounded-2xl border border-white/10 bg-zinc-900/40 p-4 text-sm text-amber-300">{scorecard.reason ?? "Sonuç yok"}</p>
          ) : (
            <div className="space-y-3">
              <div className="rounded-2xl border border-white/10 bg-zinc-900/40 p-4 text-sm">
                <span className="text-zinc-400">Genel ({scorecard.overall?.n} sinyal): </span>
                <strong className={`${(scorecard.overall?.hit_rate ?? 0) >= 50 ? "text-emerald-400" : "text-red-400"}`}>
                  %{scorecard.overall?.hit_rate} isabet
                </strong>
                <span className="text-zinc-500"> · ort. yön hareketi %{scorecard.overall?.avg_move_pct}</span>
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <ScoreTable title="Kaynağa göre" rows={scorecard.by_source ?? {}} />
                <ScoreTable title="Güce göre" rows={scorecard.by_impact ?? {}} />
              </div>
            </div>
          )
        )}
      </section>

      {/* İşlem günlüğü (kapanan işlemler + CSV) */}
      <section className="mx-auto mt-10 max-w-5xl">
        <div className="mb-3 flex items-center justify-between">
          <button
            type="button"
            onClick={() => setShowJournal((v) => !v)}
            className="text-lg font-semibold text-white transition hover:text-zinc-300"
          >
            İşlem günlüğü <span className="ml-1 text-sm font-normal text-zinc-500">({closed.length}) {showJournal ? "▾" : "▸"}</span>
          </button>
          <a
            href={`${API_BASE}/trades/closed.csv`}
            className="rounded-lg border border-zinc-700 bg-zinc-800/80 px-3 py-1.5 text-xs font-semibold text-zinc-300 transition hover:border-emerald-500/40 hover:text-emerald-300"
          >
            ⬇ CSV indir
          </a>
        </div>
        {showJournal && (
          closed.length === 0 ? (
            <p className="rounded-2xl border border-white/10 bg-zinc-900/40 p-4 text-sm text-zinc-500">Henüz kapanmış işlem yok.</p>
          ) : (
            <div className="overflow-hidden rounded-2xl border border-white/10 bg-zinc-900/40 shadow-xl backdrop-blur">
              <div className="overflow-x-auto">
                <table className="w-full min-w-[680px] text-left text-sm">
                  <thead>
                    <tr className="border-b border-white/10 bg-zinc-900/90 text-xs uppercase text-zinc-500">
                      <th className="px-4 py-3">Kapanış</th>
                      <th className="px-4 py-3">Coin</th>
                      <th className="px-4 py-3">Yön</th>
                      <th className="px-4 py-3">Mod</th>
                      <th className="px-4 py-3">Tutar</th>
                      <th className="px-4 py-3">Sebep</th>
                      <th className="px-4 py-3">P&L</th>
                    </tr>
                  </thead>
                  <tbody>
                    {closed.map((t, i) => (
                      <tr key={`${t.symbol}-${t.closed_at}-${i}`} className="border-b border-white/5 hover:bg-white/[0.03]">
                        <td className="px-4 py-3 whitespace-nowrap text-xs text-zinc-500">{timeAgo(t.closed_at)}</td>
                        <td className="px-4 py-3 font-semibold text-zinc-200">{t.symbol}</td>
                        <td className="px-4 py-3 text-xs">
                          <span className={t.side === "long" ? "text-emerald-300" : "text-red-300"}>{t.side === "long" ? "LONG" : "SHORT"}</span>
                        </td>
                        <td className="px-4 py-3 text-xs"><span className={t.mode === "live" ? "text-red-300" : "text-emerald-300"}>{t.mode === "live" ? "CANLI" : "paper"}{t.source === "auto" ? " · oto" : ""}</span></td>
                        <td className="px-4 py-3 tabular-nums text-zinc-400">${t.usdt}</td>
                        <td className="px-4 py-3 text-xs text-zinc-400">{t.close_reason ?? "—"}</td>
                        <td className="px-4 py-3 tabular-nums">
                          {t.pnl === null ? <span className="text-zinc-500">—</span> : (
                            <span className={t.pnl >= 0 ? "text-emerald-400" : "text-red-400"}>
                              {t.pnl >= 0 ? "+" : ""}{t.pnl} USDT
                              {t.pnl_pct !== null && <span className="ml-1 text-xs opacity-70">({t.pnl_pct >= 0 ? "+" : ""}{t.pnl_pct}%)</span>}
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )
        )}
      </section>

      {/* Sinyal arşivi tarayıcısı */}
      {showArchive && (
        <section className="mx-auto mt-10 max-w-5xl">
          <h2 className="mb-3 text-lg font-semibold text-white">
            Sinyal arşivi <span className="ml-2 text-sm font-normal text-zinc-500">(son {archive.length} / {signalSpan.count})</span>
          </h2>
          {archive.length === 0 ? (
            <p className="rounded-2xl border border-white/10 bg-zinc-900/40 p-4 text-sm text-zinc-500">
              Henüz arşivlenmiş sinyal yok — motor güçlü haber yakaladıkça burada birikir.
            </p>
          ) : (
            <div className="overflow-hidden rounded-2xl border border-white/10 bg-zinc-900/40 shadow-xl backdrop-blur">
              <div className="overflow-x-auto">
                <table className="w-full min-w-[680px] text-left text-sm">
                  <thead>
                    <tr className="border-b border-white/10 bg-zinc-900/90 text-xs uppercase text-zinc-500">
                      <th className="px-4 py-3">Zaman</th>
                      <th className="px-4 py-3">Güç</th>
                      <th className="px-4 py-3">Yön</th>
                      <th className="px-4 py-3">Coin</th>
                      <th className="px-4 py-3">Kaynak</th>
                      <th className="px-4 py-3">Teyit</th>
                      <th className="px-4 py-3">Başlık</th>
                    </tr>
                  </thead>
                  <tbody>
                    {archive.map((s) => (
                      <tr key={s.id} className="border-b border-white/5 hover:bg-white/[0.03]">
                        <td className="px-4 py-3 whitespace-nowrap text-xs text-zinc-500">{timeAgo(s.published ?? s.fetched_at ?? s.ts)}</td>
                        <td className="px-4 py-3"><ImpactBadge impact={s.impact} direction={s.direction} /></td>
                        <td className="px-4 py-3 whitespace-nowrap text-xs">{DIR_LABEL[s.direction]}</td>
                        <td className="px-4 py-3 text-xs font-semibold text-zinc-300">{s.coins.length ? s.coins.join(", ") : "—"}</td>
                        <td className="px-4 py-3 text-xs text-zinc-400">{s.source}</td>
                        <td className="px-4 py-3 text-xs">{s.confirmed ? <span className="text-emerald-400">✅</span> : <span className="text-zinc-600">⏳</span>}</td>
                        <td className="px-4 py-3 max-w-md truncate text-xs text-zinc-400" title={s.title}>
                          {s.url ? <a href={s.url} target="_blank" rel="noreferrer" className="hover:text-emerald-300">{s.title}</a> : s.title}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

function EquityChart({ points }: { points: Array<{ cumulative: number }> }) {
  if (points.length < 2) return null;
  const W = 600;
  const H = 120;
  const pad = 6;
  const vals = points.map((p) => p.cumulative);
  const min = Math.min(0, ...vals);
  const max = Math.max(0, ...vals);
  const range = max - min || 1;
  const x = (i: number) => pad + (i / (points.length - 1)) * (W - 2 * pad);
  const y = (v: number) => pad + (1 - (v - min) / range) * (H - 2 * pad);
  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.cumulative).toFixed(1)}`).join(" ");
  const last = vals[vals.length - 1];
  const color = last >= 0 ? "#34d399" : "#f87171";
  const area = `${line} L${x(points.length - 1).toFixed(1)},${y(min).toFixed(1)} L${x(0).toFixed(1)},${y(min).toFixed(1)} Z`;
  const zeroY = y(0);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-28 w-full" preserveAspectRatio="none" role="img" aria-label="Kümülatif P&L eğrisi">
      <line x1={pad} y1={zeroY} x2={W - pad} y2={zeroY} stroke="#3f3f46" strokeWidth={1} strokeDasharray="3 3" />
      <path d={area} fill={color} fillOpacity={0.12} />
      <path d={line} fill="none" stroke={color} strokeWidth={2} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function ScoreTable({ title, rows }: { title: string; rows: Record<string, ScoreStat> }) {
  const entries = Object.entries(rows);
  if (entries.length === 0) return null;
  return (
    <div className="rounded-lg border border-white/10 bg-zinc-800/40 p-3">
      <p className="mb-2 text-xs uppercase text-zinc-500">{title}</p>
      <div className="overflow-x-auto">
      <table className="w-full text-left text-xs">
        <thead>
          <tr className="text-zinc-600">
            <th className="pb-1 font-normal">grup</th>
            <th className="pb-1 text-right font-normal">n</th>
            <th className="pb-1 text-right font-normal">isabet</th>
            <th className="pb-1 text-right font-normal">ort. hareket</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([k, v]) => (
            <tr key={k} className="text-zinc-300">
              <td className="py-0.5">{k}</td>
              <td className="py-0.5 text-right tabular-nums text-zinc-400">{v.n}</td>
              <td className={`py-0.5 text-right tabular-nums ${v.hit_rate >= 50 ? "text-emerald-400" : "text-red-400"}`}>%{v.hit_rate}</td>
              <td className={`py-0.5 text-right tabular-nums ${v.avg_move_pct >= 0 ? "text-emerald-400" : "text-red-400"}`}>{v.avg_move_pct >= 0 ? "+" : ""}{v.avg_move_pct}</td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </div>
  );
}

function BreakdownTable({ title, rows }: { title: string; rows: Record<string, BucketStat> }) {
  const entries = Object.entries(rows);
  if (entries.length === 0) return null;
  return (
    <div className="rounded-lg border border-white/10 bg-zinc-800/40 p-3">
      <p className="mb-2 text-xs uppercase text-zinc-500">{title}</p>
      <div className="overflow-x-auto">
      <table className="w-full text-left text-xs">
        <thead>
          <tr className="text-zinc-600">
            <th className="pb-1 font-normal">grup</th>
            <th className="pb-1 text-right font-normal">n</th>
            <th className="pb-1 text-right font-normal">kazanma</th>
            <th className="pb-1 text-right font-normal">P&L</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([k, v]) => (
            <tr key={k} className="text-zinc-300">
              <td className="py-0.5">{k}</td>
              <td className="py-0.5 text-right tabular-nums text-zinc-400">{v.n}</td>
              <td className="py-0.5 text-right tabular-nums">%{v.win_rate}</td>
              <td className={`py-0.5 text-right tabular-nums ${v.total_pnl_usdt >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                {v.total_pnl_usdt >= 0 ? "+" : ""}{v.total_pnl_usdt.toFixed(1)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </div>
  );
}

function Stat({ label, value, accent }: { label: string; value: string; accent?: "pos" | "neg" }) {
  const color = accent === "pos" ? "text-emerald-400" : accent === "neg" ? "text-red-400" : "text-zinc-200";
  return (
    <div className="rounded-lg bg-zinc-800/50 p-3">
      <p className="text-xs uppercase text-zinc-500">{label}</p>
      <p className={`mt-1 font-semibold tabular-nums ${color}`}>{value}</p>
    </div>
  );
}
