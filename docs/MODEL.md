# QuoteLab model

This note gives the formulas the code uses, how each one is calibrated, and where it lives in the code. The models are textbook market-making and microstructure models, run on public Binance market data. The signals (microprice, order-flow imbalance) are short-horizon public indicators. They are not alpha claims. Calibration is walk-forward: parameters are fitted on one day and applied to the next day (`walk_forward_pairs` in `quotelab/experiments/jobs.py`). No test day informs its own parameters.

Config defaults are quoted as default `name: value` in config/default.yaml. Code paths are relative to the repository root, the directory that holds `config/` and `quotelab/`.

## Notation

| symbol | meaning | units | where in code |
|---|---|---|---|
| mid | Mid price, (bid + ask) / 2 | USD | `quotelab/strategies/signals.py` `mid_price` |
| m | Exchange mid at a fill, or at run end | USD | `quotelab/engine/exchange.py` `SimExchange._mid_of` |
| side | Side of an order or fill: +1 buy, -1 sell | none | `quotelab/types.py` `Side` |
| F | Fair value used for quoting | USD | `quotelab/strategies/quoting.py` `QuotingStrategy.fair_value` |
| e | OFI contribution of one book update | lots | `quotelab/strategies/signals.py` `ofi_contribution` |
| OFI | Normalised order-flow imbalance | none, in [-1, 1] | `quotelab/strategies/signals.py` `OfiTracker.value` |
| β | OFI coefficient in the fair value | USD per unit OFI | `quotelab/strategies/calibration.py` `fit_ofi` |
| σ | Mid volatility | USD per √s | `quotelab/strategies/signals.py` `RollingVol.sigma` |
| q | Inventory | base units | `quotelab/strategies/quoting.py` `QuotingStrategy.position_qty` |
| Q | Inventory | lots | `quotelab/strategies/quoting.py` `QuotingStrategy.position` |
| Δ | Order size | base units | `quotelab/strategies/glft.py` and `avellaneda_stoikov.py` (`delta`) |
| L | Order size | lots | config `strategy.order_lots` |
| M | Inventory limit, max_inventory_orders × L | lots | `quotelab/strategies/factory.py` `quoting_params` |
| δ | Distance of a quote from the mid | USD | `quotelab/strategies/calibration.py` `fit_intensity` (`delta_usd`) |
| λ(δ) | Rate of taker orders reaching δ past the mid, per side | per s | `quotelab/strategies/calibration.py` `fit_intensity` |
| A | Intensity prefactor | per s | `quotelab/strategies/calibration.py` `calibrate` (`a`) |
| k | Intensity decay | per USD | `quotelab/strategies/calibration.py` `calibrate` (`k`) |
| γ | Risk aversion | per USD per base unit | `quotelab/strategies/glft.py` `gamma_from_skew` |
| τ | Time to the A-S horizon | s | `quotelab/strategies/avellaneda_stoikov.py` `AvellanedaStoikov.tau` |
| x | γΔ / k | none | `quotelab/strategies/glft.py` `glft_coefficients` |
| c1 | GLFT half spread at zero inventory | USD | `quotelab/strategies/glft.py` `glft_coefficients` |
| c2 | GLFT change of each offset per unit of q | USD per base unit | `quotelab/strategies/glft.py` `glft_coefficients` |
| tick | Tick size | USD | `quotelab/types.py` `InstrumentSpec.tick_size` |
| α | Share of an unexplained queue drop taken as cancellation ahead of us | none, in [0, 1] | `quotelab/fills/queue_aware.py` `QueueAwareFill` |
| ρ | Participation: share of at-level trade volume reaching our queue | none, in (0, 1] | `quotelab/fills/queue_aware.py` `QueueAwareFill` |
| Q_ahead | Estimated lots queued ahead of our order | lots | `quotelab/fills/base.py` `RestingOrder.queue_ahead` |
| f | Fee rate, maker or taker | bps | config `fees` |

Naming: the strategy modules call the order size `delta`. Calibration calls the distance from the mid `delta_usd`. This note uses Δ for the first and δ for the second.

## a. Fair value

