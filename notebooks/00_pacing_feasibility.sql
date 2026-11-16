-- Databricks notebook source
-- MAGIC %md
-- MAGIC # Pacing data feasibility (Phase 0 deliverable: go/no-go for Phase 2 forecasting)
-- MAGIC
-- MAGIC Phase 2 (buyer-side shortfall forecasting) depends on delivery data that is **granular** and **timely**.
-- MAGIC This notebook profiles `bronze_delivery_pacing` / `silver_delivery_metrics` on four questions, then runs an
-- MAGIC `ai_forecast` smoke test. On real delivery feeds, re-run it and fill in `docs/pacing_feasibility.md`.
-- MAGIC
-- MAGIC | Question | Go threshold |
-- MAGIC |---|---|
-- MAGIC | Grain: spot- or hour-level delivery? | spot/hour |
-- MAGIC | Latency: hours from airing to first measurement | p90 < 24h for daily forecasting; < 3h for intraday |
-- MAGIC | Coverage: share of active guaranteed orders with delivery data | > 90% |
-- MAGIC | Restatement: preliminary vs final C3 difference | < 10% |

-- COMMAND ----------

USE CATALOG YOUR_CATALOG;
USE SCHEMA YOUR_SCHEMA;

-- COMMAND ----------

-- MAGIC %md ### 1. Grain and sources

-- COMMAND ----------

SELECT source_system, measurement_stage, count(*) AS rows, count(DISTINCT spot_id) AS spots,
       count(DISTINCT order_id) AS orders, count(DISTINCT date_trunc('HOUR', event_hour)) AS distinct_hours
FROM bronze_delivery_pacing
GROUP BY ALL ORDER BY rows DESC;

-- COMMAND ----------

-- MAGIC %md ### 2. Latency from airing to first report, by measurement source

-- COMMAND ----------

WITH first_report AS (
  SELECT spot_id, station_id, min_by(source_system, reported_ts) AS first_source,
         (unix_timestamp(min(reported_ts)) - unix_timestamp(min(event_hour))) / 3600 AS latency_hours
  FROM bronze_delivery_pacing WHERE order_id IS NOT NULL GROUP BY spot_id, station_id
)
SELECT first_source,
       count(DISTINCT station_id) AS stations,
       count(*) AS spots,
       round(percentile(latency_hours, 0.5), 1) AS p50_hours,
       round(percentile(latency_hours, 0.9), 1) AS p90_hours,
       round(avg(CASE WHEN latency_hours <= 3 THEN 1 ELSE 0 END), 3) AS share_within_3h,
       round(avg(CASE WHEN latency_hours <= 24 THEN 1 ELSE 0 END), 3) AS share_within_24h
FROM first_report GROUP BY first_source;

-- COMMAND ----------

-- MAGIC %md ### 3. Coverage of active guaranteed orders (last 7 days before the as-of date)

-- COMMAND ----------

WITH cfg AS (SELECT CAST(str_value AS DATE) AS as_of FROM mg_rules_config WHERE rule_key = 'as_of_date'),
active AS (
  SELECT c.order_id FROM silver_contract_obligations c CROSS JOIN cfg
  WHERE c.flight_start <= cfg.as_of AND c.flight_end >= date_sub(cfg.as_of, 7)
),
measured AS (
  SELECT DISTINCT d.order_id FROM silver_delivery_metrics d CROSS JOIN cfg
  WHERE d.delivery_date BETWEEN date_sub(cfg.as_of, 7) AND cfg.as_of
)
SELECT count(*) AS active_orders, count(m.order_id) AS orders_with_delivery,
       round(count(m.order_id) / count(*), 3) AS coverage
FROM active a LEFT JOIN measured m ON m.order_id = a.order_id;

-- COMMAND ----------

-- MAGIC %md ### 4. Restatement: how much do final C3 numbers differ from the first report?

-- COMMAND ----------

WITH s AS (
  SELECT spot_id,
         max_by(impressions, CASE WHEN measurement_stage = 'final_c3' THEN 0 ELSE 1 END) AS first_imps,
         max(CASE WHEN measurement_stage = 'final_c3' THEN impressions END) AS final_imps
  FROM bronze_delivery_pacing WHERE order_id IS NOT NULL GROUP BY spot_id
)
SELECT count(*) AS spots_with_final, round(avg(final_imps / first_imps - 1), 4) AS avg_restatement,
       round(percentile(abs(final_imps / first_imps - 1), 0.9), 4) AS p90_abs_restatement
FROM s WHERE final_imps IS NOT NULL AND first_imps > 0 AND first_imps <> final_imps;

-- COMMAND ----------

-- MAGIC %md ### 5. `ai_forecast` smoke test: forecast delivery to flight end for 20 guaranteed orders and flag shortfalls
-- MAGIC Train on days with complete measurement (two days before the as-of date). Note that `horizon` takes an end date,
-- MAGIC and the output column is `<value_col>_forecast`.

-- COMMAND ----------

WITH orders AS (
  SELECT order_id, contracted_impressions, delivered_impressions, flight_end
  FROM gold_impression_shortfall
  WHERE flight_start <= DATE'2026-10-05' AND flight_end BETWEEN DATE'2026-11-05' AND DATE'2026-11-20'
  ORDER BY order_id LIMIT 20
),
fc AS (
  SELECT * FROM ai_forecast(
    TABLE(SELECT d.order_id, CAST(d.delivery_date AS TIMESTAMP) AS ds, CAST(d.impressions AS DOUBLE) AS y
          FROM silver_delivery_metrics d JOIN orders o USING (order_id) WHERE d.delivery_date <= DATE'2026-10-28'),
    horizon => '2026-11-21', time_col => 'ds', value_col => 'y', group_col => 'order_id')
)
SELECT o.order_id, o.contracted_impressions,
       o.delivered_impressions + CAST(sum(CASE WHEN fc.ds > TIMESTAMP'2026-10-30' AND fc.ds <= o.flight_end THEN fc.y_forecast ELSE 0 END) AS BIGINT) AS forecast_at_flight_end,
       round((o.delivered_impressions + sum(CASE WHEN fc.ds > TIMESTAMP'2026-10-30' AND fc.ds <= o.flight_end THEN fc.y_forecast ELSE 0 END)) / o.contracted_impressions, 3) AS forecast_delivery_pct,
       (o.delivered_impressions + sum(CASE WHEN fc.ds > TIMESTAMP'2026-10-30' AND fc.ds <= o.flight_end THEN fc.y_forecast ELSE 0 END)) < 0.95 * o.contracted_impressions AS shortfall_flag
FROM orders o JOIN fc ON fc.order_id = o.order_id
GROUP BY o.order_id, o.contracted_impressions, o.delivered_impressions, o.flight_end
ORDER BY forecast_delivery_pct;
