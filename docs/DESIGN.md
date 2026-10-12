# QuoteLab design

This note describes how the simulator is built and what it assumes. It covers why the engine is event-driven, how look-ahead is blocked, how fills are modelled, and what is approximated. Results, strategy formulas and experiment outcomes are out of scope.

QuoteLab replays public Binance USD-M data for BTCUSDT and ETHUSDT (`config/default.yaml`). A simulated exchange and a simulated trader exchange messages with delays. The same loop serves backtests and paper trading.

## 1. Why event-driven

Market data arrives as timestamped events: top-of-book changes (`BookEvent`), trades (`TradeEvent`) and funding (`FundingEvent`). Trader decisions and exchange actions fall between these events and are delayed. A fixed time step would either blur the ordering or add a step size that changes results. An event loop places each pending action at its own timestamp.

The engine holds pending work in five places, all in time order:

| Pending work | Structure | Delay added | Where it is added |
|---|---|---|---|
| New orders and hedges, trader to exchange | `order_queue` (deque) | `order_latency_ns`, 10 ms | `Backtest._act`, `Backtest._maybe_hedge` |
| Cancels, trader to exchange | `cancel_queue` (deque) | `cancel_latency_ns`, 10 ms | `Backtest._act` |
| Market data, own fills, rejections, cancel results, funding, exchange to trader | `trader_queue` (deque) | `data_latency_ns`, 5 ms | `Backtest._deliver` |
| Requote wake-ups | `wakes` (heap) | `min_requote_ns`, 100 ms | `OrderManager.reconcile` returns the time; `Backtest._act` pushes it |
| Strategy timer and sample grid | `next_timer`, `next_sample` | 1000 ms each | `Backtest._on_timer`, `Backtest._record_samples_until` |

All delays are constants from `EngineConfig` (`quotelab/engine/config.py`), read from the `engine` block of `config/default.yaml`. Nothing is sampled.

The exchange (`SimExchange`) and the trader (`Portfolio`, `OrderManager`, the strategy) keep separate views. The trader never reads the exchange book. It only sees messages that have been delivered.

Tie order. `Backtest._drain_before` processes items due at the same nanosecond in this order: exchange arrivals, trader deliveries, wake-ups, timers. A market event always goes first. `Backtest.push` drains only items strictly before the event's timestamp, so anything due at that instant runs after it. `run` is `push` for every event followed by `finish`, so backtests and paper sessions share this order. Sample snapshots for a grid time are taken before anything else at that time. Both consequences are conservative. An order that reaches the exchange in the same nanosecond as a trade misses that trade. A cancel that reaches the exchange in the same nanosecond as a trade is too late, so the order can still fill.

Sample rows come from `Backtest._sample`. Each one records the exchange-side state on the grid: mid, bid, ask, position, cash, equity and fee and funding totals. Runs are repeatable. `test_same_input_gives_identical_runs` in `tests/test_backtest.py` runs the same synthetic input twice and compares fills, samples and summary, leaving out wall-clock fields.

## 2. How look-ahead is prevented

Look-ahead means a decision uses information the trader could not yet have. The loop blocks it in these places.

1. Ordering. `Backtest.push` raises `ValueError` if an event is older than the engine clock. Events come from `events_from_frames` (`quotelab/data/loader.py`), which sorts by timestamp, then kind (trade, book, funding), then input row. Equal-timestamp events keep that order.
2. Exchange first. `Backtest._on_market` lets the exchange update its book or match a trade. It then delivers any resulting fills and the market event to the trader, each at `ev.ts + data_latency_ns`.
3. Matching against the pre-update state. `SimExchange.on_trade` matches against the book before the trade is applied. `SimExchange.on_book` records a book-cross fill at the previous book's mid, not the new one (`test_book_cross_fill_is_recorded_at_the_previous_books_mid` in `tests/test_exchange.py`).
4. Latency gate. An order can fill only after it reaches the exchange. `test_order_rests_only_after_order_latency` in `tests/test_backtest.py` pins this timeline:

| Time (ms) | What happens |
|---|---|
| 0 | Exchange publishes a book |
| 5 | Trader receives it and sends a bid |
| 14 | Trade at the bid: the order has not arrived, no fill |
| 15 | Order reaches the exchange and rests |
| 16 | Trade at the bid: fill |

