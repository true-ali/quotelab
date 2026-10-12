-- Markouts: the mid price after each fill, compared with the fill price.
--
-- Inputs (DuckDB views): fills (FILLS_SCHEMA rows), mids (ts, mid), horizons (h_s, seconds).
-- Parameter: $symbol.
--
-- Each fill is paired with every horizon. mid_after is the last mid at or before
-- target_ts = fill ts + horizon. Targets after the last observed mid are dropped, because
-- the mid for that horizon was never observed.
--
-- Output columns:
--   symbol        instrument name, from the $symbol parameter
--   ts            fill timestamp, int ns
--   order_id      id of the filled order
--   kind          order kind as recorded by the engine
--   side          +1 for a buy, -1 for a sell
--   px            fill price
--   qty           fill quantity
--   mid_at_fill   mid at the moment of the fill
--   horizon_s     look-ahead in seconds
--   mid_after     last mid at or before ts + horizon_s
--   markout_usd   side * (mid_after - px) * qty, mark-to-mid PnL of the fill
--   markout_bps   side * (mid_after - px) / px * 10000
--   adverse_bps   side * (mid_after - mid_at_fill) / px * 10000
--
-- A positive markout means the fill was profitable against the later mid. markout_bps
-- includes the half-spread captured at the fill. adverse_bps removes it and keeps only how
-- the mid moved after the fill, so its average is the adverse-selection measure.

WITH last_mid AS (
    SELECT max(ts) AS ts
    FROM mids
),
targets AS (
    SELECT
        f.ts,
        f.order_id,
        f.kind,
        f.side,
        f.px,
        f.qty,
        f.mid AS mid_at_fill,
        h.h_s AS horizon_s,
        f.ts + h.h_s * 1000000000 AS target_ts
    FROM fills AS f
    CROSS JOIN horizons AS h
)
SELECT
    $symbol AS symbol,
    t.ts,
    t.order_id,
    t.kind,
    t.side,
    t.px,
    t.qty,
    t.mid_at_fill,
    t.horizon_s,
    m.mid AS mid_after,
    t.side * (m.mid - t.px) * t.qty AS markout_usd,
    t.side * (m.mid - t.px) / t.px * 10000 AS markout_bps,
    t.side * (m.mid - t.mid_at_fill) / t.px * 10000 AS adverse_bps
FROM targets AS t
ASOF LEFT JOIN mids AS m
    ON t.target_ts >= m.ts
WHERE t.target_ts <= (SELECT ts FROM last_mid)
ORDER BY t.ts, t.order_id, t.horizon_s