```text
mid     = (bid + ask) / 2
P_micro = (bid · ask_qty + ask · bid_qty) / (bid_qty + ask_qty)     (mid when both sizes are 0)
F       = P_micro + β · OFI        fair_value: microprice (default)
F       = mid + β · OFI            fair_value: mid

e       = bid_in − bid_out − ask_in + ask_out                       (one book update, lots)
bid_in  = bid_qty_new if bid_px_new ≥ bid_px_old, else 0
bid_out = bid_qty_old if bid_px_new ≤ bid_px_old, else 0
ask_in  = ask_qty_new if ask_px_new ≤ ask_px_old, else 0
ask_out = ask_qty_old if ask_px_new ≥ ask_px_old, else 0
OFI     = Σe / Σ|e|    over updates in (now − W, now]        (0 when the window has no flow)
```

Derivation sketch:
- The microprice weights each touch by the size on the opposite side. A large bid queue against a thin ask pulls the value toward the ask.
- Cont, Kukanov and Stoikov (2014) define OFI from best-level changes. Size added on the bid, or removed on the ask, counts as buying pressure.
- Dividing by gross flow gives a value in [-1, 1] that is comparable across quiet and busy periods.
- β is the slope of the mid change over the next `ofi_horizon_s` seconds on OFI, fitted on the earlier day.
- Entries at or before now − W are dropped, so the window is (now − W, now].

Unverified against the paper: the sign convention and the gross-flow normalisation follow the code and its docstring. They were not checked against Cont, Kukanov and Stoikov (2014).

**Config:** default `use_ofi: true`, `ofi_window_s: 5` in config/default.yaml. With `use_ofi` off, β is set to zero.

**Code:** `quotelab/strategies/signals.py` (`microprice`, `ofi_contribution`, `OfiTracker`), `quotelab/strategies/quoting.py` (`QuotingStrategy.fair_value`), `quotelab/strategies/calibration.py` (`fit_ofi`).

## b. Volatility

```text
move_i  = change of the grid mid since the previous grid point
secs_i  = seconds spanned by that step (a quiet gap spans several 1 s steps)
σ²      = Σ move_i² / Σ secs_i        over the newest steps whose seconds cover W_σ
σ       = √σ²                         (USD per √s)
σ_cal   = √( mean(diff²) / 1 s )      from the 1 s grid of the calibration day
```

Until the window is full, σ returns the calibrated σ_cal.

Derivation sketch:
- For a random walk with volatility σ, the variance of a change over T seconds is σ²T.
- Each squared change has expected value σ² times its seconds, so the sum of squared changes divided by the sum of seconds estimates σ² per second.
- A gap spanning several steps is one change over its full duration, so it neither inflates nor deflates the estimate.
- Old steps drop out once the remaining steps still cover W_σ.
- The calibration does not remove a mean, so σ_cal is a root mean square.
- The minute-kline cross-check (`klines_sigma`) is the standard deviation of 1-minute close-to-close changes divided by √60. It is diagnostic only.

**Config:** default `sigma_window_s: 300` in config/default.yaml.

**Code:** `quotelab/strategies/signals.py` (`RollingVol`, `RollingVol.sigma`), `quotelab/strategies/calibration.py` (`mid_grid`, `realised_sigma`, `klines_sigma`).

## c. Fill intensity

```text
X_i     = 2 · side_i · (px_i − mid_prev_i) / tick     (one taker order, deepest print; half-ticks)
N(δ)    = #{ taker orders with X_i ≥ 2δ / tick }
λ(δ)    = N(δ) / (2 · span_s)                          per side per second
ln λ(δ) = ln A − k · δ                                 OLS over levels with N(δ) ≥ min_hits
δ_j     = (j + ½) · tick,   j = 0 … max_delta_ticks − 1
A       = exp(intercept),   k = −slope
```

`mid_prev_i` is the mid of the last book update strictly before the trade. Prints that share a timestamp and taker side are one order, so only the deepest print is kept. `span_s` is the length of the book data in seconds.

Derivation sketch:
- A resting order at distance δ from the mid fills when a taker order moves the price past it.
- Counting taker orders that reach each δ gives a survival curve of taker flow. That count is what is fitted.
- Its logarithm is fitted as a line in δ, matching the exponential intensity used by A-S and GLFT.
- Levels with fewer than `min_hits` taker orders are dropped, because their counts are noisy.
- The reference mid is taken before each trade, so the excursion uses only information available then.
- Both sides are pooled and the span is doubled, which gives a rate per side.

