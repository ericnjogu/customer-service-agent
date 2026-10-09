# WhatsApp Business App coexistence

New Embedded Signup requests use `whatsapp_business_app_onboarding`. The backend
requires Meta to confirm `is_on_biz_app` and Cloud API access, subscribes the WABA,
and requests history through `smb_app_data` only when the user opts in and connects
Google Drive. It does not call PIN registration.
Existing standard Cloud API connections are not converted automatically.

## Human replies

`whatsapp_connections.pause_on_human_reply` defaults to true. A new
`smb_message_echoes` event archives the human reply and pauses that customer chat.
Other chats remain active. Subsequent customer messages are stored without calling
the answering graph or sending a reply. The send path rechecks the pause under
the same database lock used by echo handling; an already-sent reply cannot be recalled.

An operator can configure future human replies per tenant:

```sql
UPDATE whatsapp_connections SET pause_on_human_reply = false
WHERE tenant_id = '<tenant-id>';
```

This does not resume already-paused conversations. No timer, automatic resume,
or SLA follow-up is implemented. Resume policy is deliberately deferred.

## History

History sharing requires the client's Meta consent. History events are staged by
connection before provisioning, then imported into the tenant's conversations.
Original timestamps and customer/human provenance are retained; duplicate message
IDs are ignored. Imported messages never trigger replies or issue-processing jobs.
Text is imported; non-text messages retain a type placeholder in the transcript.
For opted-in connections, separate attachment records associate asynchronously
archived Drive files with these messages. See [Google Drive archival](google-drive-archive.md).
Contact synchronization is not implemented.

`history_status` distinguishes requested, receiving, declined, and failed sync.
Receiving is not proof of complete history: no completion guarantee is inferred
from an individual chunk. Inspect history delivery in real Meta acceptance testing.

## Rollout checks

Enable coexistence in the Meta signup configuration and subscribe the webhook to
`messages`, `history`, and `smb_message_echoes`. Keep signature validation enabled.
Confirm the actual Business App completion event, history consent and delivery,
duplicate handling, and human-echo pause behavior using a test business number.
Verify ordinary Cloud API messaging still works for existing tenants.

This implementation does not deploy or change Meta configuration. Do not enable
customer rollout until these real-provider checks pass. A history request accepted
by Meta immediately before a process crash may need operator reconciliation before
retrying signup; database progress cannot make the remote request transactional.
