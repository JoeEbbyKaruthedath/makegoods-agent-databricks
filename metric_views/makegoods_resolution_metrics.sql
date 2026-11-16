CREATE OR REPLACE VIEW YOUR_CATALOG.YOUR_SCHEMA.makegoods_resolution_metrics
WITH METRICS
LANGUAGE YAML
AS $$
  version: 1.1
  source: YOUR_CATALOG.YOUR_SCHEMA.mg_decision_events
  comment: "Make-goods resolved in the app: booked as a make-good, refunded or escalated, with the value booked against the value of the missed spot. One row per decision. Each person sees their own clients (row filter)."
  dimensions:
    - name: Market
      expr: market
      comment: "Designated market area (DMA) of the station."
      display_name: Market
      synonyms: ["DMA", "city"]
    - name: Seller
      expr: seller_id
      comment: "Seller (account executive) who owns the client account."
      display_name: Seller
      synonyms: ["account executive", "AE", "rep"]
    - name: Outcome
      expr: "CASE outcome WHEN 'make_good' THEN 'Made good' WHEN 'make_good_exception' THEN 'Made good (exception)' WHEN 'refund' THEN 'Refunded' WHEN 'escalated' THEN 'Escalated' ELSE outcome END"
      comment: "How the make-good was resolved."
      display_name: Outcome
      synonyms: ["resolution", "result"]
    - name: Decision Week
      expr: "DATE_TRUNC('WEEK', event_at)"
      comment: "Week the decision was made."
      display_name: Decision Week
  measures:
    - name: Decisions
      expr: COUNT(1)
      comment: "Make-goods resolved."
      display_name: Decisions
    - name: Made Good Value
      expr: SUM(owed_value_usd) FILTER (WHERE outcome IN ('make_good', 'make_good_exception'))
      comment: "Value of the missed spots that were made good instead of refunded (revenue kept)."
      display_name: Made Good Value
      format:
        type: currency
        currency_code: USD
    - name: Refunded Amount
      expr: COALESCE(SUM(refund_usd), 0)
      comment: "Refunds issued, full or for a value gap."
      display_name: Refunded Amount
      format:
        type: currency
        currency_code: USD
    - name: Recovery Rate
      expr: MEASURE(`Made Good Value`) / NULLIF(MEASURE(`Made Good Value`) + MEASURE(`Refunded Amount`), 0)
      comment: "Share of the at-risk value kept by making it good rather than refunding."
      display_name: Recovery Rate
      format:
        type: percentage
    - name: Booked Value
      expr: SUM(booked_value_usd)
      comment: "Rate-card value of the inventory booked as make-goods."
      display_name: Booked Value
      format:
        type: currency
        currency_code: USD
    - name: Value Match
      expr: SUM(booked_value_usd) / NULLIF(SUM(owed_value_usd) FILTER (WHERE outcome IN ('make_good', 'make_good_exception')), 0)
      comment: "Booked make-good value as a share of the missed spots' value (1.0 = dollars match exactly)."
      display_name: Value Match
      format:
        type: percentage
    - name: Median Days to Resolve
      expr: PERCENTILE(days_to_resolve, 0.5)
      comment: "Median days from the missed spot to the decision."
      display_name: Median Days to Resolve
    - name: P90 Days to Resolve
      expr: PERCENTILE(days_to_resolve, 0.9)
      comment: "90th percentile of days from the missed spot to the decision."
      display_name: P90 Days to Resolve
$$;
