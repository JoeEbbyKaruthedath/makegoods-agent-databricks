-- Who sees what (run BEFORE the pipeline: its serving tables are created WITH ROW FILTER mg_row_visible).
--
-- Unity Catalog enforces it for every query the app, the agent's rule functions or the analytics run as the
-- signed-in person:
--   seller   sees the clients they own (seller book), in any market
--   manager  sees every client in the markets they manage
--   admin    sees everything
-- An admin can "view as" a seller or manager for a short time (view_as_until) to see exactly what they see; the
-- granted role is unchanged and comes back on its own. A principal with no row sees nothing, including the app's
-- own service principal, so a query that loses the person's identity returns no rows instead of everyone's.
--
-- The access map lives in its own schema so that only admins can change it. Seed it after creation (see
-- uc_functions/91_grants_v2.sql) with run_sql.py --param admin_user=<you>.
USE CATALOG YOUR_CATALOG;
CREATE SCHEMA IF NOT EXISTS YOUR_SCHEMA_security
  COMMENT 'Make-goods access control: who can see which clients and markets. Only admins can modify it.';
USE SCHEMA YOUR_SCHEMA_security;

CREATE TABLE IF NOT EXISTS mg_user_access (
  principal STRING NOT NULL COMMENT 'User email or service principal application id, as session_user() returns it',
  display_name STRING,
  role STRING NOT NULL COMMENT 'seller | manager | admin',
  seller_id STRING COMMENT 'For sellers: their id in ref_sellers (owns clients in ref_seller_book)',
  markets ARRAY<STRING> COMMENT 'For managers: the markets they manage',
  view_as_role STRING COMMENT 'Admin preview: role to see the app as, until view_as_until',
  view_as_seller_id STRING,
  view_as_markets ARRAY<STRING>,
  view_as_until TIMESTAMP,
  updated_by STRING,
  updated_at TIMESTAMP
)
COMMENT 'Access map for the make-goods app and agent. One row per person. The row filter mg_row_visible reads it on every query.';

CREATE TABLE IF NOT EXISTS mg_access_audit (
  changed_at TIMESTAMP NOT NULL,
  changed_by STRING NOT NULL,
  principal STRING NOT NULL,
  action STRING NOT NULL COMMENT 'grant | revoke | view_as | view_as_end',
  details STRING COMMENT 'JSON: role, seller_id, markets, expiry'
)
COMMENT 'Every change to the access map: who changed whose access, when, and to what.';

-- The one row filter. Filters run with the definer's rights except for session_user(), which returns the
-- person running the query, also when the filtered table is read inside a rule function or a view.
CREATE OR REPLACE FUNCTION mg_row_visible(p_seller_id STRING, p_market STRING)
RETURNS BOOLEAN
COMMENT 'Row filter for make-goods data: TRUE when the person running the query may see a row owned by p_seller_id in p_market (admin: all; manager: their markets; seller: their own clients).'
RETURN EXISTS (
  SELECT 1 FROM (
    SELECT IF(view_as_until > current_timestamp(), view_as_role, role) AS r,
           IF(view_as_until > current_timestamp(), view_as_seller_id, seller_id) AS s,
           IF(view_as_until > current_timestamp(), view_as_markets, markets) AS m
    FROM YOUR_CATALOG.YOUR_SCHEMA_security.mg_user_access
    WHERE principal = session_user()
  ) u
  WHERE u.r = 'admin' OR (u.r = 'manager' AND array_contains(u.m, p_market)) OR (u.r = 'seller' AND u.s = p_seller_id)
);

-- What the app shows as "who am I". Views read the access map with the owner's rights, so people need SELECT on
-- this view only, not on the map.
CREATE OR REPLACE VIEW mg_my_access
COMMENT 'The signed-in person’s make-goods access: effective role, seller id and markets (after any admin preview), and the role granted to them.'
AS SELECT principal, display_name,
  IF(view_as_until > current_timestamp(), view_as_role, role) AS role,
  IF(view_as_until > current_timestamp(), view_as_seller_id, seller_id) AS seller_id,
  IF(view_as_until > current_timestamp(), view_as_markets, markets) AS markets,
  role AS granted_role,
  CASE WHEN view_as_until > current_timestamp() THEN view_as_until END AS view_as_until
FROM YOUR_CATALOG.YOUR_SCHEMA_security.mg_user_access
WHERE principal = session_user();
