# Interview notes

Thirty questions an interviewer for a market-making or quant-developer role is likely to ask about this repository, with short answers drawn from the code. Results are not repeated here. They live in the generated reports, so every number has one source. Formulas are in [MODEL.md](MODEL.md), engine design in [DESIGN.md](DESIGN.md).

## Pricing

**1. What is your fair value, and why not just the mid?**
The microprice, `(bid · ask_qty + ask · bid_qty) / (bid_qty + ask_qty)`, plus `β · OFI` (`QuotingStrategy.fair_value`). When the bid queue is much larger than the ask queue, the next mid move is more likely up, and the microprice leans that way before the mid does. β is fitted on the previous day by regressing the next-second mid change on OFI (`fit_ofi`). The signals are standard public indicators, not alpha claims.

**2. Why do spreads widen with volatility?**
A quote is an option the taker can exercise against you. The more the price can move before you can requote or unwind, the more each fill costs you on average. In A-S the inventory term is γσ²τ. In GLFT the inventory coefficient c2 grows with σ, and the zero-inventory half spread is c1 + Δ·c2/2, so it widens with σ as well. Unit tests check that the GLFT spread at zero inventory grows with σ, and that a higher γ raises c2 while lowering c1, trading a little spread for a much stronger skew (`tests/test_strategies.py`).

**3. How does the skew work, and why does it always push inventory back to zero?**
Each GLFT offset is linear in inventory q: the bid offset is c1 + (2q + Δ)/2 · c2 and the ask offset is c1 − (2q − Δ)/2 · c2. When long, the bid moves away and the ask moves closer, so sells become more likely than buys. The quotes are symmetric at q = 0. Both properties are unit-tested.

**4. Avellaneda-Stoikov or GLFT, and why?**
GLFT is the default. A-S needs a terminal horizon τ, and for a perpetual future there is no natural end of day: with a session horizon the spread collapses before 00:00 UTC and jumps back after it. GLFT is the infinite-horizon solution with an inventory bound, so its quotes depend only on q, σ, A, k and γ. E2 runs A-S both ways: `as_session`, and `as_rolling`, where τ is chosen so the skew matches GLFT at the calibrated σ.

**5. How did you choose risk aversion γ?**
γ is not readable on its own, so the config states the skew instead: `glft_skew_ticks_per_order: 1.0`, meaning one order of inventory moves each quote by one tick. `gamma_from_skew` back-solves γ by bisection on ln γ. The root is unique because c2 increases in γ. E3 varies this skew from 0.25 to 4 ticks.

**6. What are A and k, and how are they calibrated?**
They describe how often taker flow reaches a distance δ from the mid: λ(δ) = A·exp(−kδ). `fit_intensity` takes the previous day's trades, measures how far each taker order went from the mid just before it (keeping the deepest print per order), counts orders reaching each half-tick level, and fits ln λ against δ by OLS. Two caveats: those are counts of flow reaching a price, not queue-aware fill rates, and the nested counts make the OLS standard errors too small. A first-passage estimator was tried first and rejected: on BTCUSDT the mid wanders far enough in a few seconds that every level was reached by drift rather than by a sweep, and the fitted curve came out nearly flat.

**7. How are order size and the inventory limit set?**
Size is a fixed number of lots per symbol (`strategy.order_lots`). The limit is `max_inventory_orders × order_lots`. The strategy drops a side whose fill would breach it, and the risk manager re-applies the same test after the size ramp scales the quotes. E3 varies size (1×, 2×, 4×) and the limit (2 to 20 orders).

**8. Why clamp quotes and round outward?**
Binance quotes are on a tick grid and a maker quote must not cross. The bid is floored and the ask ceiled, so rounding never makes a quote more aggressive than the model asked for. Then each side is clamped one tick inside the opposite touch the strategy last saw. The exchange still rejects a crossing order as `post_only` against its own, newer book, because the strategy's view is 5 ms old.

## Backtest realism

**9. What is queue priority, and how do you estimate queue position without L2 data?**
At one price level, orders fill first come, first served, so a new order waits behind the size already displayed. `QueueAwareFill` sets the queue ahead to the displayed touch size on joining, or to zero when improving the touch. Trades at our price drain it, scaled by `participation`. A size drop at our level that no trade explains is treated as cancellations, and a share `alpha` of it is taken from ahead of us. Whatever trade volume is left after the queue ahead is consumed fills us. A trade or book that goes through our price fills us in full.

**10. Why does a naive backtest overstate fills?**
It assumes you fill whenever the price touches your quote, as if you were first in the queue. In reality you are behind everyone who arrived earlier, and touches that do not trade through often leave you unfilled at exactly the moments the price is about to move against the queue. That is adverse selection. E1 runs the same strategy under optimistic, trade-through and queue-aware fills, with and without latency, and a stress case (`alpha` 0, `participation` 0.5). The gap between those rows is the size of the assumption. The ladder is not monotone in fills: in the Binance run the latency rung filled more than `optimistic`, because a quote keeps its stale price until the update arrives and gets hit in that window, and that rung lost the most.

