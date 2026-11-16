-- Make-good offers produced by the inherited system (rules engine, GPT assist, manual).
CREATE OR REFRESH STREAMING TABLE bronze_make_good_offers
COMMENT 'Raw make-good offer history from the inherited system: offered avail, outcome, refund, matching method and GPT token usage. Basis for the evaluation dataset and the cost baseline.'
AS SELECT *, _metadata.file_path AS _source_file, current_timestamp() AS _ingest_ts
FROM STREAM read_files(
  '${mg_source_root}/make_good_offers/',
  format => 'json',
  schema => 'offer_id STRING, spot_id STRING, order_id STRING, station_id STRING, original_daypart STRING, offered_avail_id STRING, offered_daypart STRING, offered_air_date DATE, offered_program STRING, offered_ts TIMESTAMP, offer_status STRING, decision_ts TIMESTAMP, refund_issued BOOLEAN, refund_amount_usd DOUBLE, matched_by STRING, reviewer STRING, gpt_prompt_tokens BIGINT, gpt_completion_tokens BIGINT'
);
