tnt_0d688d324c4344ef977270587843c872BEGIN;

SET LOCAL app.purge_tenant = 'tnt_0d688d324c4344ef977270587843c872';

DELETE FROM conversation_turn_sentiments
WHERE tenant_id = current_setting('app.purge_tenant');

DELETE FROM conversation_state_events
WHERE conversation_id IN (
    SELECT id FROM conversations
    WHERE tenant_id = current_setting('app.purge_tenant')
);

DELETE FROM messages
WHERE tenant_id = current_setting('app.purge_tenant');

DELETE FROM conversation_issues
WHERE tenant_id = current_setting('app.purge_tenant');

DELETE FROM conversations
WHERE tenant_id = current_setting('app.purge_tenant');

DELETE FROM knowledge_documents
WHERE tenant_id = current_setting('app.purge_tenant');

DELETE FROM tenant_configs
WHERE tenant_id = current_setting('app.purge_tenant');

DELETE FROM tenants
WHERE tenant_id = current_setting('app.purge_tenant');

COMMIT;