5. Strategy boundary. The strategy protocol (`quotelab/strategies/base.py`) has `on_book`, `on_trade`, `on_fill` and `on_timer`. It has no access to the exchange, its book or its queue. The only time it sees is the `t_ns` passed to `on_timer`.
6. Calibration uses earlier data only. `calibrate` (`quotelab/strategies/calibration.py`) fits from the day it is given. Within it, `aggressive_order_excursions` compares each trade with the last book strictly before that trade. Walk-forward pairs come from `walk_forward_pairs` (`quotelab/experiments/jobs.py`): an earlier day calibrates and the next day is tested. `run_job` (`quotelab/experiments/worker.py`) reads calibration only from `job.calib_date` and loads events only from `job.test_date`. With the five dates in `config/default.yaml`, this gives four pairs.

Live data follows the same ordering rule. See section 5.

## 3. How fills are modelled

Shared assumptions, stated in the docstring of `quotelab/fills/base.py`:

- No market impact. Our orders do not move the book or the trade tape.
- A passive order fills at its own price, never a better one.

Post-only. `SimExchange.place` rejects a buy at or above the ask, or a sell at or below the bid (`_would_take`). The trader receives a `Rejected` message with reason `post_only`. An order placed before the first book is rejected with reason `no_book`.

Model selection. `fill_model_from_config` (`quotelab/fills/factory.py`) reads `fills.model` from config. The first and third models are the generous and the strict ends of the fill ladder; the queue model is the default. They are not strict bounds on a run's fills: a different fill path changes inventory and so every later quote, and latency leaves quotes at stale prices where they can fill more often than under `optimistic` without latency.

| Config name | Class | Trade rule | Book rule | Role |
|---|---|---|---|---|
| `optimistic` | `OptimisticFill` | Full fill on any trade at our price or through it, from either aggressor | Full fill when the opposite touch reaches our price | Generous end |
| `queue` (default) | `QueueAwareFill` | Queue model, below | Queue model, below | Central model |
| `trade_through` | `TradeThroughFill` | Full fill only on a trade strictly through our price | No fills | Strict end |

Queue model (`QueueAwareFill`, `quotelab/fills/queue_aware.py`). Each resting order tracks `queue_ahead` on its `RestingOrder`.

- On arrival, `on_new_order` sets queue ahead to the displayed size if we join the touch, to 0 if we improve inside the spread, and to `None` if our level is not yet visible. The `None` case is learned later (`test_queue_s4_queue_is_learned_once_our_level_becomes_visible`).
- A trade at our level (`_fills_from_level_trade`) reduces queue ahead by trade size times `participation`. Any excess beyond the queue fills the order, capped at its remaining size. Buy trades at our bid do not count.
- Level size that falls without matching trades (`_queue_after_touch_update`) reduces queue ahead by `alpha` times the unexplained amount, capped at the new level size. So `alpha` is the assumed share of unexplained size reduction that sits ahead of us.
- A market price that crosses our price fills the order in full (`on_book`, `_crosses`). A trade strictly through our price also fills in full.

Worked example, from `test_queue_s1_queue_drains_by_trades_then_cancels_then_fills`: a buy order for 5 at 100, with `alpha` 0.5, joins behind a level of 10.

| Event | Queue ahead after the event |
|---|---|
| Join at the touch behind 10 | 10 |
| Trade of 4 at our level | 6 |
| Level falls from 10 to 6, all explained by that trade | 6 |
| Level falls from 6 to 2 with no trade (4 unexplained, `alpha` 0.5) | 2, which is the lower of 6 minus 0.5 times 4 and the new level size |
| Trade of 5 at our level | 0, and 3 fills (5 minus the 2 ahead, capped by size) |

Passive fill mechanics. `SimExchange._passive_fill` fills at `order.px`. The fee is `fee_for(notional, maker_bps)` (`quotelab/engine/portfolio.py`). `Portfolio.apply_fill` updates position, cash and fees. `SimExchange._apply_passive` clips each fill to the remaining size.

Hedges. `SimExchange.hedge` takes the opposite touch for the full requested size, charges `taker_bps`, and never fills partially. It is recorded with `kind` set to `hedge`. The default risk layer never requests a hedge (see section 4).

Worked example, from `test_hedge_takes_the_touch_once_and_pays_taker_fee` in `tests/test_backtest.py`. A buy of 0.01 BTC fills at 16 ms at 40000.0. The trader sees that fill at 21 ms and sends a sell hedge. The hedge reaches the exchange at 31 ms and takes the bid at 40000.0, paying a taker fee of 0.2 on 400 of notional at 5 bps. The book updates at 25 ms and 30 ms (exchange time) reach the trader at 30 ms and 35 ms. The hedge fill reaches the trader at 36 ms (`Backtest._deliver`), and `Backtest._on_own_fill` clears `hedge_order_id` only then. No second hedge is sent. This test uses `RecordingRisk`, a test-only risk layer, not the default config.

