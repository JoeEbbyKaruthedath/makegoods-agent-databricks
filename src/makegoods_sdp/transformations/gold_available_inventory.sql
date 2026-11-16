CREATE OR REFRESH MATERIALIZED VIEW gold_available_inventory
CLUSTER BY (station_id, air_date)
COMMENT 'Every avail with its booking status: Open/Held from traffic, plus booked_for_spot_id when an accepted make-good already uses it. The agent''s inventory_search evaluates the commensurate rules over this table.'
AS WITH booked AS (
  SELECT offered_avail_id, spot_id, offer_id
  FROM silver_make_good_offers WHERE offer_status = 'Accepted'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY offered_avail_id ORDER BY decision_ts) = 1
)
SELECT i.*,
  b.spot_id AS booked_for_spot_id,
  b.offer_id AS booked_by_offer_id,
  i.avail_status = 'Open' AND b.spot_id IS NULL AS is_bookable
FROM silver_inventory_enriched i
LEFT JOIN booked b ON b.offered_avail_id = i.avail_id;
