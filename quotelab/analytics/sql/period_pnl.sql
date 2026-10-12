-- PnL per fixed-length period, from a sampled equity timeseries.
--
-- Input (DuckDB view): timeseries, TIMESERIES_SCHEMA rows.
-- Parameter: $period_ns, the period length in nanoseconds.
--
-- Each sample belongs to period = ts // $period_ns. The last sample of each period gives
-- equity_end for that period.
--
-- Output columns:
--   period_start  start of the period, int ns (period * $period_ns)
--   equity_end    equity at the last sample of the period
--   pnl           equity_end minus the previous period's equity_end
--
-- The first period's pnl is measured from zero because every run starts flat with zero equity.

WITH bucketed AS (
    SELECT
        ts,
        equity,
        ts // $period_ns AS period
    FROM timeseries
),
period_end AS (
    SELECT
        period,
        equity AS equity_end
    FROM bucketed
    QUALIFY row_number() OVER (PARTITION BY period ORDER BY ts DESC) = 1
)
SELECT
    CAST(period * $period_ns AS BIGINT) AS period_start,
    equity_end,
    equity_end - lag(equity_end, 1, 0.0) OVER (ORDER BY period) AS pnl
FROM period_end
ORDER BY period_start