Funding example, from `test_funding_is_charged_on_the_open_position_at_the_mid`: a long position of 0.01 BTC, marked at the exchange mid of 40000.05, with a rate of 0.0001, pays 0.04000005. `Portfolio.funding_payment` uses `position_qty * mark * rate`.

Fill rows. `SimExchange.fill_rows` stores each fill with the mid of the book the exchange was using at that moment. For a book-cross fill, that is the previous book.

## 4. Known approximations

Each row is an assumption in the code. The effect column gives direction only.

| Area | Assumption | Effect |
|---|---|---|
| Latency | One constant per message type (`EngineConfig`). No jitter, no exchange queueing. The latency sweep is `experiments.e4_latency_ms` | Real delays vary, so an order can catch or miss a trade differently |
| Depth | Only the top of book is used. `BookEvent` has one bid and one ask level | Queue position uses touch size only |
| Market impact | None (`quotelab/fills/base.py`) | Fills are not penalised for moving price |
| Price improvement | Passive fills only at our own price (`SimExchange._passive_fill`) | Fill prices are never better than posted |
| Queue parameters | `alpha` 0.5 and `participation` 1.0 are config values, not fitted. `cmd_calibrate` prints sigma, k, A, R2 and ofi_beta only. The stress variant is `experiments.e1_stress` | Fill counts depend directly on these values |
| Touch intensity | Calibration counts a hit when a taker reaches the price, so `A` is an optimistic touch intensity. Real flow has a point mass at the touch (`quotelab/strategies/calibration.py` docstring) | Intensity estimates may be high |
| Hedges | Full size at the touch, no slippage beyond it, no partial fill (`SimExchange.hedge`) | Hedge cost is understated |
| Fees | Flat maker 2.0 bps and taker 5.0 bps (`config/default.yaml`). Not verified against Binance's current fee schedule or any account tier | Cash and equity depend directly on these |
| Funding | Charged at the exchange mid on the open position (`Portfolio.funding_payment`, `SimExchange.on_funding`). If a month's funding file is missing, no funding is charged (`test_load_events_without_a_funding_file_emits_no_funding`) | A missing file drops funding silently |
| Same millisecond | Times are in ms (`parse_message`, `quotelab/live/binance_ws.py`). Events in the same ms keep kind and row order from `events_from_frames`, not true arrival order | Ordering within one ms is an assumption |
| End of run | `Backtest.finish` processes only items due up to the engine clock, which for a backtest is the last market event. Orders, cancels, fills and timers due after it are never processed | Affects only messages in flight at the end of a run |
| Calibration age | Calibration comes from the previous day (`run_job` reads `job.calib_date`) | Parameters do not adapt within a day |
| Event-study window | `most_violent_hour` picks the window from `market_hours`, which `run_experiments` computes on the test days (`quotelab/experiments/runner.py`). The window is the test-day hour with the largest realised variance of the 1-second mid (`market_hours_day`, `hourly_variance`). This is selection on test-day data, which is acceptable for an event study because the window never feeds a strategy parameter | Results for that window depend on the choice |
| Risk layer | Default runs use `NoRisk`. `worker.risk_layer` builds a manager only when a control is switched on or `hedge_threshold_orders` is set, and all config defaults are off | Default backtests have no kill switches, circuit breakers, size ramp or hedges |
| Synthetic data | `quotelab/data/synthetic.py` generates data for tests and the offline demo. `quotelab backtest` defaults to `--dataset synthetic` (`quotelab/cli.py`) | Synthetic results say nothing about real markets |

The formulas behind the strategies, calibration, attribution and markouts are in [MODEL.md](MODEL.md).

## 5. Live paper trading uses the same loop

`quotelab paper` (`cmd_paper` in `quotelab/cli.py`) starts a `PaperSession` from `quotelab/live/paper_runner.py`. The session is read-only. `quotelab/live/binance_ws.py` connects only to the public `bookTicker` and `trade` streams (`stream_url`), and nothing in the package places or cancels orders. `parse_message` converts millisecond times to nanoseconds.