Caveats:
- The counts are taker flow that reached a price. They are not fills of a resting order at that price. A and k describe that flow and are not queue-aware fill rates.
- The levels are nested: each count includes all deeper hits. The OLS standard errors treat the points as independent, so they are probably too small.
- These are excursion counts, not first-passage times.

**Config:** default `max_delta_ticks: 20`, `min_hits: 30` in config/default.yaml under `calibration`.

**Code:** `quotelab/strategies/calibration.py` (`aggressive_order_excursions`, `fit_intensity`, `fit_line`, `calibrate`).

## d. Fixed and skew quoting

```text
fixed:  bid offset = half,            ask offset = half
        half = fixed_half_spread_ticks · tick

skew:   bid offset = half + skew,     ask offset = half − skew
        half = skew_half_spread_ticks · tick
        skew = skew_ticks_per_order · tick · Q / L
```

Derivation sketch:
- Both rules are baselines for the strategy comparison. Their spreads come from config, not from calibration.
- The fixed rule ignores inventory.
- The skew rule moves both quotes against the position. Long inventory raises the bid offset and lowers the ask offset, so the side that reduces the position is more likely to fill.
- The skew is set in ticks per order, so the same setting gives the same inventory response on every symbol.
- Neither rule uses σ or the intensity fit.
- The class docstring calls the skew a heuristic that A-S and GLFT derive from first principles.

**Config:** default `fixed_half_spread_ticks: 0.5`, `skew_half_spread_ticks: 0.5`, `skew_ticks_per_order: 1.0` in config/default.yaml under `strategy`.

**Code:** `quotelab/strategies/fixed.py` (`FixedSpread`, `InventorySkew`), `quotelab/strategies/factory.py` (`build_strategy`).

## e. Avellaneda-Stoikov

```text
r          = F − q · γ · σ² · τ                        (reservation price)
half       = γΔσ²τ / 2 + ln(1 + γΔ / k) / (γΔ)
bid offset = half + q · γ · σ² · τ                     (below F)
ask offset = half − q · γ · σ² · τ                     (above F)

τ, session:     seconds until the next 00:00 UTC
τ, match_glft:  c2 / (γ · σ²)     with σ the calibrated σ and c2 from section f
```

Derivation sketch:
- The model assumes a mid with volatility σ, Poisson fills with intensity A·exp(−kδ), and exponential utility with risk aversion γ over a finite horizon τ.
- The reservation price moves against inventory by γσ²τ per unit.
- Quoting around the reservation price with the optimal half spread gives the offsets above.
- The Δ scaling is a repo choice. The docstring says it is not taken from the A-S paper. At Δ = 1 the formulas match the A-S closed forms as recalled.
- The session horizon falls toward zero before midnight UTC and jumps back at 00:00 UTC.
- With τ = c2 / (γσ²), the skew per unit q equals c2, the GLFT value. The two models agree when the live σ equals the calibrated σ, and diverge when it moves.

Unverified against the paper: the A-S closed form, (2/γ) ln(1 + γ/k) for the full spread at Δ = 1, and the derivation steps are from memory. They were not checked against Avellaneda and Stoikov (2008). The docstring attribution of the Δ scaling to Gueant is also unverified.

**Config:** default `as_horizon: match_glft` in config/default.yaml under `strategy`.

**Code:** `quotelab/strategies/avellaneda_stoikov.py` (`as_offsets`, `seconds_to_utc_midnight`, `AvellanedaStoikov.tau`), `quotelab/strategies/factory.py` (`matched_as_horizon`).

## f. GLFT

```text
x          = γΔ / k
c1         = ln(1 + x) / (γΔ)
c2         = √( γσ² / (2AΔk) · (1 + x)^(1/x + 1) )
bid offset = c1 + (2q + Δ) / 2 · c2
ask offset = c1 − (2q − Δ) / 2 · c2

γ solves   Δ · c2(γ) = target,   target = glft_skew_ticks_per_order · tick
           with σ, A, k from calibration
```

γ is found by bisection on ln γ over [−60, 60], in 200 steps (`gamma_from_skew`). The target is in USD per order. A target outside the skew range reachable for γ in [e^−60, e^60] raises `ValueError`.

