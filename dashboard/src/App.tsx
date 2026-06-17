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
  rel_volume: number | null;
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
  tier1_skip_confirm_impact: number;
  use_entry_brain: boolean;
  brain_escalate: boolean;
  brain_self_improve: boolean;
  cooldown_sec: number;
  use_sl_tp: boolean;
  stop_loss_pct: number;
  take_profit_pct: number;
  trailing_stop_pct: number;
  daily_loss_limit_usdt: number;
  max_total_exposure_usdt: number;
  max_per_coin_usdt: number;
  order_type: "market" | "limit";
  exchange_native_stops: boolean;
  reconcile_autoclose: boolean;
  auto_halt_on_anomaly: boolean;
  slippage_guard_pct: number;
  min_orderbook_usd: number;
  size_by_impact: boolean;
  size_by_volume: boolean;
  min_rel_volume: number;
  max_book_frac: number;
  time_stop_min: number;
  breakeven_pct: number;
  partial_tp_pct: number;
  partial_tp_frac: number;
  max_open_risk_usdt: number;
  reduce_after_losses: number;
  suppress_losing_sources: boolean;
  min_source_samples: number;
  skip_already_priced_pct: number;
  halt_trade_on_stale: boolean;
  max_news_age_sec: number;
  max_same_direction: number;
  max_funding_rate_pct: number;
  use_atr_exits: boolean;
  atr_sl_mult: number;
  atr_tp_mult: number;
  has_live_keys: boolean;
  open_exposure_usdt: number;
  realized_today: number;
};

