import { useCallback, useEffect, useMemo, useState } from "react";

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
  by_symbol: Record<string, { count: number; pnl: number; wins: number }>;
  recent: Array<{ symbol: string; side: string; pnl: number | null; pnl_pct: number | null; close_reason?: string; source: string }>;
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
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const [search, setSearch] = useState("");
  const [minImpact, setMinImpact] = useState(0);
  const [onlyAlerts, setOnlyAlerts] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);

  const load = useCallback(async () => {
    setErr(null);
    try {
      const [nRes, sRes, pRes, perfRes] = await Promise.all([
        fetch(`${API_BASE}/news?limit=200`),
        fetch(`${API_BASE}/settings`),
        fetch(`${API_BASE}/positions`),
        fetch(`${API_BASE}/performance`),
      ]);
      if (!nRes.ok) throw new Error(`news ${nRes.status}`);
      const nData: NewsPayload = await nRes.json();
      setNews(nData.news);
      setMeta({ total_seen: nData.total_seen, alert_threshold: nData.alert_threshold, updated_at: nData.updated_at });
      if (nData.error) setErr(nData.error);
      if (sRes.ok) setSettings(await sRes.json());
      if (pRes.ok) {
        const pData = await pRes.json();
        setPositions(pData.positions);
        setTotalPnl(pData.total_pnl);
      }
      if (perfRes.ok) setPerf(await perfRes.json());
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

  const displayed = useMemo(() => {
    const q = search.trim().toLowerCase();
    const floor = onlyAlerts ? Math.max(minImpact, meta.alert_threshold) : minImpact;
    return news.filter((n) => {
      if (n.impact < floor) return false;
      if (q === "") return true;
      return (
        n.title.toLowerCase().includes(q) ||
        n.coins.some((c) => c.toLowerCase().includes(q)) ||
        n.source.toLowerCase().includes(q)
      );
    });
  }, [news, search, minImpact, onlyAlerts, meta.alert_threshold]);

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

        {/* İşlem ayar çubuğu */}
        {settings && (
          <div className="mt-6 flex flex-wrap items-center gap-3 rounded-2xl border border-white/10 bg-zinc-900/60 p-3">
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
            </div>
            <div className="space-y-2">
              <p className="text-xs font-semibold uppercase tracking-wider text-amber-400/80">Risk limitleri</p>
              <NumField label="Günlük zarar limiti USDT (0=kapalı)" value={settings.daily_loss_limit_usdt} onSave={(v) => patchSettings({ daily_loss_limit_usdt: v })} />
              <NumField label="Toplam maruziyet USDT" value={settings.max_total_exposure_usdt} onSave={(v) => patchSettings({ max_total_exposure_usdt: v })} />
              <NumField label="Coin başına maruziyet USDT" value={settings.max_per_coin_usdt} onSave={(v) => patchSettings({ max_per_coin_usdt: v })} />
              <NumField label="Max açık pozisyon" value={settings.max_positions} onSave={(v) => patchSettings({ max_positions: v })} />
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
            </div>
          </div>
        )}

        <div className="mt-4 flex flex-wrap gap-4 text-sm text-zinc-500">
          <span>Taranan: <strong className="text-zinc-300">{meta.total_seen}</strong></span>
          <span className="text-zinc-700">|</span>
          <span>Görüntülenen: <strong className="text-zinc-300">{displayed.length}</strong></span>
          {meta.updated_at && (
            <>
              <span className="text-zinc-700">|</span>
              <span>Son tarama: <time className="text-zinc-400">{timeAgo(meta.updated_at)}</time></span>
            </>
          )}
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
                    </div>

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
                        <span className="text-red-400">{p.sl_price ?? "—"}</span>
                        <span className="text-zinc-600"> / </span>
                        <span className="text-emerald-400">{p.tp_price ?? "—"}</span>
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
          </div>
          {Object.keys(perf.by_source).length > 0 && (
            <div className="mt-3 rounded-2xl border border-white/10 bg-zinc-900/40 p-4">
              <p className="mb-2 text-xs uppercase text-zinc-500">Kaynağa göre (hangisi kazandırıyor?)</p>
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
        </section>
      )}
    </div>
  );
}
