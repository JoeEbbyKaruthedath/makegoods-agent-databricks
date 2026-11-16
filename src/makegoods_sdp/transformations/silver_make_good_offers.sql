CREATE OR REFRESH MATERIALIZED VIEW silver_make_good_offers (
  CONSTRAINT valid_offer EXPECT (offer_id IS NOT NULL AND spot_id IS NOT NULL) ON VIOLATION DROP ROW
)
COMMENT 'Make-good offer history, deduplicated (latest record per offer).'
AS SELECT * EXCEPT (_source_file, _ingest_ts)
FROM bronze_make_good_offers
QUALIFY ROW_NUMBER() OVER (PARTITION BY offer_id ORDER BY _ingest_ts DESC) = 1;
