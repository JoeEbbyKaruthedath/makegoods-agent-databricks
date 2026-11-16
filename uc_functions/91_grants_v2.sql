-- v2 access: who may use the make-goods app and what they may read and run (run AFTER 02, the pipeline, 10, 15, 50).
-- People only get the serving objects; row filters (mg_row_visible) decide which rows they see. The app's own
-- service principal can run the rule functions and write the decision and rule records, but has no row in the access
-- map, so anything it reads on its own sees nothing.
--
--   python tools/run_sql.py uc_functions/91_grants_v2.sql --catalog <catalog> --schema makegoods_v2 \
--     --param admin_user=<you@company.com> --param admin_name="<Your Name>" --param app_sp=<app service principal id> \
--     --param users=<group or user who may use the app>
USE CATALOG YOUR_CATALOG;

-- the people who use the app (a group, or one user per run)
GRANT USE CATALOG ON CATALOG YOUR_CATALOG TO `{{users}}`;
GRANT USE SCHEMA ON SCHEMA YOUR_CATALOG.YOUR_SCHEMA TO `{{users}}`;
GRANT USE SCHEMA ON SCHEMA YOUR_CATALOG.YOUR_SCHEMA_security TO `{{users}}`;
GRANT SELECT ON VIEW YOUR_CATALOG.YOUR_SCHEMA_security.mg_my_access TO `{{users}}`;
-- the rule functions and the agent model
GRANT EXECUTE ON SCHEMA YOUR_CATALOG.YOUR_SCHEMA TO `{{users}}`;
-- serving objects only; the row filter limits each person to their own clients / markets
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.gold_make_good_candidates TO `{{users}}`;
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.gold_impression_shortfall TO `{{users}}`;
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.gold_spot_access TO `{{users}}`;
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.gold_make_good_outcomes TO `{{users}}`;
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.mg_decision_events TO `{{users}}`;
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.mg_advertiser_rules TO `{{users}}`;
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.mg_proposals TO `{{users}}`;
GRANT SELECT ON VIEW YOUR_CATALOG.YOUR_SCHEMA.mg_client_emails_mine TO `{{users}}`;
-- station inventory, reference data, rule settings and the governed metric views (no client-level data)
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.gold_available_inventory TO `{{users}}`;
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.ref_stations TO `{{users}}`;
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.ref_advertisers TO `{{users}}`;
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.ref_sellers TO `{{users}}`;
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.mg_rules_config TO `{{users}}`;
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.gold_advertiser_preferences TO `{{users}}`;
GRANT SELECT ON VIEW YOUR_CATALOG.YOUR_SCHEMA.makegoods_open_queue_metrics TO `{{users}}`;
GRANT SELECT ON VIEW YOUR_CATALOG.YOUR_SCHEMA.makegoods_offer_outcome_metrics TO `{{users}}`;
GRANT SELECT ON VIEW YOUR_CATALOG.YOUR_SCHEMA.makegoods_contract_delivery_metrics TO `{{users}}`;
GRANT SELECT ON VIEW YOUR_CATALOG.YOUR_SCHEMA.makegoods_preemption_metrics TO `{{users}}`;
GRANT SELECT ON VIEW YOUR_CATALOG.YOUR_SCHEMA.makegoods_resolution_metrics TO `{{users}}`;
-- Not granted on purpose: the silver tables, ref_seller_book, ref_advertiser_contacts and gold_make_good_history
-- (unfiltered client and contract data). If check S3 in docs/v2_setup.md shows the rule functions need the caller to
-- read the tables inside them, grant SELECT on the silver tables those functions read (see the doc for the list).

