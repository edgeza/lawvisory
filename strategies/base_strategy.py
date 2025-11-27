# strategies/base_strategy.py
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
import math
import re
import pandas as pd

from lumibot.strategies import Strategy


@dataclass(frozen=True)
class Profile:
    # Portfolio construction
    max_positions: int
    rebalance_every_days: int

    # Risk + exposure
    risk_per_entry_cap: float          # e.g. 0.01 == ~1% loss if trailing stop hit
    total_risk_budget: float           # caps sum of per-position risk (at stop)
    target_exposure_bull: float        # % of portfolio invested when risk-on
    target_exposure_bear: float        # % invested when risk-off (avoid no-trade deadlocks)

    # Filters + scoring
    min_price: float
    vol_penalty: float                 # higher = more penalty on volatile names

    # Diversification
    max_sector_positions: int           # cap per sector

    # Stops
    atr_period: int
    atr_mult_trail: float              # trailing distance = ATR * mult

    # Safety
    max_drawdown: float                # breaker, e.g. 0.20
    cooldown_days: int


PROFILES: dict[str, Profile] = {
    "balanced": Profile(
        max_positions=10,
        rebalance_every_days=14,
        risk_per_entry_cap=0.01,
        total_risk_budget=0.07,
        target_exposure_bull=0.98,
        target_exposure_bear=0.20,
        min_price=5.0,
        vol_penalty=0.25,
        max_sector_positions=3,
        atr_period=14,
        atr_mult_trail=2.7,
        max_drawdown=0.20,
        cooldown_days=10,
    ),
    "max_return": Profile(
        max_positions=7,
        rebalance_every_days=10,
        risk_per_entry_cap=0.01,
        total_risk_budget=0.10,
        target_exposure_bull=0.99,
        target_exposure_bear=0.30,
        min_price=5.0,
        vol_penalty=0.15,
        max_sector_positions=2,
        atr_period=14,
        atr_mult_trail=3.0,
        max_drawdown=0.20,
        cooldown_days=7,
    ),
}