**11. How is latency modelled?**
There are two clocks. The exchange processes market data at its exchange time. The trader sees it `data_latency` (5 ms) later. Orders and cancels reach the exchange `order_latency` / `cancel_latency` (10 ms) after they are sent. At equal timestamps a market event goes first, so an order arriving in the same nanosecond as a trade misses it, and a cancel arriving with a trade is too late. Both choices are conservative. E4 sweeps order latency from 1 to 500 ms.

**12. How do you prevent look-ahead?**
Parameters come from the previous day only (walk-forward pairs). Within a day, the strategy has no handle on the exchange and sees only delayed messages. Calibration compares each trade with the book strictly before it. Markouts use an ASOF join that takes the last mid at or before the horizon. The event-study window (E6) is chosen from test-day volatility, but it only decides what to replay and never feeds a parameter. The engine raises an error if a market event is older than its clock.

**13. Why event-driven instead of a vectorised backtest?**
Queue position, latency and the requote throttle are all path-dependent. Whether an order rests in time for a trade depends on when it was sent, which depends on earlier fills. A fixed time step would either reorder those events or add a step size that changes results. The engine processes each pending item at its own nanosecond timestamp from a few time-ordered queues.

**14. `alpha` and `participation` are not fitted. Isn't that a problem?**
Yes, and it is stated as a limitation. Fitting them needs order-level (L3) data or our own live fills, and this project has neither. Instead, E1 shows how far results move between the queue model, a pessimistic stress and the two bounds, so a reader can judge how much the conclusions depend on them.

**15. How do you know the engine is correct?**
Four checks. Unit tests pin exact timelines, for example "an order rests only after its latency" and "a cancel in flight does not stop a fill". PnL attribution is computed from the fills and must reconcile with the engine's final equity to within a cent, or the job fails (`AttributionMismatch`). Identical inputs give bit-identical runs. E7 reruns the baseline at a different maker fee and checks the fills are identical and the PnL moves exactly as the fee formula predicts.

## Performance analysis

**16. How do you attribute PnL?**
Each fill is split at the exchange mid when it happened. Spread is the edge against that mid on quote fills. Inventory is the move from that mid to the final mid on the quantity held. Fees, hedging and funding are separate lines. The mids cancel in the sum, so the identity is exact and equals final equity (`quotelab/analytics/attribution.py`).

**17. What is a markout, and why report two?**
`markout_bps` is side · (mid at t + h − fill price) / price, at 1, 5, 30 and 60 s. It includes the edge captured at the fill. `adverse_bps` uses the mid at the fill instead of the fill price, so it isolates the price move after the fill, which is pure adverse selection. A market maker with positive markout at short horizons and negative adverse at long ones is being picked off slowly. Both come from one DuckDB ASOF query (`markouts.sql`).

**18. How do you measure fill rate and why does it matter?**
Quote fills per order sent. A low rate with many orders means the quotes are rarely competitive, or are cancelled before they reach the front of the queue. A high rate together with negative adverse markouts means the quotes are too tight. It is reported per experiment alongside uptime (the share of time both sides are quoted).

**19. What dominates PnL in your results?**
On both datasets the report shows the fee line as the largest negative component of baseline `glft` PnL: at the assumed 2 bps maker fee, the half-spread captured on a quote at or near the touch is smaller than the fee. Fees are not the whole story: on the Binance data PnL is negative even before fees, because the inventory line (the mid moving against the position after fills, i.e. adverse selection) is larger than the spread captured, and the markouts say the same. That is why E7 reports a break-even maker fee rather than a profit. Large market makers often pay lower or negative maker fees than the default tier, but I did not check Binance's current schedule, so the fee values are config assumptions.

**20. How do you compute confidence intervals with so little data?**
A percentile bootstrap over test days (10,000 resamples, fixed seed). Days are the unit because fills within a day are strongly dependent. With four test days per symbol the intervals are wide, and the report says so instead of claiming significance.

**21. How do fee tiers work, and when does that shortcut break?**
A maker fee change only moves cash: PnL(f) = PnL_base + (f_base − f) · maker volume / 10⁴. This is exact only if the fills do not depend on the fee. This strategy never looks at fees when quoting, so they don't. The E7 rerun at 0 bps checks it on a real run. A strategy that widened its quotes to cover fees would break the shortcut.

## Risk

**22. How does the kill switch work?**
`RiskManager` latches three kill switches. `kill_stale` fires when no book has arrived for 5 s, checked on the 1 s timer. `kill_drawdown` fires when equity falls more than D below its peak. `kill_inventory` fires when |position| exceeds 15 orders. Once killed, no quote is sent and, with `flatten_on_kill`, the position is flattened with a taker hedge. Nothing in code clears a kill: a human has to restart.