Derivation sketch:
- Setting: the mid follows dS = σ dW. Each side fills in clips of Δ as a Poisson process with intensity A·exp(−kδ). Inventory stays in [−M, M]. The market maker maximises E[−exp(−γ(cash + q·S))].
- Guess a value function of the form −exp(−γ(x + qS + θ(t, q))). The HJB equation then reduces to one ODE per inventory level, with a running penalty −½γσ²q² and one term per side for the fills.
- Maximising each fill term gives the optimal offsets:

  ```text
  bid offset = ln(1 + γΔ/k) / (γΔ) + (θ(q) − θ(q + Δ)) / Δ
  ask offset = ln(1 + γΔ/k) / (γΔ) + (θ(q) − θ(q − Δ)) / Δ
  ```

  The first term is c1.
- Far from the horizon, θ stops depending on t. Treating q as continuous turns the ODE system into one equation whose solution is close to a quadratic, θ(q) ≈ −½·c2·q². Substituting it gives (θ(q) − θ(q ± Δ))/Δ = (c2/2)(±2q + Δ), which is the (2q ± Δ)/2 · c2 term above. The value of c2 comes from solving that continuous equation. This note summarises that step and does not reproduce it.
- c1 is the half spread at zero inventory. c2 is the change in each offset per unit of inventory.
- Each offset is linear in q. The inventory limit M is a separate hard bound (section g).
- The closed form is asymptotic. The module docstring says it is most accurate at small inventory and less accurate near M, where the exact solution of the ODE system bends away from these straight lines.
- c2 increases with γ. The factor (1 + x)^(1/x + 1) increases in x, and x increases with γ, so the root of the skew equation is unique.
- At fixed γ, c2 scales with σ, so the GLFT skew scales with σ and not σ².
- The target is stated in ticks per order, so γ is set in units the operator can read.

Unverified against the paper: the exponent k/(γΔ) + 1 in c2, the (2q ± Δ)/2 placement, and the Δ scaling of c1 and c2 follow the code docstring. They were not checked against Gueant (2017), eq. 4.6-4.7, or GLFT (2013). The equation numbers and the attribution to "GLFT (2013)" are unverified.

**Config:** default `glft_skew_ticks_per_order: 1.0` in config/default.yaml under `strategy`.

**Code:** `quotelab/strategies/glft.py` (`glft_coefficients`, `glft_offsets`, `gamma_from_skew`, `Glft.offsets`), `quotelab/strategies/factory.py` (`default_gamma`).

## g. Tick rounding and post-only clamping

```text
bid_px = floor((F − bid offset) / tick + 1e-9)
ask_px = ceil((F + ask offset) / tick − 1e-9)

strategy clamp, on the strategy's (delayed) book:
    if bid_px ≥ book.ask_px:  bid_px = book.ask_px − 1      (counted in bid_clamps)
    if ask_px ≤ book.bid_px:  ask_px = book.bid_px + 1      (counted in ask_clamps)

bid kept only if Q + L ≤ M
ask kept only if Q − L ≥ −M

exchange, on its own book when the order arrives:
    a buy at or above the best ask is rejected as post_only
    a sell at or below the best bid is rejected as post_only
```

Derivation sketch:
- Rounding moves the bid down and the ask up, so a quote is never more aggressive than the model asked for.
- The 1e-9 term stops floating-point error from moving an exact tick off the grid.
- The clamp keeps each quote one tick inside the touch the strategy sees, so it stays post-only.
- The strategy acts on a book delayed by data latency. The exchange checks against its own book when the order arrives. Both checks stay.
- The inventory test uses the strategy's position in lots and drops a side before it is sent.
- `quote_updates` counts every quote calculation. The clamp counters count only quotes the clamp moved, so their ratio to `quote_updates` is the share of quotes where the model asked for a price at or through the opposite touch.

**Config:** default `max_inventory_orders: 10` in config/default.yaml under `strategy`.

**Code:** `quotelab/strategies/quoting.py` (`QuotingStrategy.quotes`, `ROUNDING_EPS`), `quotelab/engine/exchange.py` (`SimExchange._would_take`), `quotelab/strategies/factory.py` (`quoting_params`).

## h. Fill models and latency

Fill models decide which resting orders fill. A fill is at the resting order's own price.

