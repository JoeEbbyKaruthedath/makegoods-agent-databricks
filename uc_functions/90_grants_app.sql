-- Least-privilege UC grants for the agent app's service principal (re-run after recreating the app).
-- Read business data, execute the rule/tool functions, write trace spans (mg_agent_otel_* tables).
-- App: makegoods-dev  ->  service principal client id YOUR_APP_SP_CLIENT_ID
USE CATALOG YOUR_CATALOG;
GRANT USE CATALOG ON CATALOG YOUR_CATALOG TO `YOUR_APP_SP_CLIENT_ID`;
GRANT USE SCHEMA, SELECT, EXECUTE ON SCHEMA YOUR_CATALOG.YOUR_SCHEMA TO `YOUR_APP_SP_CLIENT_ID`;
GRANT MODIFY ON TABLE YOUR_CATALOG.YOUR_SCHEMA.mg_agent_otel_spans TO `YOUR_APP_SP_CLIENT_ID`;
GRANT MODIFY ON TABLE YOUR_CATALOG.YOUR_SCHEMA.mg_agent_otel_logs TO `YOUR_APP_SP_CLIENT_ID`;
GRANT MODIFY ON TABLE YOUR_CATALOG.YOUR_SCHEMA.mg_agent_otel_metrics TO `YOUR_APP_SP_CLIENT_ID`;
GRANT MODIFY ON TABLE YOUR_CATALOG.YOUR_SCHEMA.mg_agent_otel_annotations TO `YOUR_APP_SP_CLIENT_ID`;
