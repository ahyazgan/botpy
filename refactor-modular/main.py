"""
Giriş noktası.

Kullanım:
  python main.py              → FastAPI sunucu (varsayılan)
  python main.py --arb        → Arbitraj botu (CLOB gerçek mod)
  python main.py --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time

import requests

import config


def run_server(host: str, port: int) -> None:
    import uvicorn
    from app import app  # geç import — logging başlatıldıktan sonra

    uvicorn.run(app, host=host, port=port, reload=False)


def run_arb_bot() -> None:
    """Async arbitraj botunu çalıştırır."""
    import aiohttp

    from fetcher import fetch_all_markets_async
    from screener import parse_market, quick_screen, verify_opportunity
    from trader import LiveTrader

    live = LiveTrader()

    async def loop() -> None:
        connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300, keepalive_timeout=30)
        headers = {"User-Agent": "polymarket-arb/2.0", "Accept": "application/json"}

        async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
            log = logging.getLogger("arb")
            log.info(
                "Arb botu başladı | MIN_PROFIT=%.0f%% | MAX_TRADE=%.0f USDC | SCAN=%ds",
                config.MIN_PROFIT * 100, config.MAX_TRADE_USDC, config.SCAN_INTERVAL_SEC,
            )
            while True:
                t0 = time.monotonic()
                try:
                    raw_markets = await fetch_all_markets_async(session)
                    markets = [m for raw in raw_markets if (m := parse_market(raw))]
                    candidates = [m for m in markets if quick_screen(m)]

                    log.info(
                        "Tarama | %d market | %d aday | %.0fms",
                        len(markets), len(candidates), (time.monotonic() - t0) * 1000,
                    )

                    if candidates:
                        results = await asyncio.gather(
                            *[verify_opportunity(session, m) for m in candidates],
                            return_exceptions=True,
                        )
                        from screener import ArbOpportunity  # noqa: PLC0415
                        opps = sorted(
                            (r for r in results if isinstance(r, ArbOpportunity)),
                            key=lambda o: o.profit_pct,
                            reverse=True,
                        )
                        if opps:
                            log.info("%d ARB fırsatı!", len(opps))
                            for opp in opps:
                                await live.execute_arb(opp, config.MAX_TRADE_USDC)
                        else:
                            log.info("ARB yok.")

                except aiohttp.ClientError as e:
                    log.error("HTTP hatası: %s", e)
                except Exception:
                    log.exception("Beklenmeyen hata")

                elapsed = time.monotonic() - t0
                wait = max(0.0, config.SCAN_INTERVAL_SEC - elapsed)
                if wait:
                    await asyncio.sleep(wait)

    try:
        asyncio.run(loop())
    except KeyboardInterrupt:
        logging.getLogger("arb").info("Bot durduruldu.")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Polymarket Bot v2")
    parser.add_argument("--arb", action="store_true", help="Arbitraj modunu çalıştır (CLOB)")
    parser.add_argument("--host", default="127.0.0.1", help="API host")
    parser.add_argument("--port", type=int, default=8000, help="API port")
    args = parser.parse_args()

    if args.arb:
        run_arb_bot()
    else:
        run_server(args.host, args.port)


if __name__ == "__main__":
    main()
