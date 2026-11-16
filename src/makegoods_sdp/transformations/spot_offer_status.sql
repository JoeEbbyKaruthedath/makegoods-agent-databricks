-- Pipeline-private helper: make-good resolution status per preempted/missed spot.
CREATE TEMPORARY VIEW spot_offer_status AS
SELECT spot_id,
  count(*) AS n_offers,
  max_by(offer_id, offered_ts) AS latest_offer_id,
  max_by(offer_status, offered_ts) AS latest_offer_status,
  max(CASE WHEN offer_status = 'Accepted' THEN 1 ELSE 0 END) = 1 AS made_good,
  max(CASE WHEN refund_issued THEN 1 ELSE 0 END) = 1 AS refunded,
  sum(refund_amount_usd) AS refund_amount_usd
FROM silver_make_good_offers
GROUP BY spot_id;
