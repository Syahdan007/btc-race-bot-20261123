"""cbBTC long-only trend bot for agent-fun/bars-replay/v1 (make.fun btc-race).

Speaks the wire protocol through the vendored kit shim under vendor/; the
strategy in this file is written fresh for the 2026-11-23 dev window.

The economics: a full-size round trip costs ~45 bps here (4 bps fee each
way, 2 bps modelled slippage each way, and ~17 bps of pool impact each way
at the dev window's ~$600k depth), while one-minute noise is ~8 bps wide.
So the bot trades rarely, only with a confirmed trend, and lets winners run:

- Trend: fast EMA above slow EMA, slow EMA rising, gap at least
  SEP_MIN_BPS wide, and the slow EMA's slope over SLOPE_BARS at least
  SLOPE_MIN_ATR. Entries on a cross-up (the gap was at or under zero
  within DIP_WINDOW bars and is now wide), or on a genuine pullback: the
  price dipped at least DIP_MIN_ATR under the fast EMA within DIP_WINDOW
  bars and has now recovered above it, turning up. Never chasing strength,
  never entering flat chop. Every entry also needs momentum confirmation:
  the close up at least MOM_MIN_BPS over the last MOM_BARS bars, so the
  move is already real and not a wiggle. An optional second filter
  (MOM2_BARS/MOM2_MIN_BPS, off when MOM2_BARS is 0) asks the same of a
  longer lookback; it was tested and left off — it did not improve the
  out-of-sample window.
- One entry deploys SIZE_FRAC of equity. Exits: hard stop STOP_PCT under
  the fill price, trailing stop TRAIL_PCT under the best close since entry
  (armed once the peak is TRAIL_ARM_PCT above entry), the close under the
  slow EMA for EXIT_CONFIRM consecutive bars, or a time stop TIME_STOP bars
  after entry when the position is not above its fill. EXIT_MODE 1 drops
  the slow-EMA exit: the trailing stop (and the hard/time stops) are the
  only exits, for windows where the trend rides through chop.
- Brake: at BRAKE_DD drawdown from the equity peak the position is sold
  and entries pause until the fast EMA is back under the slow one (a full
  signal reset). Entries are skipped in extreme per-bar volatility and for
  COOLDOWN bars after any exit.

Everything is a deterministic function of the bars seen: no clock, no
randomness, no files, no network. Orders decided on one batch fill at the
next bar's open, so every signal uses closes up to the current bar only.
"""

import os
import sys
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- parameters (bars are one minute; the sweep rewrites these) ---
EMA_FAST = 20            # fast EMA period
EMA_SLOW = 72            # slow EMA period
ENTRY = "either"         # "cross" | "pullback" | "either"
SEP_MIN_BPS = 5.0        # trend entry needs the EMA gap at least this wide
DIP_WINDOW = 30          # bars back a pullback dip may come from
DIP_MIN_ATR = 0.5        # pullback entry: dip at least this many ATR under fast EMA
SLOPE_MIN_ATR = 0.3      # trend entry: slow EMA slope over SLOPE_BARS at least this
SLOPE_BARS = 30          # bars over which the slow EMA's slope is measured
MOM_BARS = 30            # momentum filter: lookback bars for the entry return
MOM_MIN_BPS = 80.0       # trend entry: close up at least this vs MOM_BARS ago
MOM2_BARS = 0            # second momentum filter: longer lookback (0 = off)
MOM2_MIN_BPS = 0.0       # trend entry: close up at least this vs MOM2_BARS ago
EXIT_MODE = 1            # signal exit: 0 = slow-EMA break (confirmed),
                         # 1 = trailing stop only (no slow-break exit)
EXIT_CONFIRM = 3         # consecutive closes under the slow EMA to exit
STOP_PCT = 0.012         # hard stop under the entry fill price
TRAIL_PCT = 0.02        # trailing stop under the best close since entry
TRAIL_ARM_PCT = 0.004    # trailing arms once the peak is this far above entry
MIN_HOLD = 30            # bars a signal exit is ignored after entry
TIME_STOP = 120          # bars after which a flat position is cut (0 = off)
COOLDOWN = 45            # bars after an exit before a new entry
SIZE_FRAC = 1.0          # share of equity one entry deploys
BRAKE_DD = 0.12          # equity drawdown from peak that flattens and pauses
VOL_CAP = 0.006          # skip entries when ATR/price is above this
MAX_SLIP_BPS = 200       # max_slippage_bps sent with every order
MIN_NOTIONAL = 2_000_000  # never send a buy under 2 USDC
ATR_PERIOD = 20          # Wilder ATR period (true range of the bars)
HISTORY = 300            # closes retained for pullback / slope / momentum reads


