# Pacing Data Feasibility — Go/No-Go for Phase 2 Forecasting

Source: `notebooks/00_pacing_feasibility.sql`, run on the synthetic reference feeds (as of 2026-10-30).
**Re-run it on the real delivery feeds and replace the "Synthetic result" column.**

| Question | Go threshold | Synthetic result | Verdict |
|---|---|---|---|
| Grain | spot or hour level | Spot-level rows at hourly grain (95.8K measurements, 3 stages) | ✅ Go |
| Latency, ACR stations (25 of 40) | p90 < 3h for intraday | p50 2.0h, p90 2.8h, 100% within 3h | ✅ Go (intraday) |
| Latency, Nielsen-only stations | p90 < 24h for daily | p50 14.1h, p90 25.3h, 87% within 24h | ⚠️ Go for daily forecasts with a T-2 cutoff |
| Coverage of active guaranteed orders (last 7 days) | > 90% | 98.5% (1,713 of 1,739) | ✅ Go |
| Restatement, first report → final C3 | < 10% | avg +3.1%, p90 8.5% | ✅ Go (forecast on final when available) |

## `ai_forecast` smoke test (20 guaranteed orders)
- **What ran:** each order's daily delivery was forecast to flight end with `ai_forecast`, training on fully measured days only (through Oct 28).
- **Result:** 4 of 20 orders are forecast to land below 95% of contracted impressions (ORD-00075 at 67%, ORD-00078 at 84%, ORD-00179 at 87%, ORD-00029 at 94%). These are exactly the orders Phase 2 would hand to the seller-side agent for proactive sourcing.
- **Syntax fixes vs the PDF:**
  - `horizon` takes an **end date** (`'2026-11-21'`), not `'7 days'`.
  - The output column is `<value_col>_forecast` (e.g. `y_forecast`), with `y_lower` / `y_upper`.

## Recommendation
1. **Go for Phase 2 with daily batch forecasting** on every station, using the best available measurement (final C3, else preliminary or overnight) and a T-2 training cutoff.
2. **Add intraday re-forecasting** only for the ACR-measured stations, where data arrives within 3 hours.
3. **Confirm during discovery:**
   - which stations have set-top-box / ACR measurement;
   - whether restatements arrive as new rows (as modeled here) or as updates.