```text
optimistic:    a buy fills in full on a trade at or below its price,
               or when the book ask is at or below its price (sell: mirrored)
trade-through: a buy fills in full only on a trade strictly below its price;
               book updates never fill (sell: mirrored)
queue-aware:   (sizes in lots)
  on joining:     Q_ahead = displayed size when joining the touch
                  Q_ahead = 0 when improving the touch
                  Q_ahead = None (no fill possible) when behind the visible touch
  trade strictly through our price:        full fill
  trade at our price from the other side (no fill while Q_ahead is None):
                                           eff = ρ · trade_qty
                                           lots = floor(eff − Q_ahead)
                                           fill min(order size, lots) if lots ≥ 1
                                           Q_ahead = max(0, Q_ahead − eff)
  book update where the opposite touch reaches or passes our price:  full fill
  any other book update, by where our price sits on our side of the new book:
    Q_ahead is None:                  set again by the joining rule above
    strictly better than the touch:   Q_ahead = 0
    at the touch, which was already at our price:
                  cancelled = max(0, (old size − new size) − traded at our price since last update)
                  Q_ahead = min(max(0, Q_ahead − α · cancelled), new size)
    at the touch, which has just moved to our price:   Q_ahead = min(Q_ahead, new size)
    behind the touch:                 Q_ahead unchanged
```

Latency: market data reaches the trader after `data_latency`. Orders reach the exchange after `order_latency`, and cancels after `cancel_latency`. Events with equal timestamps run in this order: market data, exchange arrivals, trader deliveries, requote wake-ups, timers. Each side keeps at most one working order. A changed quote sends a cancel and frees that side at once, so a replacement can go out in the same step. The old order can still fill until the cancel reaches the exchange. A new order is sent only when that side has no working order and at least `min_requote` has passed since that side's last send. Quotes below one lot or below the minimum notional are not sent. They count as local rejects, once per run of identical repeats.

Derivation sketch:
- A trade reaches a resting order only in part. ρ scales the trade volume that reaches our queue.
- The queue ahead is not observed. It starts at the displayed size when we join, and trades at our price drain it.
- A drop at our price that no trade explains is treated as cancellation. A share α of it is taken from ahead of us.
- The trader acts on delayed data, while the exchange fills against its book at arrival time.
- Fills found in a trade are recorded at the mid of the current book. Fills found in a book update are recorded at the mid of the previous book.
- Optimistic fills are expected to overstate fill rates and trade-through fills to understate them. Neither is a strict bound, because a different fill path changes inventory and so every later quote.

**Config:** default `model: queue`, `alpha: 0.5`, `participation: 1.0` under `fills`, and `data_latency_ms: 5`, `order_latency_ms: 10`, `cancel_latency_ms: 10`, `min_requote_ms: 100`, `timer_ms: 1000` under `engine` in config/default.yaml.

**Code:** `quotelab/fills/base.py` (`RestingOrder`, `FillModel`), `quotelab/fills/optimistic.py` (`OptimisticFill`), `quotelab/fills/trade_through.py` (`TradeThroughFill`), `quotelab/fills/queue_aware.py` (`QueueAwareFill`), `quotelab/fills/factory.py` (`fill_model_from_config`), `quotelab/engine/config.py` (`engine_config`), `quotelab/engine/backtest.py` (`Backtest.run`), `quotelab/engine/orders.py` (`OrderManager._cancel_if_changed`, `OrderManager._send_if_allowed`).

## i. PnL attribution and reconciliation

```text
fill i:  side_i (+1 buy, −1 sell), px_i, qty_i, exchange mid at fill m_i, fee fee_i
run end: mid m_end;  funding payments pay_j (positive when we pay)

spread    = Σ over quote fills     side_i (m_i − px_i) qty_i
hedging   = Σ over hedge fills     side_i (m_i − px_i) qty_i − fee_i
fees      = −Σ over quote fills    fee_i
inventory = Σ over all fills       side_i qty_i (m_end − m_i)
funding   = −Σ_j pay_j
total     = spread + hedging + fees + inventory + funding
          = Σ side_i qty_i (m_end − px_i) − Σ fee_i − Σ_j pay_j
```

Derivation sketch:
- Each fill is split at the mid when it happened. The gap between fill price and that mid is the captured edge. Quote edge goes to spread and hedge edge to hedging.
- The move from that mid to the final mid, on the quantity held, goes to inventory.
- Fees and funding are separate lines.
- The m_i terms cancel in the sum, so the total depends only on fill prices, fees, funding and the final mid.
- The run starts flat with zero cash, so the identity is exact. The total equals final equity: cash plus the position marked at the final mid.
- `reconcile` compares the total with the engine's final equity. The two come from different code paths. A gap of 0.01 USD or more raises `AttributionMismatch`.