class Bot:
    """The strategy's whole state, advanced one batch at a time."""

    def __init__(self):
        self.bars_seen = 0
        self.prev_close = None
        self.prev_fast = None
        self.prev_slow = None
        self.ema_fast = None
        self.ema_slow = None
        self.atr = None
        self.closes = deque(maxlen=HISTORY)
        self.gap_atr = deque(maxlen=DIP_WINDOW)   # (close - fast EMA)/ATR
        self.gap_hist = deque(maxlen=DIP_WINDOW)  # fast-slow gap in bps
        self.slow_hist = deque(maxlen=SLOPE_BARS + 1)
        self.below_slow = 0      # consecutive closes under the slow EMA
        self.holding = False
        self.entry_price = None    # fill price (scaled int) of the position
        self.peak_close = None     # best close since the position opened
        self.entry_seq = -1
        self.last_exit_seq = -10 ** 9
        self.equity_peak = None
        self.paused = False        # brake: wait for a full signal reset

    # -- indicators ---------------------------------------------------------
    def _feed_bar(self, bar):
        close = int(bar["c"])
        high = int(bar["h"])
        low = int(bar["l"])
        if self.ema_fast is None:
            self.ema_fast = float(close)
            self.ema_slow = float(close)
            self.atr = float(high - low)
        else:
            self.prev_fast = self.ema_fast
            self.prev_slow = self.ema_slow
            self.ema_fast += (2.0 / (EMA_FAST + 1)) * (close - self.ema_fast)
            self.ema_slow += (2.0 / (EMA_SLOW + 1)) * (close - self.ema_slow)
            tr = max(high - low, abs(high - self.prev_close),
                     abs(low - self.prev_close))
            self.atr += (tr - self.atr) / ATR_PERIOD
        self.prev_close = close
        self.closes.append(close)
        if self.atr and self.atr > 0:
            self.gap_atr.append((close - self.ema_fast) / self.atr)
        self.slow_hist.append(self.ema_slow)
        self.bars_seen += 1
        return close

    # -- orders ---------------------------------------------------------------
    def _buy(self, state):
        notional = min(int(state.equity * SIZE_FRAC), state.cash,
                      state.weight_room(SYMBOL))
        if notional < MIN_NOTIONAL:
            return []
        return [{
            "side": "buy",
            "symbol": SYMBOL,
            "notional_quote_units": notional,
            "max_slippage_bps": MAX_SLIP_BPS,
        }]

    def _sell_all(self, state):
        held = state.positions.get(SYMBOL)
        if held is None or held.qty <= 0:
            return []
        return [{
            "side": "sell",
            "symbol": SYMBOL,
            "qty": held.qty,
            "max_slippage_bps": MAX_SLIP_BPS,
        }]

    # -- per-batch decision -----------------------------------------------------
    def on_batch(self, state, batch):
        bar = None
        for b in batch["bars"]:
            if b["symbol"] == SYMBOL:
                bar = b
        held = state.positions.get(SYMBOL)
        now_holding = held is not None and held.qty > 0

        # Learn the entry fill price from the fills when a position opens.
        if now_holding and not self.holding:
            self.entry_price = None
            for f in batch["fills"]:
                if f["symbol"] == SYMBOL and f["side"] == "buy" \
                        and f["status"] == "filled":
                    self.entry_price = int(f["price"])
            if self.entry_price is None and bar is not None:
                self.entry_price = int(bar["o"])
            self.entry_seq = state.seq
            self.peak_close = self.entry_price
        if self.holding and not now_holding:
            self.last_exit_seq = state.seq
            self.entry_price = None
            self.peak_close = None
        self.holding = now_holding

        if bar is None:
            return []
        close = self._feed_bar(bar)

        if self.equity_peak is None or state.equity > self.equity_peak:
            self.equity_peak = state.equity
        if state.warming_up:
            return []
        if self.bars_seen <= EMA_SLOW + 10:
            return []
        atr = self.atr
        if not atr or atr <= 0 or close <= 0:
            return []

        gap_bps = (self.ema_fast - self.ema_slow) / close * 10000.0
        self.gap_hist.append(gap_bps)
        slow_rising = self.ema_slow > self.prev_slow
        slope_atr = (self.slow_hist[-1] - self.slow_hist[0]) / atr \
            if len(self.slow_hist) > SLOPE_BARS else 0.0
        trend = self.ema_fast > self.ema_slow and slow_rising \
            and gap_bps >= SEP_MIN_BPS and slope_atr >= SLOPE_MIN_ATR
        if close < self.ema_slow:
            self.below_slow += 1
        else:
            self.below_slow = 0

        # Brake reset: a fresh down-cross clears the pause.
        if self.paused and gap_bps < 0.0:
            self.paused = False

        # Equity brake: flatten, then wait for a full signal reset.
        if self.equity_peak and state.equity <= self.equity_peak * (1.0 - BRAKE_DD):
            self.paused = True
            if self.holding:
                return self._sell_all(state)
            return []

        if self.holding:
            if close > self.peak_close:
                self.peak_close = close
            age = state.seq - self.entry_seq
            # Stops first: they apply from the first bar.
            if self.entry_price is not None \
                    and close <= self.entry_price * (1.0 - STOP_PCT):
                return self._sell_all(state)
            if self.entry_price is not None and self.peak_close is not None \
                    and self.peak_close >= self.entry_price * (1.0 + TRAIL_ARM_PCT) \
                    and close <= self.peak_close * (1.0 - TRAIL_PCT):
                return self._sell_all(state)
            if age < MIN_HOLD:
                return []
            # Time stop: a position that has gone nowhere is a failed entry.
            if TIME_STOP and age >= TIME_STOP and self.entry_price is not None \
                    and close <= self.entry_price:
                return self._sell_all(state)
            # Signal exit: the close breaks the slow EMA, confirmed over
            # EXIT_CONFIRM consecutive closes so chop cannot shake it out.
            # EXIT_MODE 1 drops the signal exit entirely: the trailing stop
            # (and the hard/time stops) are the only exits.
            if EXIT_MODE == 0 and self.below_slow >= EXIT_CONFIRM:
                return self._sell_all(state)
            return []

        # Flat: entries.
        if self.paused:
            return []
        if state.seq - self.last_exit_seq < COOLDOWN:
            return []
        if atr / close > VOL_CAP:
            return []
        if not trend:
            return []

        # A cross-up counts if the gap was at or under zero within the last
        # DIP_WINDOW bars and is now wide: this also catches a cross that
        # happened while the EMAs were still warming up.
        crossed_up = len(self.gap_hist) >= 2 and min(self.gap_hist) <= 0.0 \
            and gap_bps >= SEP_MIN_BPS
        # A genuine pullback: the price dipped at least DIP_MIN_ATR under the
        # fast EMA within the window and has now recovered above it, turning up.
        prev = self.closes[-2] if len(self.closes) >= 2 else close
        dipped = len(self.gap_atr) >= 2 and min(self.gap_atr) <= -DIP_MIN_ATR
        pulled_back = dipped and (close - self.ema_fast) / atr >= 0.0 \
            and close > prev
        # Momentum confirmation: the move must already be real, not a wiggle.
        # The optional second filter asks the same of a longer lookback, so a
        # burst inside a flat range does not qualify.
        mom_ok = True
        if MOM_BARS and len(self.closes) > MOM_BARS:
            past = list(self.closes)[-1 - MOM_BARS]
            mom_ok = past > 0 and (close / past - 1.0) * 10000.0 >= MOM_MIN_BPS
        if mom_ok and MOM2_BARS and len(self.closes) > MOM2_BARS:
            past2 = list(self.closes)[-1 - MOM2_BARS]
            mom_ok = past2 > 0 and (close / past2 - 1.0) * 10000.0 >= MOM2_MIN_BPS
        if ENTRY == "cross":
            fire = crossed_up
        elif ENTRY == "pullback":
            fire = pulled_back
        else:  # "either"
            fire = crossed_up or pulled_back
        if fire and mom_ok:
            return self._buy(state)
        return []


_BOT = Bot()


def on_batch(state, batch):
    return _BOT.on_batch(state, batch)


if __name__ == "__main__":
    makefun_bars.run(on_batch)
