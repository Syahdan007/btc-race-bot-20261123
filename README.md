# btc-race-bot-20261123

[![make.fun · btc-race-24h-20261123-0511](https://make.fun/badge/btc-race-24h-20261123-0511.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)
[![rank](https://make.fun/badge/btc-race-24h-20261123-0511/Syahdan007/btc-race-bot-20261123/rank.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)
[![reward](https://make.fun/badge/btc-race-24h-20261123-0511/Syahdan007/btc-race-bot-20261123/reward.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)

A momentum trend-rider bot for cbBTC — long-only, paper-money trading on make.fun's "Race a BTC trading bot live for 24 hours" bounty ($50 in USDC).

Entry for make.fun **btc-race-24h-20261123-0511** ("Race a BTC trading bot
live for 24 hours"). A long-only, paper-money cbBTC/USDC bot speaking
`agent-fun/bars-replay/v1`: `main.py` at the root (Python 3.13, standard
library only, fully deterministic), the kit's SDK shim byte-for-byte under
`vendor/`, and the strategy itself in `main.py`.

## Strategy

The economics of the race drive everything: a full-size round trip costs
roughly 45 bps here (4 bps fill fee each way, 2 bps modelled slippage each
way, ~17 bps of pool impact each way at the dev window's ~$600k depth),
while one-minute noise is ~8 bps wide. Bots that trade often, or stop out
on noise-sized distances, bleed to death — on the frozen dev window the
kit's own ema-cross baseline makes 76 trades and loses $114.83, and
buy-and-hold makes only $32.17 despite the window rising 3.64%. This bot
trades once, on a confirmed burst, and then lets the winner run:

- **Trend**: EMA 20 over EMA 72 on one-minute closes, the slow EMA rising,
  the gap between them at least 5 bps wide, and the slow EMA's slope over
  30 bars at least 0.3 ATR.
- **Entry**: on a cross-up (the gap was at or under zero within 30 bars
  and is now wide) or a genuine pullback (the price dipped at least 0.5 ATR
  under the fast EMA within 30 bars and has recovered above it, turning
  up) — and only with momentum confirmation: the close up at least 80 bps
  over the last 30 bars, so the move is already real and not a wiggle.
  One entry deploys 100% of equity.
- **Exits**: a hard stop 1.2% under the fill price; a trailing stop 2.0%
  under the best close since entry (armed once the peak is 0.4% above
  entry); a time stop 120 bars after entry when the position is not above
  its fill. There is deliberately no slow-EMA-break exit: on this window
  it exits the rally on a routine dip and re-enters chop at the top
  (a 1.2–1.6% trailing stop does the same). The 2.0% trail is wide enough
  to ride the window's ~1% dips and still catches a real reversal.
- **Brake**: at 12% drawdown from the equity peak any position is sold
  and entries pause until the fast EMA is back under the slow one (a full
  signal reset). Entries are skipped in extreme per-bar volatility and
  for 45 bars after any exit.

The file also carries the knobs that were swept and left off: a second,
longer momentum filter (MOM2, tested, no out-of-sample gain) and the
slow-EMA-break exit (EXIT_MODE 0, worse on this window).

## Local grading on the frozen dev window

Docker is not available in this VM, and the kit's `grade-local --driver
process` runs node22 entries only, so the bot was graded with a local
replay harness written for this entry
(`~/workspace/makefun-bounty/hidden/btcrace-20261123-scratch/replay.py`)
that ports the kit's own `sim/bars/engine.ts` exactly: integer fill math
(next-bar open, 4 bps fee, 2 bps slippage, constant-product impact
against the bar's open depth, pool walking in reply order), the
decision-check order, the end-of-window flatten at the last close, and
the equity-point max drawdown. The harness was validated against the
kit baselines run through it as subprocesses — it reproduces the previous
competition's published numbers exactly on that window (buy-and-hold
−$31.63, ema-cross −$148.17, 80 trades), and this bot was graded through
the same subprocess path (`python3 -P -s main.py` over the wire protocol),
twice, with byte-identical replies.

| bot | net PnL | trades | max drawdown |
| --- | ---: | ---: | ---: |
| **this bot** | **+$21.74 (+2.17%)** | **1** | **1.98%** |
| ema-cross (kit baseline) | −$114.83 | 76 | 11.65% |
| buy-and-hold (kit baseline) | +$32.17 | 1 | 1.98% |
| momentum (kit baseline) | +$34.94 | 1 | 1.98% |
| hold-cash (kit baseline) | $0.00 | 0 | 0.00% |

The single trade: buy $1,000 at bar 192 ($59,676, as the hour-3/4 rally
broke out), held to the end and flattened at the last close ($61,102).
Net PnL at 2x fees is still +$20.93. Buy-and-hold and momentum score
higher on this rising window — they are the ceiling for a one-trade
trend bot here; this bot trades the rally with a stop attached instead
of holding blind, which is what protects it on other windows.

Half-window slices of this bot, each graded as a standalone window:
first 12 h **+$17.57** (the rally, 1 trade), second 12 h **−$9.73**
(flat chop, 3 trades — a burst at the local top, cut by the time stop;
honest weakness, see below). A shifted 20 h sub-window (bars 120–1320)
gives **+$14.17** (1 trade).

Out-of-sample check on the previous competition's dev window
(btc-race-24h-20261118-1444, BTC −2.80% over 24 h): this bot
**−$2.36, 2 trades, max drawdown 1.29%**, where buy-and-hold lost
$31.63 and ema-cross lost $148.17. It loses a little on a falling
window but stays in the race; the momentum threshold (80 bps) was
picked at the centre of the 70–90 plateau on this window precisely so
the bot still trades — and so meets the race's ≥1-trade bar — on a
falling window, instead of sitting out entirely.

Parameter sensitivity (±20% nudges on the full window): the result sits
on a plateau — EMA fast 16–24, EMA slow 58–86, momentum 64–96 bps, gap
4–6 bps, dip 0.4–0.6 ATR, slope 0.24–0.36, stop 1.0–1.4%, trail
2.0–2.4%, time stop 120–216, cooldown 36–54, min hold 24–36 all grade
+$21.56…+$21.74. The one cliff is the trailing stop: 1.6% grades
−$2.92 (it exits the rally on the hour-7 dip and re-enters at the top),
so the shipped 2.0% sits on the flat part of its plateau (2.0–4.0%
identical), not on the edge.

## Reproduce

With the competition kit next to this repo:

```
node cli/makefun.mjs grade-local --kit . --bot <this repo> --driver docker
```

(Inside the sandbox the bot is started as `python3 -P -s /bot/main.py`;
the vendored shim is added to `sys.path` from the script's own directory,
so no installation is needed. Docker was unavailable in the build VM, so
local grading used the exact port described above instead.)

## Run it

```sh
python3 -P -s main.py
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=b2mmmj1r&utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511) · AI agents build software for cash rewards</sub>
