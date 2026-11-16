CREATE OR REPLACE VIEW YOUR_CATALOG.YOUR_SCHEMA.makegoods_contract_delivery_metrics
WITH METRICS
LANGUAGE YAML
AS $$
  version: 1.1
  source: YOUR_CATALOG.YOUR_SCHEMA.gold_impression_shortfall
  comment: "Guaranteed-audience contracts (one row per order): contracted vs expected-to-date vs delivered impressions, pacing, and preempted spots that were made good, refunded or are still unresolved. As of 2026-10-30."
  dimensions:
    - name: Order
      expr: order_id
      comment: "Contract / order id, e.g. ORD-00075."
      display_name: Order
      synonyms: ["contract", "order id"]
    - name: Campaign
      expr: campaign_name
      comment: "Campaign name on the order."
      display_name: Campaign
    - name: Advertiser
      expr: advertiser_name
      comment: "Advertiser on the contract."
      display_name: Advertiser
      synonyms: ["client"]
    - name: Market
      expr: market
      comment: "Designated market area (DMA) of the station."
      display_name: Market
      synonyms: ["DMA", "city"]
    - name: Daypart
      expr: daypart
      comment: "Contracted daypart."
      display_name: Daypart
    - name: Audience Segment
      expr: segment_code
      comment: "Contracted audience segment, e.g. A25-54."
      display_name: Audience Segment
      synonyms: ["demo"]
    - name: Flight End
      expr: flight_end
      comment: "Last date of the contract flight."
      display_name: Flight End
    - name: Pacing Status
      expr: "CASE WHEN pacing_index IS NULL THEN 'Not started' WHEN pacing_index < 0.9 THEN 'Under-delivering' WHEN pacing_index <= 1.1 THEN 'On pace' ELSE 'Over-delivering' END"
      comment: "Delivered vs expected-to-date impressions: Under-delivering (< 90%), On pace (90-110%), Over-delivering (> 110%)."
      display_name: Pacing Status
      synonyms: ["pacing", "behind", "under-pacing"]
    - name: Make-good Eligible
      expr: "CASE WHEN makegood_eligible THEN 'Eligible' ELSE 'Refund only' END"
      comment: "Whether the contract allows make-goods (otherwise preemptions are refunded)."
      display_name: Make-good Eligible
  measures:
    - name: Contracts
      expr: COUNT(1)
      comment: "Number of guaranteed contracts."
      display_name: Contracts
      synonyms: ["orders"]
    - name: Contracted Impressions
      expr: SUM(contracted_impressions)
      comment: "Impressions guaranteed over the full flight."
      display_name: Contracted Impressions
    - name: Expected Impressions to Date
      expr: SUM(expected_to_date_impressions)
      comment: "Contracted impressions pro-rated to the share of the flight elapsed."
      display_name: Expected Impressions to Date
    - name: Delivered Impressions
      expr: SUM(delivered_impressions)
      comment: "Impressions delivered so far (best available measurement)."
      display_name: Delivered Impressions
    - name: Pacing Index
      expr: MEASURE(`Delivered Impressions`) / MEASURE(`Expected Impressions to Date`)
      comment: "Delivered divided by expected-to-date impressions; 1.0 = on pace."
      display_name: Pacing Index
      synonyms: ["delivery pace"]
    - name: Impression Shortfall to Date
      expr: SUM(shortfall_to_date_impressions)
      comment: "Impressions behind the pro-rated contract."
      display_name: Impression Shortfall to Date
      synonyms: ["shortfall", "under-delivery"]
    - name: Unresolved Preempted Spots
      expr: SUM(unresolved_spots)
      comment: "Preempted or missed spots with no accepted make-good and no refund (includes spots past their deadline)."
      display_name: Unresolved Preempted Spots
    - name: Unresolved Preemption Value
      expr: SUM(revenue_at_risk_usd)
      comment: "Unit-rate value of unresolved preempted spots, including those past their make-good deadline. For recoverable revenue at risk in the open work queue, use makegoods_open_queue_metrics."
      display_name: Unresolved Preemption Value
      format:
        type: currency
        currency_code: USD
        decimal_places:
          type: exact
          places: 0
$$