**Code:** `quotelab/analytics/attribution.py` (`attribution`, `run_attribution`, `reconcile`), `quotelab/engine/portfolio.py` (`Portfolio.equity`), `quotelab/engine/exchange.py` (`SimExchange._fill_row`).

## j. Markouts

```text
t_h         = ts + h                              h in seconds
mid_after   = last book mid at or before t_h      (book mid = (bid + ask) / 2 of every book row;
                                                  targets after the last observed mid are dropped)
markout_usd = side · (mid_after − px) · qty
markout_bps = side · (mid_after − px) / px · 1e4
adverse_bps = side · (mid_after − m_fill) / px · 1e4
SE          = stddev_samp(markout_bps) / √n       (NULL when n < 2)
```

Horizons are 1, 5, 30 and 60 s (`HORIZONS_S`).

Derivation sketch:
- A markout values a fill against a later mid. A positive value means the fill beat that mid.
- `markout_bps` includes the edge captured at the fill. `adverse_bps` removes that edge, so it keeps only the mid move after the fill.
- The ASOF join takes the last mid at or before the target time. No later mid enters a row.
- The standard error treats fills as independent. Fills that cluster in time make it too small.
- Figures draw error bars at 1.96 SE (`SE_Z`), a normal approximation.

**Code:** `quotelab/analytics/queries.py` (`book_mids`, `markouts`, `markout_summary`, `HORIZONS_S`), `quotelab/analytics/sql/markouts.sql`, `quotelab/analytics/sql/markout_summary.sql`, `quotelab/report/figures.py` (`SE_Z`).

## k. E7 fee tiers

```text
fee on a fill   = notional · f / 1e4          (positive is paid, negative is a rebate)
PnL(f_maker)    = PnL_base + (f_base − f_maker) / 1e4 · V_maker
break-even f*   = f_base + PnL_base / V_maker · 1e4
```

PnL_base and V_maker are totals over the test days. V_maker is maker notional in USD. The break-even fee is the rate at which total PnL is zero.

Derivation sketch:
- A maker fee change only changes cash. It moves PnL by the fee difference times maker volume.
- This holds only if fills do not depend on the fee. The fee rerun check (`fee_rerun_check`) reruns with the new fee and records whether the fills match.
- The tier table is predicted from the baseline. The reruns listed under `e7_rerun_maker_bps` test that prediction.
- The break-even fee solves the same identity for zero PnL. A negative value is a rebate.
- With all risk controls off, the baseline run has no risk layer.

**Config:** default `e7_maker_bps: [2.0, 1.0, 0.0, -0.5, -1.0]` and `e7_rerun_maker_bps: [0.0]` under `experiments`, and `maker_bps: 2.0` under `fees`, in config/default.yaml.

**Code:** `quotelab/report/tables.py` (`fee_tier_table`, `break_even_maker_bps`, `fee_rerun_check`), `quotelab/engine/portfolio.py` (`fee_for`).

## l. Risk controls

The hard inventory bound always applies. The strategy drops a side that would take Q beyond M. The risk manager applies the same test to the ramp-scaled quotes.

```text
kill_inventory:  |Q| > K,  K = kill_inventory_orders · L          (checked on fills)
kill_drawdown:   peak equity − equity > D                       (checked on timers and before each quote)
                 D = max_drawdown_bps / 1e4 · to_qty(M) · mid_0
                 to_qty(M): the inventory limit in base units;  mid_0: mid of the first book
kill_stale:      local time since the trader last saw a book > stale_after_s   (checked on timers)
```

Kill switches latch in `_kill`. Nothing in `RiskManager` clears them. Every kill needs `kill_switches` on.

```text
cb_gap:       exchange time between consecutive market events > max_gap_s   (checked when the next event arrives)
cb_spread:    spread / mid · 1e4 > max_spread_bps
cb_vol:       σ over vol_short_s > vol_ratio · σ over vol_long_s     (both windows full)
cb_toxicity:  |OFI| ≥ toxicity_threshold, with ≥ toxicity_min_updates in toxicity_window_s
```

A breaker trip pauses both sides for `cooldown_s` and resets the size ramp.

Size ramp: the multiplier starts at the first of `ramp_levels`. It steps up one level after each `ramp_step_hours` since the last change, where a change is the start, a step, or a trip.

Hedge (optional): when |Q| exceeds `hedge_threshold_orders` × L, take the touch for the excess over `hedge_to_orders` × L. After a kill with `flatten_on_kill`, flatten the whole position.

