import { useCallback, useEffect, useMemo, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";
const POLL_MS = 30_000;

type MarketRow = {
  id: string;
  question: string;
  bid: number | null;
  ask: number | null;
  spread: number | null;
  volume24h: number | null;
};

type MarketsPayload = {
  markets: MarketRow[];
  paper_mode: boolean;
  total_active: number;
  filtered_count: number;
  min_volume_24hr: number;
  updated_at: string | null;
  error: string | null;
};

type BtcPayload = {
  price: number | null;
  symbol: string;
  updated_at: string | null;
  error: string | null;
};

type SortKey = "question" | "bid" | "ask" | "spread" | "volume24h";
type SortDir = "asc" | "desc";

function fmtNum(n: number | null | undefined, decimals = 4): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toFixed(decimals);
}

function fmtVol(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function fmtBtc(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function SortIcon({ active, dir }: { active: boolean; dir: SortDir }) {
  return (
    <span className={`ml-1 inline-block text-xs ${active ? "text-emerald-400" : "text-zinc-600"}`}>
      {active ? (dir === "asc" ? "▲" : "▼") : "⇅"}
    </span>
  );
}

export default function App() {
  const [markets, setMarkets] = useState<MarketRow[]>([]);
  const [btc, setBtc] = useState<number | null>(null);
  const [paperMode, setPaperMode] = useState(true);
  const [meta, setMeta] = useState({
    total_active: 0,
    filtered_count: 0,
    min_volume_24hr: 10_000,
    updated_at: null as string | null,
  });
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [settingsLoading, setSettingsLoading] = useState(false);

  const [search, setSearch] = useState("");
  const [minVol, setMinVol] = useState<number>(0);
  const [minVolInput, setMinVolInput] = useState("0");
  const [sortKey, setSortKey] = useState<SortKey>("volume24h");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  const load = useCallback(async () => {
    setErr(null);
    try {
      const [mRes, bRes] = await Promise.all([
        fetch(`${API_BASE}/markets`),
        fetch(`${API_BASE}/btc`),
      ]);
      if (!mRes.ok) throw new Error(`markets ${mRes.status}`);
      if (!bRes.ok) throw new Error(`btc ${bRes.status}`);
      const mData: MarketsPayload = await mRes.json();
      const bData: BtcPayload = await bRes.json();
      setMarkets(mData.markets);
      setPaperMode(mData.paper_mode);
      setMeta({
        total_active: mData.total_active,
        filtered_count: mData.filtered_count,
        min_volume_24hr: mData.min_volume_24hr,
        updated_at: mData.updated_at,
      });
      setBtc(bData.price);
      const apiErr = mData.error ?? bData.error;
      if (apiErr) setErr(apiErr);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Yukleme hatasi");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(id);
  }, [load]);

  const togglePaper = async () => {
    setSettingsLoading(true);
    try {
      const next = !paperMode;
      const r = await fetch(`${API_BASE}/settings`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paper_mode: next }),
      });
      if (!r.ok) throw new Error(String(r.status));
      const { paper_mode } = await r.json();
      setPaperMode(paper_mode);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Ayar degismedi");
    } finally {
      setSettingsLoading(false);
    }
  };

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "question" ? "asc" : "desc");
    }
  };

  const handleMinVolBlur = () => {
    const parsed = parseFloat(minVolInput.replace(/,/g, ""));
    if (Number.isFinite(parsed) && parsed >= 0) {
      setMinVol(parsed);
    } else {
      setMinVolInput(String(minVol));
    }
  };

  const displayedMarkets = useMemo(() => {
    const q = search.trim().toLowerCase();
    let rows = markets.filter((m) => {
      const volOk = (m.volume24h ?? 0) >= minVol;
      const searchOk = q === "" || m.question.toLowerCase().includes(q);
      return volOk && searchOk;
    });

    rows = [...rows].sort((a, b) => {
      let va: number | string | null;
      let vb: number | string | null;
      if (sortKey === "question") {
        va = a.question;
        vb = b.question;
      } else {
        va = a[sortKey];
        vb = b[sortKey];
      }
      if (va === null && vb === null) return 0;
      if (va === null) return 1;
      if (vb === null) return -1;
      if (typeof va === "string" && typeof vb === "string") {
        return sortDir === "asc" ? va.localeCompare(vb) : vb.localeCompare(va);
      }
      return sortDir === "asc"
        ? (va as number) - (vb as number)
        : (vb as number) - (va as number);
    });

    return rows;
  }, [markets, search, minVol, sortKey, sortDir]);

  const thClass = "px-4 py-3 font-semibold text-zinc-400 cursor-pointer select-none hover:text-zinc-200 transition whitespace-nowrap";

  return (
    <div className="min-h-screen px-4 pb-16 pt-10 sm:px-8">
      <header className="mx-auto max-w-7xl">
        <div className="flex flex-col gap-6 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <p className="font-display text-xs font-semibold uppercase tracking-[0.2em] text-emerald-400/90">
              Polymarket
            </p>
            <h1 className="font-display mt-1 text-3xl font-bold tracking-tight text-white sm:text-4xl">
              Market tarayici
            </h1>
            <p className="mt-2 max-w-xl text-sm text-zinc-400">
              Gamma aktif marketler, hacim filtresi ve Binance BTC spot. Veri her{" "}
              <span className="text-zinc-200">30 sn</span> yenilenir.
            </p>
          </div>

          <div className="flex flex-col gap-3 sm:items-end">
            <div className="rounded-2xl border border-white/10 bg-zinc-900/80 px-5 py-4 shadow-glow backdrop-blur">
              <p className="text-xs font-medium uppercase tracking-wider text-zinc-500">
                Binance BTC / USDT
              </p>
              <p className="font-display mt-1 text-3xl font-semibold tabular-nums text-white">
                {loading && btc === null ? (
                  <span className="animate-pulse text-zinc-500">—</span>
                ) : (
                  <>${fmtBtc(btc)}</>
                )}
              </p>
            </div>

            <div className="flex flex-wrap items-center gap-3">
              <button
                type="button"
                onClick={() => void load()}
                className="rounded-xl border border-zinc-700 bg-zinc-800/80 px-4 py-2 text-sm font-medium text-zinc-200 transition hover:border-emerald-500/40 hover:bg-zinc-800"
              >
                Simdi yenile
              </button>
              <button
                type="button"
                disabled={settingsLoading}
                onClick={() => void togglePaper()}
                className={`relative inline-flex h-10 items-center gap-2 rounded-xl border px-4 text-sm font-semibold transition ${
                  paperMode
                    ? "border-emerald-500/40 bg-emerald-950/50 text-emerald-200"
                    : "border-amber-500/40 bg-amber-950/40 text-amber-200"
                }`}
              >
                <span
                  className={`h-2 w-2 rounded-full ${
                    paperMode ? "bg-emerald-400 shadow-[0_0_12px]" : "bg-amber-400"
                  }`}
                />
                PAPER_MODE: {paperMode ? "On" : "Off"}
              </button>
            </div>
          </div>
        </div>

        <div className="mt-8 flex flex-wrap gap-4 text-sm text-zinc-500">
          <span>
            Aktif market:{" "}
            <strong className="text-zinc-300">{meta.total_active}</strong>
          </span>
          <span className="text-zinc-700">|</span>
          <span>
            API filtresi (vol24h &gt; {meta.min_volume_24hr.toLocaleString()}):{" "}
            <strong className="text-zinc-300">{meta.filtered_count}</strong>
          </span>
          <span className="text-zinc-700">|</span>
          <span>
            Goruntulenen:{" "}
            <strong className="text-zinc-300">{displayedMarkets.length}</strong>
          </span>
          {meta.updated_at && (
            <>
              <span className="text-zinc-700">|</span>
              <span>
                Son guncelleme:{" "}
                <time className="text-zinc-400" dateTime={meta.updated_at}>
                  {new Date(meta.updated_at).toLocaleString()}
                </time>
              </span>
            </>
          )}
        </div>

        {err && (
          <div
            className="mt-4 rounded-xl border border-red-500/30 bg-red-950/40 px-4 py-3 text-sm text-red-200"
            role="alert"
          >
            {err}
          </div>
        )}

        {/* Arama ve filtre araçları */}
        <div className="mt-6 flex flex-wrap gap-3">
          <input
            type="text"
            placeholder="Market ara..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="h-10 min-w-[220px] flex-1 rounded-xl border border-zinc-700 bg-zinc-800/80 px-4 text-sm text-zinc-200 placeholder-zinc-600 outline-none focus:border-emerald-500/50 focus:ring-1 focus:ring-emerald-500/30"
          />
          <div className="flex items-center gap-2">
            <label className="text-sm text-zinc-500 whitespace-nowrap">Min. Vol 24h</label>
            <input
              type="text"
              inputMode="numeric"
              value={minVolInput}
              onChange={(e) => setMinVolInput(e.target.value)}
              onBlur={handleMinVolBlur}
              onKeyDown={(e) => e.key === "Enter" && handleMinVolBlur()}
              className="h-10 w-32 rounded-xl border border-zinc-700 bg-zinc-800/80 px-3 text-sm tabular-nums text-zinc-200 outline-none focus:border-emerald-500/50 focus:ring-1 focus:ring-emerald-500/30"
            />
          </div>
        </div>
      </header>

      <main className="mx-auto mt-6 max-w-7xl">
        <div className="overflow-hidden rounded-2xl border border-white/10 bg-zinc-900/40 shadow-xl backdrop-blur">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[900px] text-left text-sm">
              <thead>
                <tr className="border-b border-white/10 bg-zinc-900/90">
                  <th className={thClass} onClick={() => handleSort("question")}>
                    Question <SortIcon active={sortKey === "question"} dir={sortDir} />
                  </th>
                  <th className={thClass} onClick={() => handleSort("bid")}>
                    Bid <SortIcon active={sortKey === "bid"} dir={sortDir} />
                  </th>
                  <th className={thClass} onClick={() => handleSort("ask")}>
                    Ask <SortIcon active={sortKey === "ask"} dir={sortDir} />
                  </th>
                  <th className={thClass} onClick={() => handleSort("spread")}>
                    Spread <SortIcon active={sortKey === "spread"} dir={sortDir} />
                  </th>
                  <th className={`${thClass} text-right`} onClick={() => handleSort("volume24h")}>
                    Vol 24h <SortIcon active={sortKey === "volume24h"} dir={sortDir} />
                  </th>
                </tr>
              </thead>
              <tbody>
                {loading && markets.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="px-4 py-12 text-center text-zinc-500">
                      Yukleniyor…
                    </td>
                  </tr>
                ) : displayedMarkets.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="px-4 py-12 text-center text-zinc-500">
                      Veri yok veya filtre sonucu bos.
                    </td>
                  </tr>
                ) : (
                  displayedMarkets.map((row, i) => (
                    <tr
                      key={row.id || `${i}-${row.question.slice(0, 24)}`}
                      className="border-b border-white/5 transition hover:bg-white/[0.03]"
                    >
                      <td className="max-w-md px-4 py-3 text-zinc-200">
                        {row.question}
                      </td>
                      <td className="px-4 py-3 tabular-nums text-zinc-300">
                        {fmtNum(row.bid)}
                      </td>
                      <td className="px-4 py-3 tabular-nums text-zinc-300">
                        {fmtNum(row.ask)}
                      </td>
                      <td className="px-4 py-3 tabular-nums text-emerald-400/90">
                        {fmtNum(row.spread)}
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-zinc-400">
                        {fmtVol(row.volume24h)}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </main>
    </div>
  );
}