class LawvisoryBaseStrategy(Strategy):
    """
    Daily trend-following across S&P500-scale universe.

    Key features:
      - No-lookahead signals (uses completed bars)
      - Regime is a "risk dial" (SPY trend + optional breadth dial)
      - Inverse-vol weighting + ATR risk sizing (per-entry risk cap + total risk budget)
      - ATR trailing stop
      - Drawdown breaker + cooldown
      - Per-day historical data cache for speed
      - Sector cap to reduce concentration drawdowns
    """

    # Trend / momentum windows (daily)
    TREND_SMA_DAYS = 200
    MOM_12M = 252
    MOM_6M = 126
    MOM_3M = 63
    VOL_LOOKBACK = 63

    # Regime indicator
    REGIME_SYMBOL = "SPY"

    # Universe speed limiter
    MAX_UNIVERSE_FOR_SPEED = 500

    # Breadth dial sampling (keeps it fast)
    BREADTH_SAMPLE = 150
    BREADTH_LOW = 0.35   # below = risk-off
    BREADTH_HIGH = 0.65  # above = risk-on

    # Minimum dollars per order to avoid churn
    MIN_ORDER_DOLLARS = 150.0

    # If True: compute signals from the prior completed daily bar (more realistic)
    NO_LOOKAHEAD = True

    def initialize(
        self,
        risk_profile_name: str = "balanced",
        universe: list[str] | None = None,
        # optional overrides for parameter sweeps
        risk_per_entry_cap: float | None = None,
        total_risk_budget: float | None = None,
        atr_mult_trail: float | None = None,
        rebalance_every_days: int | None = None,
        vol_penalty: float | None = None,
        max_drawdown: float | None = None,
        target_exposure_bear: float | None = None,
        target_exposure_bull: float | None = None,
    ):
        self.sleeptime = "1D"

        name = str(risk_profile_name or "balanced").lower().strip()
        prof = PROFILES.get(name, PROFILES["balanced"])

        # apply overrides safely (keeps dataclass frozen)
        if risk_per_entry_cap is not None:
            prof = replace(prof, risk_per_entry_cap=float(risk_per_entry_cap))
        if total_risk_budget is not None:
            prof = replace(prof, total_risk_budget=float(total_risk_budget))
        if atr_mult_trail is not None:
            prof = replace(prof, atr_mult_trail=float(atr_mult_trail))
        if rebalance_every_days is not None:
            prof = replace(prof, rebalance_every_days=int(rebalance_every_days))
        if vol_penalty is not None:
            prof = replace(prof, vol_penalty=float(vol_penalty))
        if max_drawdown is not None:
            prof = replace(prof, max_drawdown=float(max_drawdown))
        if target_exposure_bear is not None:
            prof = replace(prof, target_exposure_bear=float(target_exposure_bear))
        if target_exposure_bull is not None:
            prof = replace(prof, target_exposure_bull=float(target_exposure_bull))

        self.profile = prof

        self.universe = universe or self._default_universe(self.MAX_UNIVERSE_FOR_SPEED)
        self._last_rebalance_day: date | None = None

        # trailing stop tracking
        self._highest_close: dict[str, float] = {}

        # drawdown breaker
        self._equity_peak: float | None = None
        self._cooldown_until: date | None = None

        # per-day cache
        self._cache_day: date | None = None
        self._hist_cache: dict[tuple[str, int], pd.DataFrame] = {}

        # optional sector map (from your CSVs if present)
        self._sector_by_symbol: dict[str, str] = self._load_sector_map()

        self.log_message(
            f"[INIT] profile={name} universe={len(self.universe)} "
            f"max_pos={self.profile.max_positions} dd_cap={self.profile.max_drawdown:.0%} "
            f"risk_entry_cap={self.profile.risk_per_entry_cap:.2%} total_risk={self.profile.total_risk_budget:.2%}"
        )

    # -------------------------
    # Universe helpers
    # -------------------------
    def _default_universe(self, limit: int) -> list[str]:
        """
        Uses your repo CSV names as the universe source:
        data/STOCKS/*.csv  (or fallback STOCKS/*.csv)
        Accepts tickers like BRK.B, BF.B, etc.
        """
        data_dir = Path(__file__).resolve().parents[1] / "data" / "STOCKS"
        if not data_dir.exists():
            data_dir = Path(__file__).resolve().parents[1] / "STOCKS"

        tickers: list[str] = []
        for p in data_dir.glob("*.csv"):
            t = p.stem.replace("_data", "").upper().strip()
            t = re.sub(r"[^A-Z0-9\.\-]", "", t)

            # keep common US equity ticker patterns, incl BRK.B / BF.B
            if 1 <= len(t) <= 8 and re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", t):
                tickers.append(t)

        tickers = sorted(set(tickers))
        return tickers[:limit]

    def _load_sector_map(self) -> dict[str, str]:
        """
        Optional: tries to read 'sector' from your local STOCKS CSV files.
        If not present, just returns {} (strategy still works).
        """
        data_dir = Path(__file__).resolve().parents[1] / "data" / "STOCKS"
        if not data_dir.exists():
            data_dir = Path(__file__).resolve().parents[1] / "STOCKS"

        sector_map: dict[str, str] = {}
        if not data_dir.exists():
            return sector_map

        for p in data_dir.glob("*.csv"):
            sym = p.stem.replace("_data", "").upper().strip()
            sym = re.sub(r"[^A-Z0-9\.\-]", "", sym)
            if not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", sym):
                continue
            try:
                df0 = pd.read_csv(p, nrows=1)
                cols = {c.lower(): c for c in df0.columns}
                if "sector" in cols and not df0.empty:
                    sec = str(df0.iloc[0][cols["sector"]]).strip()
                    if sec and sec.lower() != "nan":
                        sector_map[sym] = sec
            except Exception:
                continue
        return sector_map

    # -------------------------
    # Scheduling / guards
    # -------------------------
    def _should_rebalance_today(self) -> bool:
        today = self.get_datetime().date()
        if self._last_rebalance_day is None:
            return True
        return (today - self._last_rebalance_day).days >= self.profile.rebalance_every_days

    def _in_cooldown(self) -> bool:
        if self._cooldown_until is None:
            return False
        return self.get_datetime().date() <= self._cooldown_until

    # -------------------------
    # Symbol mapping (Yahoo uses BRK-B style)
    # -------------------------
    def _src_symbol(self, symbol: str) -> str:
        s = str(symbol).upper().strip()
        if self.is_backtesting:
            return s.replace(".", "-")
        return s

    # -------------------------
    # Data helpers
    # -------------------------
    def _get_daily_df(self, symbol: str, length: int) -> pd.DataFrame | None:
        """
        Cached daily bars. Drops current day bar (no-lookahead).
        """
        today = self.get_datetime().date()
        if self._cache_day != today:
            self._cache_day = today
            self._hist_cache.clear()

        sym = self._src_symbol(symbol)
        key = (sym, int(length))
        if key in self._hist_cache:
            return self._hist_cache[key]

        try:
            bars = self.get_historical_prices(sym, length=length, timestep="day")
            df = bars.df.copy()
            df.columns = [c.lower() for c in df.columns]
            df = df.dropna()

            if self.NO_LOOKAHEAD and isinstance(df.index, pd.DatetimeIndex) and len(df) >= 3:
                last_date = df.index[-1].date()
                if last_date >= today:
                    df = df.iloc[:-1]

            self._hist_cache[key] = df
            return df
        except Exception as e:
            self.log_message(f"[data] Failed {sym} length={length}: {e}")
            return None

    def _atr(self, symbol: str) -> float | None:
        length = max(self.profile.atr_period + 10, 80)
        df = self._get_daily_df(symbol, length=length)
        if df is None or any(c not in df.columns for c in ("high", "low", "close")):
            return None

        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        prev_close = close.shift(1)

        tr = pd.concat(
            [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)

        atr = tr.rolling(self.profile.atr_period).mean().iloc[-1]
        if atr is None or math.isnan(float(atr)) or float(atr) <= 0:
            return None
        return float(atr)

    def _realized_vol(self, symbol: str) -> float | None:
        length = max(self.VOL_LOOKBACK + 10, 120)
        df = self._get_daily_df(symbol, length=length)
        if df is None or "close" not in df.columns:
            return None
        close = df["close"].astype(float)
        rets = close.pct_change().dropna()
        if len(rets) < 25:
            return None
        vol = float(rets.tail(self.VOL_LOOKBACK).std() * math.sqrt(252))
        if math.isnan(vol) or vol <= 0:
            return None
        return vol

    # -------------------------
    # Regime: SPY trend + breadth dial
    # -------------------------
    def _spy_trend_bull(self) -> bool | None:
        needed = self.TREND_SMA_DAYS + 30
        df = self._get_daily_df(self.REGIME_SYMBOL, length=needed)
        if df is None or "close" not in df.columns or len(df) < self.TREND_SMA_DAYS:
            return None
        close = df["close"].astype(float)
        sma = close.rolling(self.TREND_SMA_DAYS).mean().iloc[-1]
        if sma is None or math.isnan(float(sma)):
            return None
        return float(close.iloc[-1]) > float(sma)

    def _breadth_fraction(self) -> float | None:
        """
        % of sampled universe trading above SMA200.
        Computed only on rebalance days (so it doesn't slow daily loops too much).
        """
        needed = self.TREND_SMA_DAYS + 30
        sample = self.universe[: min(len(self.universe), self.BREADTH_SAMPLE)]
        if not sample:
            return None

        above = 0
        total = 0
        for sym in sample:
            df = self._get_daily_df(sym, length=needed)
            if df is None or "close" not in df.columns or len(df) < self.TREND_SMA_DAYS:
                continue
            close = df["close"].astype(float)
            last = float(close.iloc[-1])
            sma = close.rolling(self.TREND_SMA_DAYS).mean().iloc[-1]
            if sma is None or math.isnan(float(sma)):
                continue
            total += 1
            if last > float(sma):
                above += 1

        if total < max(30, int(0.3 * len(sample))):
            return None
        return above / total

    def _risk_on_fraction(self) -> float:
        """
        Final exposure is a dial between bear and bull exposure.
        Uses SPY trend as the base, then smooths via breadth if available.
        """
        bear = float(self.profile.target_exposure_bear)
        bull = float(self.profile.target_exposure_bull)

        spy_bull = self._spy_trend_bull()
        base = bull if spy_bull is True else bear if spy_bull is False else 1.0

        # breadth smoothing only on rebalance days (keeps speed)
        if not self._should_rebalance_today():
            return base

        b = self._breadth_fraction()
        if b is None:
            return base

        # map breadth into [0,1] dial
        dial = (b - self.BREADTH_LOW) / max(1e-9, (self.BREADTH_HIGH - self.BREADTH_LOW))
        dial = max(0.0, min(1.0, dial))

        # blend bear->bull by breadth
        exp = bear + dial * (bull - bear)
        return max(0.0, min(1.0, float(exp)))

    # -------------------------
    # Ranking: trend + momentum ensemble
    # -------------------------
    def _rank_candidates(self) -> list[str]:
        needed = max(self.TREND_SMA_DAYS, self.MOM_12M) + 30
        scored: list[tuple[str, float]] = []

        passed_trend = 0
        for sym in self.universe:
            df = self._get_daily_df(sym, length=needed)
            if df is None or "close" not in df.columns or len(df) < needed - 5:
                continue

            close = df["close"].astype(float)
            last_close = float(close.iloc[-1])
            if last_close < self.profile.min_price:
                continue

            sma200 = close.rolling(self.TREND_SMA_DAYS).mean().iloc[-1]
            if sma200 is None or math.isnan(float(sma200)) or last_close <= float(sma200):
                continue

            passed_trend += 1

            def mom(lb: int) -> float | None:
                if len(close) <= lb:
                    return None
                past = float(close.iloc[-lb])
                if past <= 0:
                    return None
                return (last_close / past) - 1.0

            m12 = mom(self.MOM_12M)
            m6 = mom(self.MOM_6M)
            m3 = mom(self.MOM_3M)
            if m12 is None or m6 is None or m3 is None:
                continue

            vol = self._realized_vol(sym)
            if vol is None:
                continue

            score = (0.50 * m12) + (0.30 * m6) + (0.20 * m3) - (self.profile.vol_penalty * vol)
            scored.append((sym, float(score)))

        scored.sort(key=lambda x: x[1], reverse=True)

        if self._should_rebalance_today():
            self.log_message(f"[rank] trend_pass={passed_trend} scored={len(scored)}")

        return [s for s, _ in scored]

    def _select_with_sector_caps(self, ranked: list[str]) -> list[str]:
        """
        Pick top names but cap per sector to reduce concentration drawdowns.
        If we don't have sector data, it behaves like plain top-N.
        """
        max_pos = int(self.profile.max_positions)
        cap = int(self.profile.max_sector_positions)

        if not ranked:
            return []

        if not self._sector_by_symbol:
            return ranked[:max_pos]

        sector_count: dict[str, int] = {}
        selected: list[str] = []

        for sym in ranked:
            sec = self._sector_by_symbol.get(sym, "UNKNOWN")
            if sector_count.get(sec, 0) >= cap:
                continue
            selected.append(sym)
            sector_count[sec] = sector_count.get(sec, 0) + 1
            if len(selected) >= max_pos:
                break

        # fallback: if caps too strict, fill remaining without caps
        if len(selected) < max_pos:
            for sym in ranked:
                if sym in selected:
                    continue
                selected.append(sym)
                if len(selected) >= max_pos:
                    break

        return selected

    # -------------------------
    # Stops / exits
    # -------------------------
    def _apply_trailing_stops(self):
        for pos in self.get_positions():
            sym = getattr(pos.asset, "symbol", None) or str(pos.asset)
            qty = float(getattr(pos, "quantity", 0) or 0)
            if qty <= 0:
                continue

            px = self.get_last_price(self._src_symbol(sym))
            if px is None or px <= 0:
                continue
            px = float(px)

            prev_high = self._highest_close.get(sym, px)
            if px > prev_high:
                prev_high = px
            self._highest_close[sym] = prev_high

            atr = self._atr(sym)
            if atr is None:
                continue

            stop = prev_high - self.profile.atr_mult_trail * atr
            if px <= stop:
                self.log_message(f"[EXIT] TRAIL {sym} qty={qty:.0f} px={px:.2f} stop={stop:.2f}")
                self.submit_order(self.create_order(self._src_symbol(sym), abs(qty), "sell"))
                self._highest_close.pop(sym, None)

    def _apply_drawdown_breaker(self):
        pv = float(self.get_portfolio_value())
        if self._equity_peak is None:
            self._equity_peak = pv
            return

        if pv > self._equity_peak:
            self._equity_peak = pv
            return

        dd = (self._equity_peak - pv) / max(1e-9, self._equity_peak)
        if dd >= self.profile.max_drawdown:
            self.log_message(f"[DD] breaker dd={dd:.2%} -> liquidate + cooldown")
            for pos in self.get_positions():
                sym = getattr(pos.asset, "symbol", None) or str(pos.asset)
                qty = float(getattr(pos, "quantity", 0) or 0)
                if qty != 0:
                    self.submit_order(self.create_order(self._src_symbol(sym), abs(qty), "sell" if qty > 0 else "buy"))
                self._highest_close.pop(sym, None)

            self._cooldown_until = self.get_datetime().date() + timedelta(days=self.profile.cooldown_days)

    # -------------------------
    # Rebalance: inverse-vol weights + ATR risk sizing
    # -------------------------
    def _rebalance(self, selected: list[str], exposure: float):
        if not selected:
            self.log_message("[rebalance] none selected -> cash")
            return

        pv = float(self.get_portfolio_value())
        exposure = max(0.0, min(1.0, float(exposure)))

        selected_set = set(selected)

        # 1) sell anything not in selected
        for pos in self.get_positions():
            sym = getattr(pos.asset, "symbol", None) or str(pos.asset)
            qty = float(getattr(pos, "quantity", 0) or 0)
            if qty != 0 and sym not in selected_set:
                self.submit_order(self.create_order(self._src_symbol(sym), abs(qty), "sell" if qty > 0 else "buy"))
                self._highest_close.pop(sym, None)

        # 2) build inverse-vol weights
        vols: dict[str, float] = {}
        for sym in selected:
            v = self._realized_vol(sym)
            if v is not None and v > 0:
                vols[sym] = v

        if not vols:
            self.log_message("[rebalance] no vols -> cash")
            return

        inv = {s: 1.0 / v for s, v in vols.items()}
        inv_sum = sum(inv.values())
        weights = {s: inv[s] / inv_sum for s in inv}

        # 3) risk sizing
        total_risk_dollars = pv * self.profile.total_risk_budget
        risk_dollars_cap = pv * self.profile.risk_per_entry_cap

        desired_qty: dict[str, int] = {}
        invested = 0.0

        for sym, w in weights.items():
            px = self.get_last_price(self._src_symbol(sym))
            if px is None or px <= 0:
                continue
            px = float(px)

            atr = self._atr(sym)
            if atr is None:
                continue

            stop_dist = max(0.01, self.profile.atr_mult_trail * float(atr))

            risk_dollars = min(total_risk_dollars * float(w), risk_dollars_cap)

            shares_by_risk = int(risk_dollars // stop_dist)
            shares_by_capital = int((pv * exposure * float(w)) // px)

            shares = max(0, min(shares_by_risk, shares_by_capital))
            if shares <= 0:
                continue

            if shares * px < self.MIN_ORDER_DOLLARS:
                continue

            desired_qty[sym] = shares
            invested += shares * px

        if not desired_qty:
            self.log_message("[rebalance] no targets after sizing -> cash (try raising total_risk_budget or bear_exposure)")
            return

        # 4) submit delta orders
        for sym, target in desired_qty.items():
            pos = self.get_position(self._src_symbol(sym))
            cur = int(float(getattr(pos, "quantity", 0) or 0)) if pos else 0
            delta = int(target) - int(cur)
            if delta == 0:
                continue

            px = self.get_last_price(self._src_symbol(sym))
            if px is None or px <= 0:
                continue
            if abs(delta) * float(px) < self.MIN_ORDER_DOLLARS:
                continue

            if delta > 0:
                self.submit_order(self.create_order(self._src_symbol(sym), delta, "buy"))
                last = float(self.get_last_price(self._src_symbol(sym)) or 0) or 0.0
                if last > 0:
                    self._highest_close[sym] = max(self._highest_close.get(sym, last), last)
            else:
                self.submit_order(self.create_order(self._src_symbol(sym), abs(delta), "sell"))
                if (cur + delta) <= 0:
                    self._highest_close.pop(sym, None)

        self.log_message(
            f"[rebalance] held={len(desired_qty)} exposure≈{invested/pv:.0%} "
            f"risk_budget={self.profile.total_risk_budget:.0%} regime_exposure={exposure:.0%}"
        )

    # -------------------------
    # Main loop
    # -------------------------
    def on_trading_iteration(self):
        # exits first
        self._apply_trailing_stops()
        self._apply_drawdown_breaker()

        if self._in_cooldown():
            self.log_message(f"[cooldown] until {self._cooldown_until}")
            return

        if not self._should_rebalance_today():
            return

        exposure = self._risk_on_fraction()
        ranked = self._rank_candidates()

        selected = self._select_with_sector_caps(ranked)

        self.log_message(f"[select] n={len(selected)} exposure={exposure:.0%} first={selected[:8]}")
        self._rebalance(selected, exposure=exposure)

        self._last_rebalance_day = self.get_datetime().date()
