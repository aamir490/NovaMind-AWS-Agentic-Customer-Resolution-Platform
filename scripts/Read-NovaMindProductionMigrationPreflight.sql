-- Read-only production preflight. Do not add writes, migrations, or secrets.
WITH required_tables(name) AS (VALUES
    ('customers'), ('orders'), ('order_items'), ('support_cases'),
    ('trusted_delivery_sources'), ('delivery_facts'), ('eligibility_evidence'),
    ('resolution_proposals'), ('workflow_runs'), ('workflow_review_bindings'),
    ('workflow_audit_events'), ('workflow_run_events'), ('langgraph_checkpoints'),
    ('langgraph_checkpoint_blobs'), ('langgraph_checkpoint_writes'),
    ('langgraph_checkpoint_staged_writes'), ('case_resolution_decisions'),
    ('case_customer_communications'), ('investigation_jobs'), ('investigation_control'),
    ('investigation_mode_audit'), ('investigation_start_requests'),
    ('cognito_customer_bindings'), ('investigation_results'), ('investigation_outbox'),
    ('schema_migrations')
), table_owners AS (
    SELECT r.name, c.relname, n.nspname AS schema_name,
           pg_get_userbyid(c.relowner) AS owner_name
    FROM required_tables AS r
    LEFT JOIN pg_class AS c ON c.relname = r.name AND c.relkind IN ('r', 'p')
    LEFT JOIN pg_namespace AS n ON n.oid = c.relnamespace AND n.nspname = 'novamind'
), required_functions(name) AS (VALUES ('novamind_workflow_guard')),
function_owners AS (
    SELECT r.name, p.proname, pg_get_userbyid(p.proowner) AS owner_name
    FROM required_functions AS r
    LEFT JOIN pg_proc AS p ON p.proname = r.name
    LEFT JOIN pg_namespace AS n ON n.oid = p.pronamespace
    WHERE p.oid IS NULL OR n.nspname = 'novamind'
), ledger AS (
    SELECT version FROM novamind.schema_migrations
)
SELECT current_database() AS database_name,
       current_schema() AS session_schema,
       current_user AS session_role,
       session_user AS login_role,
       COALESCE((SELECT pg_get_userbyid(nspowner) = 'novamind_app'
                 FROM pg_namespace WHERE nspname = 'novamind'), FALSE) AS schema_owned,
       has_database_privilege(current_user, current_database(), 'CONNECT') AS connect_privilege,
       has_schema_privilege(current_user, 'novamind', 'USAGE') AS usage_privilege,
       has_schema_privilege(current_user, 'novamind', 'CREATE') AS create_privilege,
       COALESCE((SELECT bool_and(relname IS NOT NULL AND schema_name = 'novamind'
                                 AND owner_name = 'novamind_app')
                 FROM table_owners), FALSE) AS required_tables_owned,
       (SELECT count(*) FROM required_tables) AS required_table_count,
       COALESCE((SELECT bool_and(proname IS NOT NULL AND owner_name = 'novamind_app')
                 FROM function_owners), TRUE) AS required_functions_owned,
       (SELECT count(*) FROM required_functions WHERE name IS NOT NULL) AS required_function_count,
       (SELECT count(*) FROM ledger) AS migration_count,
       (SELECT max(version) FROM ledger) AS current_version,
       COALESCE((SELECT bool_and(version BETWEEN 1 AND 13) FROM ledger), FALSE) AS ledger_versions_bounded,
       COALESCE((SELECT count(*) = max(version) FROM ledger), FALSE) AS ledger_contiguous;
