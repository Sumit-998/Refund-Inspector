-- Removes only the retired email delivery audit table. Customer contacts,
-- booking data, authentication tables, and all refund evidence remain intact.
DROP TABLE IF EXISTS email_logs;

INSERT INTO schema_migrations (version, description)
VALUES ('003_remove_email_features', 'Remove the retired email audit table')
ON CONFLICT (version) DO NOTHING;
