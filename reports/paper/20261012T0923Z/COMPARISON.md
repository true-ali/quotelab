# QuoteLab paper session vs replay

One paper session on Binance's public `BTCUSDT` USD-M feed. Live window 2026-10-12 09:38 to 2026-10-12 10:38 UTC (exchange time), after a 15-minute warm-up. The session stopped because: completed.

The replay runs the recorded live window through `run_backtest` with the session's own config and calibration, so the two differ only in how the feed delivered the messages. This is a simulation: paper fills come from the same queue-aware fill model as every backtest, not from an exchange.

## Contents

- [Paper vs replay](#compare)
- [The live feed](#feed)
- [Setup](#setup)

<a id="compare"></a>

## Paper vs replay

The paper session's PnL was -$21.31 and the replay's -$20.92, a difference of -$0.39. The first 14 fills match exactly; fill 15 differs: paper buy 0.003 at 83,397.40, 60.510 s into the window, replay buy 0.003 at 83,403.80, 60.009 s into the window.

They differ because the paper engine saw the feed as it arrived: 102,050 messages (9.58%) came stamped earlier than the engine's clock and were pushed at the clock, and 45,615 trades arrived after a book update with the same exchange time, which the replay processes in the opposite order. Either can shift one quote by a requote cycle, and once one fill differs the inventory, the skew and every later quote differ too, so the size of the gap says more about path dependence than about the feed.

![Paper vs replay](figures/paper_vs_replay.png)

**Paper session and replay of the same window**

|  | Paper | Replay | Paper − replay |
|---|---|---|---|
| PnL (USD) | -21.31 | -20.92 | -0.39 |
| of which spread (USD) | 0.86 | 1.17 | -0.30 |
| of which inventory (USD) | -5.02 | -5.24 | 0.22 |
| of which fees (USD) | -17.15 | -16.85 | -0.30 |
| of which hedging (USD) | 0.00 | 0.00 | 0.00 |
| of which funding (USD) | -0.00 | -0.00 | 0.00 |
| Quote fills | 360 | 352 | 8 |
| Hedge fills | 0 | 0 | 0 |
| Orders sent | 14,275 | 13,984 | 291 |
| Quote fills per order sent | 0.025 | 0.025 | 0.000 |
| Maker volume (USD) | 85,760 | 84,257 | 1,502 |
| Max drawdown (USD) | 21.31 | 20.92 | 0.39 |
| Mean \|position\| | 0.0262 | 0.0264 | -0.0001 |
| Final position | 0.0280 | 0.0280 | 0.0000 |
| Adverse move at 5 s (bps) | -0.260 | -0.295 | 0.035 |
| Markout at 5 s (bps) | -0.164 | -0.163 | -0.002 |
| Risk events | 9 | 9 | 0 |
| Uptime, both sides quoted | 24.7% | 22.9% | 1.8 pp |
| First kill switch | none | none |  |

<a id="feed"></a>

## The live feed

In the live window the feed delivered 990,082 book updates and 74,648 trades, with 0 reconnects; the longest wait between two arrivals was 1.84 s. 404 trades had a type other than `MARKET`; they are treated like any other trade, as the downloaded trade files do not mark the type.

Messages arrived a median -284.6 ms after their exchange time (90th percentile -104.8 ms, 99th 478.6 ms, maximum 1,969.7 ms). The engine assumes a constant 5 ms in both runs, so this delay is not a cause of the difference above; it measures how far the backtest's assumption is from this machine's connection. The level includes the unmeasured offset between the local clock and Binance's, and the smallest value (-403.1 ms) is below zero, which only a clock offset can explain.

**Feed during the live window**

| Measure | Value |
|---|---|
| Book updates | 990,082 |
| Trades | 74,648 |
| Messages outside the live window (warm-up) | 338,416 |
| Reconnects | 0 |
| Clamped messages | 102,050 (9.58%) |
| Largest clamp (ms) | 620 |
| Trades after a same-time book update | 45,615 |
| Longest wait between arrivals (s) | 1.836 |
| Delay p50 / p90 / p99 (ms) | -284.6 / -104.8 / 478.6 |
| Delay min / max (ms) | -403.1 / 1,969.7 |
| Assumed data latency (ms) | 5 |

<a id="setup"></a>

## Setup

The strategy `glft` was calibrated on the 15 minutes of feed recorded just before the live window, then traded it with the same code a backtest job uses: `queue` fills (alpha 0.5), data latency 5 ms, order and cancel latency 10 and 10 ms, and the E6 risk controls on (kill switches, circuit breakers, size ramp). Fees: maker 2.0 bps and taker 5.0 bps, config values that were not checked against Binance's current fee schedule.

Nothing was sent to Binance. The feed is the public read-only market stream, and every order existed only inside the simulated exchange.

**Calibration on the warm-up**

| Parameter | Value | Std. error |
|---|---|---|
| sigma (USD/√s) | 3.4083 |  |
| A (fills per second at the mid) | 0.571 | 0.124 |
| k (per USD) | 0.808 | 0.189 |
| Intensity fit R² | 0.505 |  |
| Levels in the fit | 20 |  |
| OFI beta (USD) | 2.7337 | 0.8262 |