1. Warm-up. `PaperSession._start_engine` calibrates on the first `live.warmup_minutes` (15 by default) with `calibrate` and writes `calibration.json`. It then calls `trading_setup`, which builds the strategy, fill model, engine config and risk layer. `_start_engine` creates the `Backtest` from them. The `trading_setup` docstring says the replay calls it too, so any difference between the two comes from the feed.
2. Messages. `PaperSession.on_message` passes each message to `_push`. If a message is older than the engine clock, `_push` moves its timestamp to the clock with `dataclasses.replace`. `_record` keeps the original row and stores the time actually pushed as `pushed_ts`. Then `Backtest.push` runs the event.
3. Silence. `heartbeat` moves the engine forward with `Backtest.advance_to`, to the newest exchange time plus the local time elapsed since it arrived, minus `live.heartbeat_s` (1 s by default), capped at the session end. The lag keeps the clock behind messages still in flight. Through a silence, timers keep firing, and after `risk.stale_after_s` (5 s) the stale-data kill switch trips. `market_messages` in `quotelab/live/binance_ws.py` reconnects after `live.silence_reconnect_s` (10 s) without a message.
4. Risk. `session_config` in `paper_runner` applies `RISK_ON` (`quotelab/experiments/definitions.py`): kill switches, circuit breakers and size ramp are on. Paper sessions therefore run with risk controls, while default backtests do not. Their results are not directly comparable.
5. Stop. `PaperSession.close` calls `Backtest.finish` and writes `session.json`.
6. Replay. `replay` in `quotelab/live/compare.py` runs the recorded messages through `run_backtest` with the session's config and calibration. `feed_stats` reports volume, ordering, clamping, gaps and delay (`recv_ns - ts`). `same_ts_reversals` counts the reversals.

Latency in live mode. The engine clock follows exchange timestamps. Local arrival delay is recorded (`feed_stats`), but the configured latencies are added to exchange time, not to arrival time. This is the design `quotelab/live/compare.py` documents: the latencies are constant and identical in paper and replay, and `feed_stats` measures lateness against that constant.

## 6. Where the rules are tested

Each rule is pinned by a test in the repository.

| Rule | Test |
|---|---|
| Trader sees market data after data latency | `test_trader_sees_market_data_after_data_latency` (`tests/test_backtest.py`) |
| Order rests only after order latency | `test_order_rests_only_after_order_latency` |
| Own fill reaches the trader after data latency | `test_own_fill_reaches_trader_after_data_latency` |
| Order can fill while its cancel is in flight | `test_order_can_fill_while_its_cancel_is_in_flight` |
| Post-only cross is rejected | `test_post_only_order_that_would_cross_is_rejected` |
| Throttled requote is sent when the throttle expires | `test_throttled_requote_is_sent_when_the_throttle_expires` |
| Maker fill updates cash, fees and equity | `test_maker_fill_updates_cash_fees_and_equity` |
| Hedge takes the touch once and pays the taker fee | `test_hedge_takes_the_touch_once_and_pays_taker_fee` |
| Funding is charged at the mid | `test_funding_is_charged_on_the_open_position_at_the_mid` |
| Timer and sample grid | `test_timer_fires_every_second_and_samples_follow_the_grid` |
| Post-only and no-book rejections | `test_post_only_rejects_buy_at_ask_and_sell_at_bid`, `test_place_before_any_book_is_rejected_as_no_book` (`tests/test_exchange.py`) |
| Cancel reports false once filled | `test_cancel_removes_live_order_and_reports_false_once_filled` (`tests/test_exchange.py`) |
| Loader ordering by ts, then kind, then input row | `test_events_are_ordered_by_ts_then_trade_book_and_input_order` (`tests/test_loader.py`) |
| Funding emitted at its ts and last among equal ts | `test_funding_is_emitted_at_its_ts_and_last_among_events_sharing_a_ts` |
| Window is start-inclusive and end-exclusive | `test_window_keeps_events_in_start_inclusive_end_exclusive_range` |
| Funding file is cut to the requested day | `test_load_events_yields_only_the_requested_days_funding_in_date_order` |
| Missing book file points to `quotelab prepare` | `test_missing_book_file_points_to_quotelab_prepare` |
| Queue model cases, by name prefix | `test_queue_s1_...` to `test_queue_s5_...`, `test_queue_sell_mirror_of_s1_drains_by_buy_trades_then_cancels_then_fills`, `test_participation_scales_the_at_level_volume_that_reaches_the_queue` (`tests/test_fills.py`) |