Derivation sketch:
- Kill switches latch: nothing in a run clears them. The code docstring says a human would reset them in production. They cover states that do not clear themselves: stale data, large inventory, deep drawdown.
- `cb_gap` can trip only once data resumes. A feed that stops completely is caught by `kill_stale` on the timer instead. In a backtest the timer runs on exchange time, so `kill_stale` trips only across a gap in the recorded data.
- Circuit breakers are temporary. Each trip pauses quoting for `cooldown_s`.
- The size ramp limits exposure after a trip. Quotes restart at the first level and climb only with clean time.
- The drawdown limit is fixed from the first book's mid. The peak starts at the first equity seen.
- Each breaker kind is logged once per pause, so a condition that stays true does not flood the log.
- With all three flags off and no hedge threshold, the worker uses `NoRisk`.

**Config:** default `kill_switches`, `circuit_breakers`, `size_ramp`: false, with thresholds in the `risk` section of config/default.yaml. Defaults: `kill_inventory_orders: 15`, `max_drawdown_bps: 100`, `stale_after_s: 5`, `cooldown_s: 30`, `vol_short_s: 10`, `vol_long_s: 600`, `vol_ratio: 4.0`, `max_spread_bps: 5.0`, `toxicity_window_s: 5`, `toxicity_threshold: 0.8`, `toxicity_min_updates: 20`, `max_gap_s: 2.0`, `ramp_levels: [1, 2, 4]`, `ramp_step_hours: 4`, `hedge_threshold_orders: null`, `hedge_to_orders: 0`, `flatten_on_kill: true`.

**Code:** `quotelab/risk/manager.py` (`RiskConfig`, `risk_config`, `RiskManager.filter_quotes`, `RiskManager.hedge`, `RiskManager._check_gap`, `RiskManager._check_breakers`, `RiskManager._vol_spiked`, `RiskManager._maybe_ramp_up`, `RiskManager._trip`, `RiskManager._kill`, `RiskManager._check_drawdown`), `quotelab/risk/base.py` (`NoRisk`), `quotelab/experiments/worker.py` (`risk_layer`), `quotelab/strategies/quoting.py` (`QuotingStrategy.quotes`).

## m. Statistics

```text
mean    = (1/n) Σ v_d                      v_d = one value per test day
CI      = [quantile(tail/2), quantile(1 − tail/2)] of B resampled means,   tail = 1 − level
B = 10000, level = 0.95, seed = 0          (NaN interval when n < 2)
fill_rate          = quote fills / orders sent
pnl_per_volume_bps = pnl / maker volume · 1e4
max_drawdown       = max over t of (running peak_t − equity_t)     (≥ 0)
```

An hour is `high` when its realised variance is above its symbol's median, and `low` otherwise.

Derivation sketch:
- The unit of resampling is the test day, as the report tables pass per-day values. With few days the interval is wide and only rough.
- Percentile intervals are simple, and the fixed seed makes them reproducible.
- Resampling assumes the days are exchangeable. Serial dependence across days is ignored.
- Max drawdown depends on how finely equity is sampled (`sample_ms`, default 1000 ms).
- Regime labels use each hour's own realised variance. They describe what happened, not what was known in advance.

**Code:** `quotelab/analytics/stats.py` (`bootstrap_mean_ci`, `max_drawdown`, `run_metrics`), `quotelab/report/tables.py` (`regime_labels`), `quotelab/experiments/worker.py` (`hourly_variance`).

## References

- Avellaneda, M. and Stoikov, S. (2008). High-frequency trading in a limit order book. Cited in `avellaneda_stoikov.py` as "Avellaneda-Stoikov (2008)". Bibliographic details are from memory and not checked.
- Gueant, O., Lehalle, C.-A. and Fernandez-Tapia, J. (2013). Dealing with the inventory risk: a solution to the market making problem. Cited in `glft.py` as "GLFT (2013)". Title and author list are from memory and not checked.
- Gueant, O. (2017). Optimal market making. Cited in `glft.py` as "Gueant (2017), eq. 4.6-4.7". Details are from memory and not checked.
- Cont, R., Kukanov, A. and Stoikov, S. (2014). The price impact of order book events. Cited in `signals.py` as "Cont, Kukanov, Stoikov 2014". Details are from memory and not checked.
