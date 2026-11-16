CREATE OR REPLACE VIEW YOUR_CATALOG.YOUR_SCHEMA.makegoods_open_queue_metrics
WITH METRICS
LANGUAGE YAML
AS $$
  version: 1.1
  source: YOUR_CATALOG.YOUR_SCHEMA.gold_make_good_candidates
  comment: "Open make-good work queue: preempted or missed spots on make-good-eligible contracts that have no accepted make-good or refund yet and whose make-good deadline has not passed (as of the simulation date, 2026-10-30). Revenue at risk is refunded if the spot is not made good."
  dimensions:
    - name: Spot
      expr: spot_id
      comment: "Preempted or missed spot id, e.g. SPT-0058222. Use with Revenue at Risk and Days to Deadline to list individual spots."
      display_name: Spot
      synonyms: ["spot id", "make-good"]
    - name: Order
      expr: order_id
      comment: "Contract / order id the spot belongs to."
      display_name: Order
      synonyms: ["contract"]
    - name: Market
      expr: market
      comment: "Designated market area (DMA) of the station, e.g. Nashville."
      display_name: Market
      synonyms: ["DMA", "city"]
    - name: Station
      expr: call_letters
      comment: "Station call letters, e.g. KPBT."
      display_name: Station
      synonyms: ["call letters", "affiliate"]
    - name: Network
      expr: network_affiliation
      comment: "Network the station is affiliated with (FOX, ABC, CBS, NBC, CW)."
      display_name: Network
      synonyms: ["affiliation"]
    - name: Advertiser
      expr: advertiser_name
      comment: "Advertiser whose spot was preempted or missed."
      display_name: Advertiser
      synonyms: ["client", "customer"]
    - name: Advertiser Category
      expr: advertiser_category
      comment: "Advertiser business category, e.g. Automotive, Legal Services."
      display_name: Advertiser Category
      synonyms: ["category", "vertical"]
    - name: Daypart
      expr: daypart
      comment: "Daypart of the preempted spot: Early Morning, Daytime, Early Fringe, Prime Access, Prime, Late Fringe."
      display_name: Daypart
    - name: Preemption Reason
      expr: preemption_reason
      comment: "Why the spot did not air as scheduled, e.g. Sports Overrun, Political Priority, Breaking News, Technical Failure."
      display_name: Preemption Reason
      synonyms: ["reason", "cause", "why bumped"]
    - name: Audience Segment
      expr: segment_code
      comment: "Contracted audience segment, e.g. A25-54."
      display_name: Audience Segment
      synonyms: ["demo", "target audience"]
    - name: Queue Status
      expr: queue_status
      comment: "'Needs make-good' (no offer yet) or 'Offer pending' (an offer awaits the advertiser)."
      display_name: Queue Status
    - name: Preempted Date
      expr: broadcast_date
      comment: "Broadcast date the spot was scheduled to air."
      display_name: Preempted Date
      synonyms: ["air date", "spot date"]
    - name: Make-good Deadline
      expr: makegood_deadline
      comment: "Last date a make-good can air (the earlier of the make-good window end and the flight end)."
      display_name: Make-good Deadline
      synonyms: ["deadline", "due date"]
    - name: Days to Deadline
      expr: days_to_deadline
      comment: "Days from the as-of date (2026-10-30) to the make-good deadline."
      display_name: Days to Deadline
      synonyms: ["days left"]
    - name: Deadline Bucket
      expr: "CASE WHEN days_to_deadline <= 3 THEN 'Due within 3 days' WHEN days_to_deadline <= 7 THEN 'Due within a week' ELSE 'Later' END"
      comment: "Urgency bucket by days to deadline: Due within 3 days, Due within a week, Later."
      display_name: Deadline Bucket
      synonyms: ["urgency", "urgent"]
  measures:
    - name: Open Spots
      expr: COUNT(1)
      comment: "Number of preempted or missed spots still needing a make-good."
      display_name: Open Spots
      synonyms: ["open make-goods", "spots needing a make-good", "queue size"]
    - name: Revenue at Risk
      expr: SUM(revenue_at_risk_usd)
      comment: "Unit rate of the open spots: revenue refunded if they are not made good before their deadline."
      display_name: Revenue at Risk
      format:
        type: currency
        currency_code: USD
        decimal_places:
          type: exact
          places: 0
      synonyms: ["at risk", "exposure", "dollars at risk"]
    - name: Spots Due Within 3 Days
      expr: COUNT(1) FILTER (WHERE days_to_deadline <= 3)
      comment: "Open spots whose make-good deadline is within 3 days of the as-of date."
      display_name: Spots Due Within 3 Days
      synonyms: ["urgent spots"]
    - name: Revenue Due Within 3 Days
      expr: SUM(revenue_at_risk_usd) FILTER (WHERE days_to_deadline <= 3)
      comment: "Revenue at risk on spots due within 3 days."
      display_name: Revenue Due Within 3 Days
      format:
        type: currency
        currency_code: USD
        decimal_places:
          type: exact
          places: 0
    - name: Owed Impressions
      expr: SUM(owed_impressions_per_spot)
      comment: "Impressions owed in the contracted audience segment for the open spots."
      display_name: Owed Impressions
$$
