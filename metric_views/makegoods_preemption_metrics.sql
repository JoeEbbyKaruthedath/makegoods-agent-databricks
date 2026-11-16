CREATE OR REPLACE VIEW YOUR_CATALOG.YOUR_SCHEMA.makegoods_preemption_metrics
WITH METRICS
LANGUAGE YAML
AS $$
  version: 1.1
  source: YOUR_CATALOG.YOUR_SCHEMA.gold_spot_access
  comment: "Every scheduled spot from the as-run log (one row per spot) with whether it aired, was preempted, or was missed, and why. Use for preemption trends by day, market, reason, daypart and program genre."
  joins:
    - name: dim_station
      source: YOUR_CATALOG.YOUR_SCHEMA.ref_stations
      'on': source.station_id = dim_station.station_id
  dimensions:
    - name: Broadcast Date
      expr: broadcast_date
      comment: "Broadcast date the spot was scheduled (5am day boundary)."
      display_name: Broadcast Date
      synonyms: ["date", "air date"]
    - name: Broadcast Week
      expr: "DATE_TRUNC('WEEK', broadcast_date)"
      comment: "Week (Monday start) of the broadcast date."
      display_name: Broadcast Week
      synonyms: ["week"]
    - name: Market
      expr: dim_station.market
      comment: "Designated market area (DMA) of the station."
      display_name: Market
      synonyms: ["DMA", "city"]
    - name: Station
      expr: dim_station.call_letters
      comment: "Station call letters."
      display_name: Station
      synonyms: ["call letters"]
    - name: Network
      expr: dim_station.network_affiliation
      comment: "Network affiliation of the station."
      display_name: Network
    - name: Daypart
      expr: daypart
      comment: "Daypart of the scheduled spot."
      display_name: Daypart
    - name: Program Genre
      expr: program_genre
      comment: "Genre of the scheduled program: News, Sports, Entertainment, Syndicated, Talk/Lifestyle."
      display_name: Program Genre
      synonyms: ["genre"]
    - name: Spot Status
      expr: status
      comment: "Aired, Preempted (bumped by other programming) or Missed (technical or traffic error)."
      display_name: Spot Status
    - name: Preemption Reason
      expr: "COALESCE(preemption_reason, 'Aired as scheduled')"
      comment: "Why the spot did not air (Sports Overrun, Political Priority, Breaking News, Technical Failure, Network Override, Traffic Error); 'Aired as scheduled' otherwise."
      display_name: Preemption Reason
      synonyms: ["reason", "cause"]
  measures:
    - name: Scheduled Spots
      expr: COUNT(1)
      comment: "All scheduled spots."
      display_name: Scheduled Spots
    - name: Preempted or Missed Spots
      expr: COUNT(1) FILTER (WHERE status <> 'Aired')
      comment: "Spots that did not air as scheduled (preempted or missed)."
      display_name: Preempted or Missed Spots
      synonyms: ["preemptions", "bumped spots", "missed spots"]
    - name: Preemption Rate
      expr: MEASURE(`Preempted or Missed Spots`) / MEASURE(`Scheduled Spots`)
      comment: "Share of scheduled spots that were preempted or missed."
      display_name: Preemption Rate
      format:
        type: percentage
        decimal_places:
          type: exact
          places: 1
    - name: Preempted Revenue
      expr: SUM(unit_rate_usd) FILTER (WHERE status <> 'Aired')
      comment: "Unit-rate value of the spots that did not air as scheduled."
      display_name: Preempted Revenue
      format:
        type: currency
        currency_code: USD
        decimal_places:
          type: exact
          places: 0
$$