type BrainBand = { band: string; n: number; win_rate: number | null; avg_pnl: number | null };
type BrainScorecard = { samples: number; bands: BrainBand[]; calibrated: boolean | null; escalated_n: number };
type BtSide = { n: number; avg_net_pct: number | null; win_rate: number | null };
type BrainBacktest = {
  ready: boolean; reason?: string; tested?: number;
  mechanical?: BtSide; brain_enter?: BtSide; brain_veto?: BtSide; edge_pct?: number | null;
};
type BrainVetoReview = {
  ready: boolean; reason?: string; n: number;
  avg_net_pct?: number | null; win_rate?: number | null; verdict?: string;
};
type ReadinessCheck = { check: string; status: "pass" | "fail" | "pending"; detail: string };
type Readiness = {
  verdict: string; samples: number; win_rate: number | null; profit_factor: number | null;
  max_drawdown: number | null; checks: ReadinessCheck[]; note: string;
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

type TuningSuggestion = {
  type: string;
  message: string;
  current?: number;
  suggested?: number;
  tier?: string;
  source?: string;
  avg_pnl?: number;
  count?: number;
};

type Tuning = {
  ready: boolean;
  samples: number;
  min_samples: number;
  suggestions: TuningSuggestion[];
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
  brain?: {
    enter: boolean; wait_seconds: number; conviction: number; direction: string;
    sl_tightness: string; hold_minutes: number; reason: string; escalated: boolean;
    scores: Record<string, number>;
  } | null;
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
  ws_connected?: boolean;
  ws_last_msg_age_sec?: number | null;
  feed_stale?: boolean;
  rate_limited?: number;
  trading_halted?: boolean;
  halt_reason?: string;
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
  mode?: "simple" | "grid" | "walk" | "smart";
  reason?: string;
  n?: number;
  tested?: number;
  candidates?: number;
  win_rate?: number;
  tp?: number;
  sl?: number;
  timeout?: number;
  time_stop?: number;
  be_stop?: number;
  partial?: number;
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

type BacktestMode = "simple" | "grid" | "walk" | "smart";

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
  const [tuning, setTuning] = useState<Tuning | null>(null);
  const [pretrade, setPretrade] = useState<(Tuning & { reason?: string; tested?: number }) | null>(null);
  const [pretradeRunning, setPretradeRunning] = useState(false);
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
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const [search, setSearch] = useState("");
  const [minImpact, setMinImpact] = useState(0);
  const [onlyAlerts, setOnlyAlerts] = useState(false);
  const [onlyConfirmed, setOnlyConfirmed] = useState(false);
  // Zaman filtresi: hızlı "son N dakika" (0 = kapalı) + opsiyonel başlangıç/bitiş (HH:MM, yerel saat)
  const [sinceMin, setSinceMin] = useState(0);
  const [timeFrom, setTimeFrom] = useState("");
  const [timeTo, setTimeTo] = useState("");
  const [notifyBrowser, setNotifyBrowser] = useState(() =>
    typeof localStorage !== "undefined" && localStorage.getItem("notifyBrowser") === "1");
  const notifiedRef = useRef<Set<string>>(new Set());
  const notifyPrimedRef = useRef(false);
  const [expandedNews, setExpandedNews] = useState<string | null>(null);
  const [showTradeBar, setShowTradeBar] = useState(false);   // mobilde ayar çubuğu drawer'ı
  const [lightTheme, setLightTheme] = useState(() =>
    typeof localStorage !== "undefined" && localStorage.getItem("theme") === "light");
  const [showAdvanced, setShowAdvanced] = useState(false);

  // Backtest paneli (talep üzerine; 15s polling'e dahil DEĞİL — Binance'i yormamak için)
  const [btSl, setBtSl] = useState(3);
  const [btTp, setBtTp] = useState(6);
  const [btSlip, setBtSlip] = useState(0);
  const [btEntryDelay, setBtEntryDelay] = useState(0);
  const [btMode, setBtMode] = useState<BacktestMode>("simple");
  const [btResult, setBtResult] = useState<BacktestResult | null>(null);
  const [btRunning, setBtRunning] = useState(false);
  const [btRuns, setBtRuns] = useState<BacktestRun[]>([]);
  const [brainSc, setBrainSc] = useState<BrainScorecard | null>(null);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [brainBt, setBrainBt] = useState<BrainBacktest | null>(null);
  const [brainBtRunning, setBrainBtRunning] = useState(false);
  const runBrainBacktest = async () => {
    setBrainBtRunning(true);
    try {
      const r = await fetch(`${API_BASE}/brain-backtest`);
      if (!r.ok) throw new Error(`brain-backtest ${r.status}`);
      setBrainBt(await r.json());
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Beyin backtest hatası");
    } finally {
      setBrainBtRunning(false);
    }
  };
  const [brainVeto, setBrainVeto] = useState<BrainVetoReview | null>(null);
  const [brainVetoRunning, setBrainVetoRunning] = useState(false);
  const runBrainVeto = async () => {
    setBrainVetoRunning(true);
    try {
      const r = await fetch(`${API_BASE}/brain-veto-review`);
      if (!r.ok) throw new Error(`brain-veto-review ${r.status}`);
      setBrainVeto(await r.json());
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Veto denetimi hatası");
    } finally {
      setBrainVetoRunning(false);
    }
  };

  const load = useCallback(async () => {
    setErr(null);
    try {
      const [nRes, sRes, pRes, perfRes, sigRes, nsRes, riskRes, healthRes, closedRes, sumRes, tuningRes, bsRes, rdRes] = await Promise.all([
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
        fetch(`${API_BASE}/tuning`),
        fetch(`${API_BASE}/brain-scorecard`),
        fetch(`${API_BASE}/readiness`),
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
      if (tuningRes.ok) setTuning(await tuningRes.json());
      if (bsRes.ok) setBrainSc(await bsRes.json());
      if (rdRes.ok) setReadiness(await rdRes.json());
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

  const clearHalt = async () => {
    try {
      const r = await fetch(`${API_BASE}/halt/clear`, { method: "POST" });
      if (r.ok) void load();
    } catch {
      setErr("Devre kesici temizlenemedi");
    }
  };

  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(id);
  }, [load]);

  useEffect(() => {
    document.documentElement.classList.toggle("light", lightTheme);
    localStorage.setItem("theme", lightTheme ? "light" : "dark");
  }, [lightTheme]);

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

  const runPretrade = async () => {
    setPretradeRunning(true);
    try {
      const r = await fetch(`${API_BASE}/tuning/pretrade`);
      if (!r.ok) throw new Error(`pretrade ${r.status}`);
      setPretrade(await r.json());
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Ön-bilgi hatası");
    } finally {
      setPretradeRunning(false);
    }
  };

  const [tuningApplying, setTuningApplying] = useState(false);
  const applyTuning = async () => {
    setTuningApplying(true);
    try {
      const r = await fetch(`${API_BASE}/tuning/apply`, { method: "POST" });
      if (!r.ok) throw new Error(`apply ${r.status}`);
      const out = await r.json();
      // ayarları + önerileri tazele
      const [s, t] = await Promise.all([fetch(`${API_BASE}/settings`), fetch(`${API_BASE}/tuning`)]);
      if (s.ok) setSettings(await s.json());
      if (t.ok) setTuning(await t.json());
      setErr(out.applied
        ? `Oto-kalibrasyon uygulandı: ${out.changes.map((c: { field: string; to: unknown }) => `${c.field}→${c.to}`).join(", ")}`
        : "Oto-kalibrasyon: yeterli örnek yok, değişiklik yapılmadı");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Oto-kalibrasyon hatası");
    } finally {
      setTuningApplying(false);
    }
  };

  const applyPreset = async (name: "news" | "safe") => {
    try {
      const r = await fetch(`${API_BASE}/settings/preset/${name}`, { method: "POST" });
      if (!r.ok) {
        const b = await r.json().catch(() => ({}));
        throw new Error(b.detail ?? String(r.status));
      }
      setSettings(await r.json());
      setNotice(name === "news" ? "Haber-trade çıkış preset'i uygulandı" : "Muhafazakâr preset'e dönüldü");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Preset uygulanamadı");
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

  const closeAll = async () => {
    if (!window.confirm("TÜM açık pozisyonlar kapatılsın mı? Bu işlem geri alınamaz.")) return;
    setBusy("close-all");
    try {
      const r = await fetch(`${API_BASE}/positions/close-all`, { method: "POST" });
      if (!r.ok) throw new Error(String(r.status));
      const rep = await r.json();
      setNotice(`⛔ ${rep.count} pozisyon kapatıldı · P&L ${rep.total_pnl >= 0 ? "+" : ""}${rep.total_pnl} USDT${rep.failed ? ` · ${rep.failed} hata` : ""}`);
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Toplu kapatma başarısız");
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

  const [previewBrainRunning, setPreviewBrainRunning] = useState(false);
  const fetchPreview = async (brain: boolean) => {
    try {
      const r = await fetch(`${API_BASE}/auto-preview${brain ? "?brain=true" : ""}`);
      if (r.ok) setPreview((await r.json()).preview ?? []);
    } catch {
      setPreview([]);
    }
  };
  const runPreview = async () => {
    setPreviewOn((v) => !v);
    if (preview === null) await fetchPreview(false);
  };
  const runPreviewBrain = async () => {
    setPreviewBrainRunning(true);
    if (!previewOn) setPreviewOn(true);
    try {
      await fetchPreview(true);
    } finally {
      setPreviewBrainRunning(false);
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
        slip: String(btSlip),
        entry_delay: String(btEntryDelay),
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
    const sinceCutoff = sinceMin > 0 ? Date.now() - sinceMin * 60_000 : null;
    // HH:MM (yerel) → o günün o anına ait epoch ms; geçersizse null
    const hhmmToday = (hhmm: string): number | null => {
      const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm.trim());
      if (!m) return null;
      const d = new Date();
      d.setHours(Number(m[1]), Number(m[2]), 0, 0);
      return d.getTime();
    };
    const fromMs = hhmmToday(timeFrom);
    const toMs = hhmmToday(timeTo);
    return news.filter((n) => {
      if (n.impact < floor) return false;
      if (onlyConfirmed && !n.confirmed) return false;
      if (sinceCutoff !== null || fromMs !== null || toMs !== null) {
        const t = Date.parse(n.published || n.fetched_at || "");
        if (!Number.isNaN(t)) {
          if (sinceCutoff !== null && t < sinceCutoff) return false;
          if (fromMs !== null && t < fromMs) return false;
          if (toMs !== null && t > toMs) return false;
        }
      }
      if (q === "") return true;
      return (
        n.title.toLowerCase().includes(q) ||
        n.coins.some((c) => c.toLowerCase().includes(q)) ||
        n.source.toLowerCase().includes(q)
      );
    });
  }, [news, search, minImpact, onlyAlerts, onlyConfirmed, sinceMin, timeFrom, timeTo, meta.alert_threshold]);

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
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => void load()}
                className="rounded-xl border border-zinc-700 bg-zinc-800/80 px-4 py-2 text-sm font-medium text-zinc-200 transition hover:border-emerald-500/40 hover:bg-zinc-800"
              >
                Şimdi yenile
              </button>
              <button
                type="button"
                onClick={() => setLightTheme((v) => !v)}
                title="Koyu/açık tema"
                className="rounded-xl border border-zinc-700 bg-zinc-800/80 px-3 py-2 text-sm font-medium text-zinc-200 transition hover:border-emerald-500/40 hover:bg-zinc-800"
              >
                {lightTheme ? "🌙" : "☀️"}
              </button>
            </div>
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
            <button
              type="button"
              onClick={() => void patchSettings({ size_by_volume: !settings.size_by_volume })}
              title="Likidite-katmanlı boyut: ince coinde küçül (≥$50M tam, $1-5M 0.4x, <$1M 0.25x). Çıkış-tuzağı önler."
              className={`h-9 rounded-lg border px-3 text-sm font-semibold transition ${
                settings.size_by_volume
                  ? "border-emerald-500/40 bg-emerald-950/50 text-emerald-200"
                  : "border-zinc-700 bg-zinc-800/80 text-zinc-300"
              }`}
            >
              🔊 Hacme göre boyut: {settings.size_by_volume ? "AÇIK" : "kapalı"}
            </button>
            <button
              type="button"
              onClick={() => void patchSettings({ use_entry_brain: !settings.use_entry_brain })}
              title="Giriş beyni: girişin tam anında Claude kararlı yargı (haber+fiyat+geçmiş+portföy). Tier-2 adaylarda çalışır, refleks girişte atlanır."
              className={`h-9 rounded-lg border px-3 text-sm font-semibold transition ${
                settings.use_entry_brain
                  ? "border-violet-500/50 bg-violet-950/50 text-violet-200"
                  : "border-zinc-700 bg-zinc-800/80 text-zinc-300"
              }`}
            >
              🧠 Giriş beyni: {settings.use_entry_brain ? "AÇIK" : "kapalı"}
            </button>
            {settings.use_entry_brain && (
              <button
                type="button"
                onClick={() => void patchSettings({ brain_escalate: !settings.brain_escalate })}
                title="İki-kademeli: kararsız konviksiyonda (0.4-0.6) daha güçlü modele ikinci derin bakış"
                className={`h-9 rounded-lg border px-3 text-sm font-semibold transition ${
                  settings.brain_escalate
                    ? "border-violet-500/50 bg-violet-950/50 text-violet-200"
                    : "border-zinc-700 bg-zinc-800/80 text-zinc-300"
                }`}
              >
                ⬆️ Eskalasyon: {settings.brain_escalate ? "AÇIK" : "kapalı"}
              </button>
            )}
            {settings.use_entry_brain && (
              <button
                type="button"
                onClick={() => void patchSettings({ brain_self_improve: !settings.brain_self_improve })}
                title="Kendini-iyileştirme: kalibrasyondan öğren — negatif conviction dilimini oto-veto et, zayıf dilimde boyutu kıs"
                className={`h-9 rounded-lg border px-3 text-sm font-semibold transition ${
                  settings.brain_self_improve
                    ? "border-violet-500/50 bg-violet-950/50 text-violet-200"
                    : "border-zinc-700 bg-zinc-800/80 text-zinc-300"
                }`}
              >
                🔁 Kendini-iyileştir: {settings.brain_self_improve ? "AÇIK" : "kapalı"}
              </button>
            )}
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
              <NumField label="Tier-1 refleks güç (0=kapalı)" value={settings.tier1_skip_confirm_impact} onSave={(v) => patchSettings({ tier1_skip_confirm_impact: v })} />
              <button
                type="button"
                onClick={() => void patchSettings({ use_atr_exits: !settings.use_atr_exits })}
                title="SL/TP'yi sabit % yerine coin oynaklığına (ATR) göre ölçekle"
                className={`w-full rounded-md border px-2 py-1 text-xs font-semibold ${settings.use_atr_exits ? "border-emerald-500/40 bg-emerald-950/40 text-emerald-200" : "border-zinc-700 text-zinc-400"}`}
              >
                ATR çıkış (volatilite SL/TP): {settings.use_atr_exits ? "AÇIK" : "kapalı"}
              </button>
              {settings.use_atr_exits && (
                <>
                  <NumField label="ATR SL çarpanı" value={settings.atr_sl_mult} onSave={(v) => patchSettings({ atr_sl_mult: v })} />
                  <NumField label="ATR TP çarpanı" value={settings.atr_tp_mult} onSave={(v) => patchSettings({ atr_tp_mult: v })} />
                </>
              )}
              <div className="flex gap-2 pt-1">
                <button type="button" onClick={() => void applyPreset("news")}
                  className="flex-1 rounded-md border border-emerald-500/40 bg-emerald-950/40 px-2 py-1 text-xs font-semibold text-emerald-200 hover:bg-emerald-900/50">
                  ⚡ Haber-trade preset'i
                </button>
                <button type="button" onClick={() => void applyPreset("safe")}
                  className="rounded-md border border-zinc-700 px-2 py-1 text-xs font-semibold text-zinc-400 hover:bg-zinc-800">
                  Muhafazakâr
                </button>
              </div>
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
              <button
                type="button"
                onClick={() => void patchSettings({ exchange_native_stops: !settings.exchange_native_stops })}
                title="Canlıda borsaya DURAN SL/TP emri koy — bot çökse/internet gitse bile pozisyon korunur (yalnız canlı mod)"
                className={`w-full rounded-md border px-2 py-1 text-xs font-semibold ${settings.exchange_native_stops ? "border-emerald-500/40 bg-emerald-950/40 text-emerald-200" : "border-red-500/40 bg-red-950/40 text-red-200"}`}
              >
                🛡️ Borsa-native stop: {settings.exchange_native_stops ? "AÇIK" : "KAPALI (riskli)"}
              </button>
              <button
                type="button"
                onClick={() => void patchSettings({ reconcile_autoclose: !settings.reconcile_autoclose })}
                title="Açılış mutabakatı: borsada görünmeyen hayalet pozisyonu otomatik kapat (kapalıysa yalnız uyarır)"
                className={`w-full rounded-md border px-2 py-1 text-xs font-semibold ${settings.reconcile_autoclose ? "border-emerald-500/40 bg-emerald-950/40 text-emerald-200" : "border-zinc-700 text-zinc-400"}`}
              >
                🔄 Hayalet pozisyon oto-kapat: {settings.reconcile_autoclose ? "AÇIK" : "kapalı (uyar)"}
              </button>
              <NumField label="Slippage koruması % (0=kapalı)" value={settings.slippage_guard_pct} onSave={(v) => patchSettings({ slippage_guard_pct: v })} />
              <NumField label="Min. orderbook likidite USDT" value={settings.min_orderbook_usd} onSave={(v) => patchSettings({ min_orderbook_usd: v })} />
              <NumField label="Oto min. güç (1-10)" value={settings.auto_min_impact} onSave={(v) => patchSettings({ auto_min_impact: v })} />
              <p className="pt-2 text-xs font-semibold uppercase tracking-wider text-violet-400/80">Sinyal kalitesi · Hacim</p>
              <NumField label="Zaten-fiyatlanmış atla % (0=kapalı)" value={settings.skip_already_priced_pct} onSave={(v) => patchSettings({ skip_already_priced_pct: v })} />
              <NumField label="Min. RVOL — hacim normalin kaçı (0=kapalı)" value={settings.min_rel_volume} onSave={(v) => patchSettings({ min_rel_volume: v })} />
              <NumField label="Maks. orderbook payı 0-1 (örn 0.10, 0=kapalı)" value={settings.max_book_frac} onSave={(v) => patchSettings({ max_book_frac: v })} />
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
            <div className="space-y-2">
              <p className="text-xs font-semibold uppercase tracking-wider text-rose-400/80">Güvenlik kapıları</p>
              <button
                type="button"
                onClick={() => void patchSettings({ halt_trade_on_stale: !settings.halt_trade_on_stale })}
                title="Haber akışı (WS) kopukken yeni oto-işlem açma — kör giriş önleme"
                className={`w-full rounded-md border px-2 py-1 text-xs font-semibold ${settings.halt_trade_on_stale ? "border-emerald-500/40 bg-emerald-950/40 text-emerald-200" : "border-zinc-700 text-zinc-400"}`}
              >
                Akış kopukken durdur: {settings.halt_trade_on_stale ? "AÇIK" : "kapalı"}
              </button>
              <NumField label="Max haber yaşı sn (0=kapalı)" value={settings.max_news_age_sec} onSave={(v) => patchSettings({ max_news_age_sec: v })} />
              <NumField label="Aynı yönde max pozisyon (0=kapalı)" value={settings.max_same_direction} onSave={(v) => patchSettings({ max_same_direction: v })} />
              <NumField label="Max funding % futures (0=kapalı)" value={settings.max_funding_rate_pct} onSave={(v) => patchSettings({ max_funding_rate_pct: v })} />
              <button
                type="button"
                onClick={() => void patchSettings({ auto_halt_on_anomaly: !settings.auto_halt_on_anomaly })}
                title="Anomalide (üst üste emir hatası / korumasız pozisyon) yeni oto-işlemi otomatik durdur"
                className={`w-full rounded-md border px-2 py-1 text-xs font-semibold ${settings.auto_halt_on_anomaly ? "border-emerald-500/40 bg-emerald-950/40 text-emerald-200" : "border-red-500/40 bg-red-950/40 text-red-200"}`}
              >
                ⛔ Anomalide oto-durdur: {settings.auto_halt_on_anomaly ? "AÇIK" : "KAPALI (riskli)"}
              </button>
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
              {health.treenews && health.ws_connected !== undefined && (
                <span title={`TreeNews WS · son mesaj: ${health.ws_last_msg_age_sec != null ? `${health.ws_last_msg_age_sec}s önce` : "—"}`}>
                  <span className={health.ws_connected ? "text-emerald-400" : "text-red-400"}>●</span> WS
                  {health.ws_last_msg_age_sec != null ? ` ${Math.round(health.ws_last_msg_age_sec)}s` : ""}
                </span>
              )}
              {health.feed_stale && (
                <span className="font-semibold text-red-400" title="Haber akışı durdu — WS kopuk veya uzun süredir mesaj yok. Gerçek-zamanlı sinyal alınamıyor olabilir.">⛔ akış durdu</span>
              )}
              {!!health.rate_limited && health.rate_limited > 0 && (
                <span className="text-amber-400" title="Binance rate-limit (429/418) sayısı">⚠ rate-limit ×{health.rate_limited}</span>
              )}
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
        {health?.trading_halted && (
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-red-500/50 bg-red-950/60 px-4 py-3 text-sm text-red-100" role="alert">
            <span>⛔ <b>OPERASYONEL DURDURMA</b> — yeni oto-işlem durdu: {health.halt_reason}</span>
            <button type="button" onClick={() => void clearHalt()}
              className="rounded-md border border-red-400/50 bg-red-900/50 px-3 py-1 text-xs font-semibold hover:bg-red-800/60">
              Temizle & devam et
            </button>
          </div>
        )}
        {notice && (
          <div className="mt-4 flex items-center justify-between rounded-xl border border-emerald-500/30 bg-emerald-950/40 px-4 py-3 text-sm text-emerald-200">
            <span>{notice}</span>
            <button type="button" onClick={() => setNotice(null)} className="text-emerald-400 hover:text-emerald-200">✕</button>
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

        {/* Zaman filtresi: hızlı "son N dk" + saat-dakika aralığı (yerel saat) */}
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="text-sm text-zinc-500 whitespace-nowrap">⏱ Zaman:</span>
          {[
            { label: "Hepsi", v: 0 },
            { label: "5 dk", v: 5 },
            { label: "15 dk", v: 15 },
            { label: "1 sa", v: 60 },
            { label: "4 sa", v: 240 },
            { label: "24 sa", v: 1440 },
          ].map((o) => (
            <button
              key={o.v}
              type="button"
              onClick={() => { setSinceMin(o.v); if (o.v) { setTimeFrom(""); setTimeTo(""); } }}
              className={`h-9 rounded-lg border px-3 text-xs font-semibold transition ${
                sinceMin === o.v && !timeFrom && !timeTo
                  ? "border-emerald-500/40 bg-emerald-950/40 text-emerald-200"
                  : "border-zinc-700 bg-zinc-800/80 text-zinc-400 hover:text-zinc-200"
              }`}
            >
              {o.label}
            </button>
          ))}
          <span className="ml-1 text-xs text-zinc-600">|</span>
          <label className="flex items-center gap-1 text-xs text-zinc-500">
            Saat aralığı
            <input
              type="time"
              value={timeFrom}
              onChange={(e) => { setTimeFrom(e.target.value); setSinceMin(0); }}
              className="h-9 rounded-lg border border-zinc-700 bg-zinc-800/80 px-2 text-xs text-zinc-200 outline-none focus:border-emerald-500/50"
            />
            <span className="text-zinc-600">→</span>
            <input
              type="time"
              value={timeTo}
              onChange={(e) => { setTimeTo(e.target.value); setSinceMin(0); }}
              className="h-9 rounded-lg border border-zinc-700 bg-zinc-800/80 px-2 text-xs text-zinc-200 outline-none focus:border-emerald-500/50"
            />
          </label>
          {(sinceMin > 0 || timeFrom || timeTo) && (
            <button
              type="button"
              onClick={() => { setSinceMin(0); setTimeFrom(""); setTimeTo(""); }}
              className="h-9 rounded-lg border border-zinc-700 bg-zinc-800/80 px-3 text-xs text-zinc-400 hover:text-zinc-200"
            >
              Temizle
            </button>
          )}
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
                        <div><span className="text-zinc-500">Hacim:</span> <span className="text-zinc-300">{fmtUsd(n.volume_usd)}</span></div>
                        <div title="RVOL: son hareketin hacmi normalin kaç katı. >1.5x = haber gerçek, hacimsiz = fake">
                          <span className="text-zinc-500">RVOL:</span>{" "}
                          <span className={n.rel_volume == null ? "text-zinc-500" : n.rel_volume >= 1.5 ? "text-emerald-400" : "text-zinc-400"}>
                            {n.rel_volume != null ? `${n.rel_volume}x` : "—"}
                          </span>
                        </div>
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
                        {n.rel_volume != null && (
                          <span
                            title="RVOL: hacim normalin kaç katı. Yüksekse haber gerçek; hacimsiz hareket fake olabilir."
                            className={`rounded-md border px-2 py-0.5 text-xs font-bold ${
                              n.rel_volume >= 3
                                ? "border-emerald-500/50 bg-emerald-950/50 text-emerald-300"
                                : n.rel_volume >= 1.5
                                ? "border-emerald-600/30 bg-emerald-950/30 text-emerald-400"
                                : "border-zinc-600/40 bg-zinc-800/60 text-zinc-400"
                            }`}
                          >
                            🔊 {n.rel_volume}x{n.rel_volume >= 3 ? " patlama" : ""}
                          </span>
                        )}
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
            <div className="flex items-center gap-3">
              <span className={`text-sm font-semibold ${totalPnl >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                Toplam P&L: {totalPnl >= 0 ? "+" : ""}{totalPnl.toFixed(2)} USDT
              </span>
              <button
                type="button"
                disabled={busy === "close-all"}
                onClick={() => void closeAll()}
                title="ACİL: tüm açık pozisyonları kapat (flatten)"
                className="rounded-lg border border-red-500/50 bg-red-950/50 px-3 py-1 text-xs font-bold text-red-200 transition hover:bg-red-900/60 disabled:opacity-40"
              >
                {busy === "close-all" ? "Kapatılıyor…" : "⛔ Tümünü kapat"}
              </button>
            </div>
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

      {/* Hazırlık kokpiti — canlıya geçiş verdikti */}
      {readiness && (
        <section className="mx-auto mt-10 max-w-5xl">
          <div className={`rounded-2xl border px-4 py-3 ${
            readiness.verdict.startsWith("UMUT") ? "border-emerald-500/40 bg-emerald-950/20"
              : readiness.verdict.startsWith("HENÜZ") ? "border-red-500/40 bg-red-950/20"
              : "border-zinc-600/40 bg-zinc-900/40"}`}>
            <div className="mb-2 flex flex-wrap items-baseline gap-2">
              <span className="text-sm font-bold text-white">🚦 Canlıya hazırlık</span>
              <span className="text-sm font-semibold text-zinc-200">{readiness.verdict}</span>
            </div>
            <div className="flex flex-wrap gap-2 text-xs">
              {readiness.checks.map((c) => (
                <span key={c.check} className={`rounded-md px-2 py-1 ${
                  c.status === "pass" ? "bg-emerald-900/50 text-emerald-200"
                    : c.status === "fail" ? "bg-red-900/50 text-red-200" : "bg-zinc-800 text-zinc-400"}`}>
                  {c.status === "pass" ? "✓" : c.status === "fail" ? "✗" : "…"} {c.check}: {c.detail}
                </span>
              ))}
            </div>
            <p className="mt-2 text-[11px] text-zinc-500">
              pf {readiness.profit_factor ?? "—"} · kazanma %{readiness.win_rate ?? "—"} · max DD {readiness.max_drawdown ?? "—"} · {readiness.note}
            </p>
          </div>
        </section>
      )}

      {/* Öğrenen beyin — öneriler (otomatik uygulanmaz) */}
      <section className="mx-auto mt-10 max-w-5xl">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-semibold text-white">
            🧠 Öğrenen beyin
            <span className="ml-2 text-sm font-normal text-zinc-500">(öneri — otomatik uygulanmaz)</span>
          </h2>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => void applyTuning()}
              disabled={tuningApplying}
              title="Önerileri korkuluklarla otomatik uygula: auto_min_impact tabana kıstırılır + negatif kaynak susturulur (risk/boyut ayarlarına dokunmaz)"
              className="rounded-md border border-amber-500/40 bg-amber-950/40 px-3 py-1 text-xs font-semibold text-amber-200 hover:bg-amber-900/50 disabled:opacity-50"
            >
              {tuningApplying ? "Uygulanıyor…" : "🤖 Oto-kalibrasyonu uygula"}
            </button>
            <button
              type="button"
              onClick={() => void runPretrade()}
              disabled={pretradeRunning}
              title="Arşivlenmiş sinyalleri gerçekçi maliyetlerle backtest edip eşik önerisi çıkar — gerçek para riske atmadan, ilk işlemden akıllı"
              className="rounded-md border border-sky-500/40 bg-sky-950/40 px-3 py-1 text-xs font-semibold text-sky-200 hover:bg-sky-900/50 disabled:opacity-50"
            >
              {pretradeRunning ? "Hesaplanıyor…" : "🔮 İşlemsiz ön-bilgi (backtest)"}
            </button>
            <button
              type="button"
              onClick={() => void runBrainBacktest()}
              disabled={brainBtRunning}
              title="Arşiv sinyallerini geçmiş fiyatlarla simüle edip beynin gir/veto kararını mekanik tabanla karşılaştır — beyin edge katıyor mu (ağ-yoğun)"
              className="rounded-md border border-violet-500/40 bg-violet-950/40 px-3 py-1 text-xs font-semibold text-violet-200 hover:bg-violet-900/50 disabled:opacity-50"
            >
              {brainBtRunning ? "Replay…" : "🧠 Beyin backtest"}
            </button>
            <button
              type="button"
              onClick={() => void runBrainVeto()}
              disabled={brainVetoRunning}
              title="Beynin vetoladığı/beklettiği sinyalleri geçmiş fiyatla sına — vetolar kaybedeni mi eledi (avg net < 0 = doğru)"
              className="rounded-md border border-rose-500/40 bg-rose-950/40 px-3 py-1 text-xs font-semibold text-rose-200 hover:bg-rose-900/50 disabled:opacity-50"
            >
              {brainVetoRunning ? "Sınanıyor…" : "🧪 Veto denetimi"}
            </button>
          </div>
        </div>

        {/* Veto denetimi: vetolanan sinyaller gerçekten kaybettirir miydi */}
        {brainVeto && (
          <div className="mb-3 rounded-2xl border border-rose-500/30 bg-rose-950/20 px-4 py-3 text-sm">
            {!brainVeto.ready ? (
              <p className="text-zinc-500">{brainVeto.reason ?? "Yetersiz veri."}</p>
            ) : (
              <div className="flex flex-wrap items-center gap-4">
                <span className="text-xs font-semibold uppercase tracking-wider text-rose-300/80">🧪 Veto denetimi</span>
                <span className="text-zinc-400">{brainVeto.n} vetolanmış sinyal</span>
                <span>Simüle ort. net: <b className={`tabular-nums ${(brainVeto.avg_net_pct ?? 0) < 0 ? "text-emerald-300" : "text-red-300"}`}>{brainVeto.avg_net_pct ?? "—"}%</b></span>
                <span className="text-zinc-500">{brainVeto.win_rate ?? "—"}% kazanırdı</span>
                <span className={`rounded px-2 py-0.5 font-semibold ${(brainVeto.avg_net_pct ?? 0) < 0 ? "bg-emerald-900/50 text-emerald-200" : "bg-red-900/50 text-red-200"}`}>
                  {brainVeto.verdict}
                </span>
              </div>
            )}
          </div>
        )}

        {/* Beyin backtest: beyin vs mekanik (offline replay) */}
        {brainBt && (
          <div className="mb-3 rounded-2xl border border-violet-500/30 bg-violet-950/20 px-4 py-3 text-sm">
            {!brainBt.ready ? (
              <p className="text-zinc-500">{brainBt.reason ?? "Yetersiz arşiv."}</p>
            ) : (
              <div className="flex flex-wrap items-center gap-4">
                <span className="text-xs font-semibold uppercase tracking-wider text-violet-300/80">🧠 Beyin backtest</span>
                <span className="text-zinc-400">{brainBt.tested} sinyal</span>
                <span>Mekanik: <b className="tabular-nums">{brainBt.mechanical?.avg_net_pct ?? "—"}%</b> · {brainBt.mechanical?.win_rate ?? "—"}% ({brainBt.mechanical?.n})</span>
                <span>Beyin girer: <b className="tabular-nums text-emerald-300">{brainBt.brain_enter?.avg_net_pct ?? "—"}%</b> · {brainBt.brain_enter?.win_rate ?? "—"}% ({brainBt.brain_enter?.n})</span>
                <span className="text-zinc-500">Veto: {brainBt.brain_veto?.avg_net_pct ?? "—"}% ({brainBt.brain_veto?.n})</span>
                {brainBt.edge_pct != null && (
                  <span className={`rounded px-2 py-0.5 font-semibold ${brainBt.edge_pct >= 0 ? "bg-emerald-900/50 text-emerald-200" : "bg-red-900/50 text-red-200"}`}>
                    Edge {brainBt.edge_pct >= 0 ? "+" : ""}{brainBt.edge_pct}%
                  </span>
                )}
              </div>
            )}
          </div>
        )}

        {/* Giriş beyni kalibrasyonu: conviction dilimi → gerçek isabet */}
        {brainSc && brainSc.samples > 0 && (
          <div className="mb-3 rounded-2xl border border-violet-500/30 bg-violet-950/20 px-4 py-3">
            <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
              <span className="font-semibold uppercase tracking-wider text-violet-300/80">🧠 Giriş beyni kalibrasyonu</span>
              <span className="text-zinc-500">{brainSc.samples} işlem · {brainSc.escalated_n} eskalasyon</span>
              <span className={`rounded px-2 py-0.5 font-semibold ${
                brainSc.calibrated === true ? "bg-emerald-900/50 text-emerald-200"
                  : brainSc.calibrated === false ? "bg-red-900/50 text-red-200" : "bg-zinc-800 text-zinc-400"}`}>
                {brainSc.calibrated === true ? "✓ kalibre (konv↑ → P&L↑)"
                  : brainSc.calibrated === false ? "✗ kalibre değil" : "yetersiz veri"}
              </span>
            </div>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {brainSc.bands.map((b) => (
                <div key={b.band} className="rounded-lg border border-white/10 bg-zinc-900/50 px-2 py-1.5 text-center">
                  <div className="text-[10px] uppercase text-zinc-500">konv {b.band}</div>
                  {b.n > 0 ? (
                    <div className="text-sm font-semibold tabular-nums">
                      <span className={(b.avg_pnl ?? 0) >= 0 ? "text-emerald-300" : "text-red-300"}>
                        {(b.avg_pnl ?? 0) >= 0 ? "+" : ""}{b.avg_pnl}
                      </span>
                      <span className="text-zinc-500"> · {Math.round((b.win_rate ?? 0) * 100)}% · {b.n}</span>
                    </div>
                  ) : <div className="text-sm text-zinc-600">—</div>}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Canlı: kapanan gerçek işlemlerden */}
        {tuning && tuning.ready && tuning.suggestions.length > 0 && (
          <div className="space-y-2">
            <p className="text-xs uppercase text-zinc-500">Canlı işlemlerden ({tuning.samples} kapanmış)</p>
            {tuning.suggestions.map((s, i) => (
              <div key={`live-${i}`} className="flex items-center justify-between gap-3 rounded-2xl border border-amber-500/30 bg-amber-950/20 px-4 py-3">
                <p className="text-sm text-amber-100/90">{s.message}</p>
                {s.type === "auto_min_impact" && typeof s.suggested === "number" && (
                  <button type="button" onClick={() => void patchSettings({ auto_min_impact: s.suggested })}
                    className="shrink-0 rounded-md border border-amber-500/40 bg-amber-900/40 px-3 py-1 text-xs font-semibold text-amber-100 hover:bg-amber-800/50">
                    Uygula → {s.suggested}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}

        {/* Ön-bilgi: işlemsiz, arşiv backtest'inden */}
        {pretrade && (
          <div className="mt-3 space-y-2">
            <p className="text-xs uppercase text-sky-400/70">
              İşlemsiz ön-bilgi {pretrade.ready ? `(${pretrade.samples} arşiv sinyali backtest)` : ""}
            </p>
            {!pretrade.ready ? (
              <p className="rounded-2xl border border-white/10 bg-zinc-900/40 px-4 py-3 text-sm text-zinc-500">{pretrade.reason ?? "Yeterli arşiv yok — motoru bir süre çalıştır."}</p>
            ) : pretrade.suggestions.length === 0 ? (
              <p className="rounded-2xl border border-emerald-500/20 bg-emerald-950/10 px-4 py-3 text-sm text-emerald-300/80">Arşiv backtest'i mevcut eşiklerle uyumlu — değişiklik önerilmiyor.</p>
            ) : (
              pretrade.suggestions.map((s, i) => (
                <div key={`pre-${i}`} className="flex items-center justify-between gap-3 rounded-2xl border border-sky-500/30 bg-sky-950/20 px-4 py-3">
                  <p className="text-sm text-sky-100/90">{s.message}</p>
                  {s.type === "auto_min_impact" && typeof s.suggested === "number" && (
                    <button type="button" onClick={() => void patchSettings({ auto_min_impact: s.suggested })}
                      className="shrink-0 rounded-md border border-sky-500/40 bg-sky-900/40 px-3 py-1 text-xs font-semibold text-sky-100 hover:bg-sky-800/50">
                      Uygula → {s.suggested}
                    </button>
                  )}
                </div>
              ))
            )}
          </div>
        )}

        {(!tuning || !tuning.ready || tuning.suggestions.length === 0) && !pretrade && (
          <p className="rounded-2xl border border-white/10 bg-zinc-900/40 px-4 py-3 text-sm text-zinc-500">
            Henüz canlı öneri yok. Gerçek para riske atmadan kalibrasyon için <strong className="text-sky-300">İşlemsiz ön-bilgi</strong>'yi çalıştır — arşivdeki sinyalleri backtest edip eşik önerir.
          </p>
        )}
      </section>

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
            <label className="flex flex-col gap-1 text-xs text-zinc-400" title="Bacak başına slippage % — canlı dolum gerçekçiliği (önerilen ~0.1)">
              <span>Slippage %</span>
              <input
                type="number" value={btSlip} step={0.05} min={0}
                onChange={(e) => setBtSlip(Number(e.target.value))}
                className="h-9 w-24 rounded-md border border-zinc-700 bg-zinc-800/80 px-2 text-right text-sm tabular-nums text-zinc-200 outline-none focus:border-emerald-500/50"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-zinc-400" title="Kaç dk gecikmeli gir — tespit+teyit+emir gecikmesini modelle (haber spike chase)">
              <span>Giriş gecikme dk</span>
              <input
                type="number" value={btEntryDelay} step={1} min={0}
                onChange={(e) => setBtEntryDelay(Number(e.target.value))}
                className="h-9 w-24 rounded-md border border-zinc-700 bg-zinc-800/80 px-2 text-right text-sm tabular-nums text-zinc-200 outline-none focus:border-emerald-500/50"
              />
            </label>
            <div className="flex flex-col gap-1 text-xs text-zinc-400">
              <span>Mod</span>
              <div className="flex overflow-hidden rounded-lg border border-zinc-700">
                {([["simple", "Basit"], ["smart", "Akıllı çıkış"], ["grid", "Grid"], ["walk", "Walk-forward"]] as [BacktestMode, string][]).map(([m, label]) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => setBtMode(m)}
                    title={m === "grid" ? "Tüm SL/TP kombinasyonlarını dene, en kârlıyı bul" : m === "walk" ? "İlk %70'te optimize, son %30'da test (overfit ölçer)" : m === "smart" ? "Mevcut çıkış ayarlarını (breakeven+kısmi TP+trailing+time-stop / preset) arşivde simüle et" : "Tek SL/TP ile backtest"}
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
                  {btResult.mode === "smart" && (
                    <p className="text-xs text-zinc-500">
                      Mevcut çıkış ayarlarıyla (preset dahil) simüle edildi · kısmi: {btResult.partial ?? 0} · time-stop: {btResult.time_stop ?? 0} · breakeven-stop: {btResult.be_stop ?? 0}
                    </p>
                  )}
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
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={() => void runPreview()}
            className="text-lg font-semibold text-white transition hover:text-zinc-300"
          >
            Oto-işlem önizleme <span className="ml-1 text-sm font-normal text-zinc-500">(dry-run) {previewOn ? "▾" : "▸"}</span>
          </button>
          <button
            type="button"
            onClick={() => void runPreviewBrain()}
            disabled={previewBrainRunning}
            title="Mekanik geçen adaylarda giriş beyni verdiktini de çalıştır (gir/bekle/veto + konviksiyon) — canlıdan önce beyni gözlemle (ağ-yoğun)"
            className="rounded-md border border-violet-500/40 bg-violet-950/40 px-3 py-1 text-xs font-semibold text-violet-200 hover:bg-violet-900/50 disabled:opacity-50"
          >
            {previewBrainRunning ? "Beyin değerlendiriyor…" : "🧠 Beyin önizleme"}
          </button>
        </div>
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
                    {preview.map((p) => {
                      const verdict = p.brain
                        ? (p.brain.wait_seconds > 0 ? "bekle" : p.brain.enter ? "girer" : "veto")
                        : null;
                      return (
                      <tr key={p.id} className="border-b border-white/5 hover:bg-white/[0.03]">
                        <td className="px-4 py-3">
                          <span className={`rounded-md px-2 py-0.5 text-xs font-bold ${p.would_trade ? "bg-emerald-950/60 text-emerald-300" : "bg-zinc-800/60 text-zinc-500"}`}>
                            {p.would_trade ? `${p.side === "long" ? "LONG" : "SHORT"} açar` : "atlar"}
                          </span>
                          {p.brain && (
                            <span className={`ml-1 rounded-md px-1.5 py-0.5 text-[10px] font-bold ${
                              verdict === "girer" ? "bg-violet-900/60 text-violet-200"
                                : verdict === "bekle" ? "bg-amber-900/50 text-amber-200" : "bg-red-900/50 text-red-200"}`}
                              title={p.brain.reason}>
                              🧠 {verdict} {(p.brain.conviction).toFixed(2)}{p.brain.escalated ? " ⬆️" : ""}
                            </span>
                          )}
                        </td>
                        <td className="px-4 py-3 tabular-nums text-zinc-400">{p.impact}/10</td>
                        <td className="px-4 py-3 font-semibold text-zinc-200">{p.symbol ?? "—"}</td>
                        <td className="px-4 py-3 tabular-nums text-zinc-400">{p.usdt !== null ? `$${p.usdt}` : "—"}</td>
                        <td className="px-4 py-3 text-xs text-zinc-400">{p.brain ? p.brain.reason : p.reason}</td>
                        <td className="px-4 py-3 max-w-xs truncate text-xs text-zinc-500" title={p.title}>{p.title}</td>
                      </tr>
                      );
                    })}
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
