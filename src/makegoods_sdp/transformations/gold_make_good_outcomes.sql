CREATE OR REFRESH MATERIALIZED VIEW gold_make_good_outcomes
COMMENT 'Make-good offer history (gold_make_good_history) with the seller who owns each account, for the app, analytics and business results. Each person sees only the clients they may see (row filter mg_row_visible).'
WITH ROW FILTER ${mg_catalog}.${mg_schema}_security.mg_row_visible ON (seller_id, market)
AS SELECT h.*, c.seller_id
FROM gold_make_good_history h
LEFT JOIN silver_contract_obligations c ON c.order_id = h.order_id;