**23. How did you set the drawdown limit?**
D = `max_drawdown_bps` (100) / 10⁴ × maximum position notional, using the first mid. At the inventory limit, a 1 % move against the full position is what a 100 bps limit allows. Tying it to the position limit makes it scale with symbol and size instead of being a hard-coded dollar amount.

**24. Why is `kill_inventory` at 15 orders when the limit is 10?**
It is a backstop. The strategy and the risk layer already refuse quotes that would take inventory past 10 orders, so the switch only fires if that logic is bypassed, for example by a bug or by fills arriving while the trader's view is stale.

**25. Kill switches versus circuit breakers?**
Circuit breakers handle conditions that clear by themselves: a gap in market data (`cb_gap`), a wide spread (`cb_spread`), short-term volatility four times the long-term level (`cb_vol`), and one-sided order flow (`cb_toxicity`). A trip pauses both sides for 30 s and resets the size ramp. Kill switches cover states that don't clear: stale data, deep drawdown, runaway inventory. E6 replays the most volatile hour of the test days with all controls off and then on.

**26. What is the size ramp?**
A staged rollout: quotes start at 1× size and step to 2× and then 4× after each 4 hours without a breaker trip. A trip drops back to 1×. It limits exposure exactly when the strategy is least proven: at startup and right after something went wrong.

**27. When would you hedge, and what does it cost?**
`RiskManager.hedge` can take the touch when |position| exceeds `hedge_threshold_orders`. Hedging pays the taker fee (5 bps here) plus half the spread, and it gives up the option of being filled passively on the reducing side. It is off by default, because the GLFT skew already pushes inventory back for less cost. It is used for flattening after a kill.

## Live

**28. How does the paper runner work?**
`quotelab paper` opens Binance's public combined stream (`bookTicker` and `trade`) with `asyncio` and `websockets`. No keys are used and no trading endpoint exists in the code. It records 15 minutes and calibrates on them, then feeds each message into the same `Backtest` engine through `push`, simulating fills with the same queue-aware model. If a message is stamped earlier than the engine clock, because the two streams interleave out of order, it is processed at the clock and the clamp is logged. A heartbeat advances the engine clock through silence, so timers keep firing and the stale-data kill switch can trip. A silent socket is dropped after 10 s and reconnected with doubling backoff. Everything is written to Parquet.

**29. How would you investigate a gap between live and backtest?**
Replay exactly the recorded window through the ordinary backtest with the same config and calibration, then find the first fill that differs. Before that point the cause must be in the inputs: messages in a different order (clamps, trades arriving after a same-time book update), a different clock, or different latency. After it, path dependence takes over, because one different fill changes inventory, skew and every later quote. `paper-compare` reports the first divergence, the clamp and reversal counts, and measured arrival delay against the assumed 5 ms.

**30. What would you change with true L2 data?**
Track queue position from depth at our price instead of the touch only, so orders behind the touch could rest and fill. Fit `alpha` from observed cancellations. Calibrate fill intensity per depth level instead of from trade excursions. Measure market impact by replaying our size against the book. Add level-two order-book imbalance as a signal. Order-level (L3) data would remove the queue estimate altogether.

## Glossary

| Term | Meaning |
|---|---|
| Adverse selection | Being filled mainly when the price is about to move against you, because better-informed traders choose when to trade with your quote |
| ASOF join | Join each row to the most recent row of another table at or before its time |
| Bootstrap | Estimating a statistic's uncertainty by resampling the data with replacement |
| bps | Basis point, one hundredth of a percent |
| Circuit breaker | A temporary pause triggered by a market condition, lifted after a cooldown |
| Clamp (live) | Processing a late message at the engine's current time instead of its own timestamp |
| Fill rate | Quote fills per order sent |
| Funding | Periodic payment between longs and shorts on a perpetual future that ties it to spot |
| GLFT | Gueant-Lehalle-Fernandez-Tapia, the infinite-horizon market-making model with an inventory bound |
| Kill switch | A latched stop that needs a human to reset |
| Look-ahead bias | Using information in a backtest that would not have been available at decision time |
| Maker / taker | A maker's order rests in the book; a taker's order trades against it immediately |
| Markout | PnL of a fill measured against the mid some time later |
| Microprice | Mid weighted by the opposite-side queue sizes |
| OFI | Order-flow imbalance: net size added to the bid and removed from the ask |
| Post-only | An order that is rejected instead of trading if it would cross the spread |
| Queue priority | First-come, first-served matching at one price level |
| Reservation price | The price at which the market maker is indifferent to one more unit of inventory |
| Size ramp | Starting small and increasing order size only after clean operation |
| Skew | Shifting both quotes against inventory so the reducing side fills more often |
| Tick | The smallest price increment |
| Touch | The best bid and best ask |
| Walk-forward | Calibrating on one period and testing on the next, never on the same data |