-- you, as the first admin: manage the access map, and see everything
GRANT SELECT, MODIFY ON TABLE YOUR_CATALOG.YOUR_SCHEMA_security.mg_user_access TO `{{admin_user}}`;
GRANT SELECT, MODIFY ON TABLE YOUR_CATALOG.YOUR_SCHEMA_security.mg_access_audit TO `{{admin_user}}`;
MERGE INTO YOUR_CATALOG.YOUR_SCHEMA_security.mg_user_access t
USING (SELECT '{{admin_user}}' AS principal, '{{admin_name}}' AS display_name) s ON t.principal = s.principal
WHEN NOT MATCHED THEN INSERT (principal, display_name, role, updated_by, updated_at)
  VALUES (s.principal, s.display_name, 'admin', 'setup', current_timestamp());
INSERT INTO YOUR_CATALOG.YOUR_SCHEMA_security.mg_access_audit
  VALUES (current_timestamp(), 'setup', '{{admin_user}}', 'grant', '{"role": "admin"}');

-- the app's service principal: load the agent, run the rule functions, write traces and its own records
GRANT USE CATALOG ON CATALOG YOUR_CATALOG TO `{{app_sp}}`;
GRANT USE SCHEMA, EXECUTE ON SCHEMA YOUR_CATALOG.YOUR_SCHEMA TO `{{app_sp}}`;
-- loading the agent model checks that the app can read every table it declares (lineage); the row filters still return
-- no client rows to the app's own identity, which has no row in the access map
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.mg_rules_config TO `{{app_sp}}`;
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.gold_advertiser_preferences TO `{{app_sp}}`;
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.gold_make_good_candidates TO `{{app_sp}}`;
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.gold_available_inventory TO `{{app_sp}}`;
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.gold_spot_access TO `{{app_sp}}`;
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.gold_make_good_outcomes TO `{{app_sp}}`;
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.ref_sellers TO `{{app_sp}}`;
GRANT SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.ref_advertiser_contacts TO `{{app_sp}}`;
GRANT SELECT ON VIEW YOUR_CATALOG.YOUR_SCHEMA.makegoods_open_queue_metrics TO `{{app_sp}}`;
GRANT SELECT ON VIEW YOUR_CATALOG.YOUR_SCHEMA.makegoods_offer_outcome_metrics TO `{{app_sp}}`;
GRANT SELECT ON VIEW YOUR_CATALOG.YOUR_SCHEMA.makegoods_preemption_metrics TO `{{app_sp}}`;
GRANT SELECT ON VIEW YOUR_CATALOG.YOUR_SCHEMA.makegoods_contract_delivery_metrics TO `{{app_sp}}`;
GRANT SELECT ON VIEW YOUR_CATALOG.YOUR_SCHEMA.makegoods_resolution_metrics TO `{{app_sp}}`;
GRANT MODIFY, SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.mg_decision_events TO `{{app_sp}}`;
GRANT MODIFY, SELECT ON TABLE YOUR_CATALOG.YOUR_SCHEMA.mg_advertiser_rules TO `{{app_sp}}`;
-- traces and reviewer feedback: writing a trace to the trace tables also reads them
GRANT SELECT, MODIFY ON TABLE YOUR_CATALOG.YOUR_SCHEMA.mg_agent_otel_spans TO `{{app_sp}}`;
GRANT SELECT, MODIFY ON TABLE YOUR_CATALOG.YOUR_SCHEMA.mg_agent_otel_logs TO `{{app_sp}}`;
GRANT SELECT, MODIFY ON TABLE YOUR_CATALOG.YOUR_SCHEMA.mg_agent_otel_metrics TO `{{app_sp}}`;
GRANT SELECT, MODIFY ON TABLE YOUR_CATALOG.YOUR_SCHEMA.mg_agent_otel_annotations TO `{{app_sp}}`;
-- client replies: the app stores each one and reads it with AI Functions (read_client_email)
GRANT SELECT, MODIFY ON TABLE YOUR_CATALOG.YOUR_SCHEMA.mg_client_emails TO `{{app_sp}}`;
GRANT SELECT, MODIFY ON TABLE YOUR_CATALOG.YOUR_SCHEMA.mg_client_email_reads TO `{{app_sp}}`;
