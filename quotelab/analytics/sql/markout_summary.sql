-- Markout summary per symbol, order kind, side and horizon.
--
-- Input (DuckDB view): markouts, the output of markouts.sql.
--
-- Output columns:
--   symbol             grouping key
--   kind               grouping key, order kind
--   side               grouping key, +1 buy or -1 sell
--   horizon_s          grouping key, look-ahead in seconds
--   n_fills            number of markout rows in the group
--   avg_markout_bps    mean markout_bps, includes the captured half-spread
--   avg_adverse_bps    mean adverse_bps, the mid move after the fill (adverse selection)
--   se_markout_bps     standard error of avg_markout_bps, sample stddev / sqrt(n_fills),
--                      NULL when n_fills < 2
--   total_markout_usd  sum of markout_usd over the group

SELECT
    symbol,
    kind,
    side,
    horizon_s,
    count(*) AS n_fills,
    avg(markout_bps) AS avg_markout_bps,
    avg(adverse_bps) AS avg_adverse_bps,
    CASE
        WHEN count(*) >= 2 THEN stddev_samp(markout_bps) / sqrt(count(*))
    END AS se_markout_bps,
    sum(markout_usd) AS total_markout_usd
FROM markouts
GROUP BY symbol, kind, side, horizon_s
ORDER BY symbol, kind, side, horizon_s
