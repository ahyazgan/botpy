"""
Risk yönetimi — profesyonel trading için kalkan katmanı.

Saf ve test edilebilir (zaman/ağ bağımlılığı dışarıdan enjekte edilir).

Kontroller:
  - Kill-switch / drawdown halt: tepe equity'den belirli % düşüşte dur.
  - Günlük zarar limiti: gün içi realize zarar eşiği aşılınca dur.
  - Pozisyon başına ve toplam exposure tavanları.
  - Maksimum eşzamanlı açık pozisyon sayısı.
  - Sabit-oransal pozisyon boyutlandırma.

Bot durdurulduğunda (halted) yeni pozisyon açılmaz; mevcut pozisyonlar
yine de kapatılabilir.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RiskLimits:
    max_open_positions: int = 20
    max_position_usdc: float = 50.0
    max_total_exposure_usdc: float = 500.0
    max_daily_loss_usdc: float = 100.0   # gün içi realize zarar eşiği
    max_drawdown_pct: float = 25.0       # tepe equity'den izinli düşüş %


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""


@dataclass
class RiskManager:
    limits: RiskLimits = field(default_factory=RiskLimits)
    starting_equity: float = 0.0
    realized_pnl: float = 0.0
    peak_equity: float = 0.0
    day: str = ""
    day_realized: float = 0.0
    halted: bool = False
    halt_reason: str = ""

    def __post_init__(self) -> None:
        # Tepe equity en az başlangıç sermayesi kadar olmalı
        self.peak_equity = max(self.peak_equity, self.equity)

    # ── Türev metrikler ──
    @property
    def equity(self) -> float:
        return self.starting_equity + self.realized_pnl

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity * 100.0)

    # ── Gün döndürme ──
    def _roll_day(self, today: str) -> None:
        if today and today != self.day:
            self.day = today
            self.day_realized = 0.0

    # ── Kapanışta PnL kaydı + kill-switch ──
    def on_close(self, pnl: float, today: str) -> None:
        self._roll_day(today)
        self.realized_pnl += pnl
        self.day_realized += pnl
        self.peak_equity = max(self.peak_equity, self.equity)

        if self.day_realized <= -self.limits.max_daily_loss_usdc:
            self._halt(f"günlük zarar limiti aşıldı ({self.limits.max_daily_loss_usdc:.0f} USDC)")
        elif self.drawdown_pct >= self.limits.max_drawdown_pct:
            self._halt(f"max drawdown aşıldı (%{self.limits.max_drawdown_pct:.0f})")

    def _halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason

    def reset_halt(self) -> None:
        self.halted = False
        self.halt_reason = ""

    # ── Açılış kontrolü ──
    def check_open(
        self,
        amount: float,
        open_positions: int,
        total_exposure: float,
        today: str | None = None,
    ) -> RiskDecision:
        if today is not None:
            self._roll_day(today)
        if self.halted:
            return RiskDecision(False, f"HALT: {self.halt_reason}")
        if open_positions >= self.limits.max_open_positions:
            return RiskDecision(False, "maksimum açık pozisyon sayısı")
        if amount > self.limits.max_position_usdc:
            return RiskDecision(False, "pozisyon boyutu limiti")
        if (total_exposure + amount) > self.limits.max_total_exposure_usdc:
            return RiskDecision(False, "toplam exposure limiti")
        return RiskDecision(True)

    # ── Sabit-oransal pozisyon boyutu ──
    def position_size(self, fraction: float = 0.02) -> float:
        """Equity'nin belirli oranı, pozisyon tavanıyla sınırlı."""
        raw = max(0.0, self.equity * fraction)
        return min(raw, self.limits.max_position_usdc)

    # ── Durum özeti (API için) ──
    def snapshot(self) -> dict[str, object]:
        return {
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "equity": self.equity,
            "starting_equity": self.starting_equity,
            "realized_pnl": self.realized_pnl,
            "day_realized": self.day_realized,
            "peak_equity": self.peak_equity,
            "drawdown_pct": self.drawdown_pct,
            "limits": {
                "max_open_positions": self.limits.max_open_positions,
                "max_position_usdc": self.limits.max_position_usdc,
                "max_total_exposure_usdc": self.limits.max_total_exposure_usdc,
                "max_daily_loss_usdc": self.limits.max_daily_loss_usdc,
                "max_drawdown_pct": self.limits.max_drawdown_pct,
            },
        }
