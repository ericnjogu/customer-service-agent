# WhatsApp media archive in Google Drive

## Setup (separate from deployment)

1. Enable the Google Drive API for a Google Cloud project and configure its OAuth
   consent screen. Use a Web application OAuth client. Complete Google's applicable
   publishing/verification requirements before customer use; testing-mode grants
   are not a durable production authorization arrangement.
2. Register the exact callback URL:
   `https://css.ristoh.co.ke/api/onboarding/drive/callback`.
   For local ngrok testing, register the equivalent callback for that hostname and
   set `AGENT_WEB_PUBLIC_BASE_URL` to that origin. Update it if the hostname changes.
3. Put `AGENT_GOOGLE_DRIVE_CLIENT_ID` and `AGENT_GOOGLE_DRIVE_CLIENT_SECRET` in the
   SOPS-encrypted `app-configs` Secret. Helm references them as optional keys; without
   both, the archive is unavailable but ordinary WhatsApp signup remains usable.
4. Apply the updated `infra/openbao/application-policy.hcl` using the existing
   operator procedure. It adds read/create/update access only to
   `tenant-credentials/data/google-drive/*`; it does not grant deletion or listing.
   Local OpenBao bootstrap uses this same policy file.
5. Deploy after tests. Verify real Google consent, private folder ownership, Meta
   historical-media availability, and one actual transfer with a test business.
   Automated tests mock providers and cannot certify Meta account eligibility.

The application requests only `drive.file` and offline access. Refresh tokens live
in OpenBao at a stable connection-specific path; no tokens enter jobs or database
payloads. Google endpoint overrides are accepted only in the `test` environment.
The Google callback is excluded from application tracing and nginx access logs.
Also configure any external ingress/tunnel logging to redact OAuth callback query
parameters. Callback codes are forwarded in a URL fragment, immediately removed by
the UI, then exchanged through a browser-cookie-authorized same-origin POST.

## User flow

The WhatsApp screen fetches saved status when loaded. Import starts unchecked.
Checking it starts Google authorization and enables historical import plus ongoing
media archival. On return the screen displays the folder name and a checkmark.
Google cancellation leaves a retry button and allows opting out.

The SDK loads automatically once WhatsApp is eligible (including Drive readiness
when import is selected). This does not launch authorization or freeze preferences.
One `Connect WhatsApp` button opens Meta directly from the user click and starts
the backend attempt in parallel, which freezes preferences. Completion waits for
both the attempt and Meta results. No network request is awaited before `FB.login`,
avoiding popup blockers. Failed preparation offers a retry.
Meta still controls the user's history-sharing consent and available history/media.
Text/history stays in PostgreSQL; media binaries go to Drive. Provisioning does not
wait for transfers. Refreshing the screen shows current counts and history status.

Existing connections have a nullable history preference preserving their previous
history behavior; they are not enrolled in Drive archival. New connections default
to no import. Changing an already-frozen choice is not exposed by this feature.

## Storage and trust boundaries

```text
Ristoh CSS - <business name> - <connection ID>/
  WhatsApp/Conversations/<opaque chat archive ID>/Media/<attachment ID>.<extension>
  Knowledge Base/Sources/
  Knowledge Base/Generated/Chunks/
```

Database records retain Drive IDs, original filenames, media/message identifiers,
timestamps, speaker provenance, and tenant/conversation links. Folder names are not
identifiers. Pre-provisioning attachments attach to staged messages and acquire
canonical links when provisioning imports those messages.

Folders/files are private; shared destinations are rejected. No sharing permissions
are created. Renames do not break links. Successfully initialized folders that later
disappear are not recreated silently. Completed attachments are not re-uploaded.
File bodies remain untrusted: only basic signature/type/size checks are performed,
not antivirus scanning, extraction, OCR, transcription or execution. Historical media
availability is limited by Meta; unavailable items remain visible as failures.

`Sources` and `Generated/Chunks` reserve separate inputs and outputs for a future KB
feature. No watcher, chunker, extraction or KB publication is implemented. Limited
`drive.file` access does not automatically authorize arbitrary files dropped into a
folder. Future ingestion must address selection/authorization and ignore generated
outputs to prevent processing loops.

## Operations and recovery

The independent `whatsapp_media_archive` PgQueuer entrypoint starts through application
lifespan, even when issue processing is disabled. The database concurrency limit is
initially one archive job globally (therefore at most one per replica). Downloads use
bounded temporary files, cleaned on exit; resumable upload URLs and pre-generated
Drive IDs let retries recover without duplicate files. Those URLs are internal
transfer state and must not be logged or exposed.

`AGENT_MEDIA_ARCHIVE_MAX_BYTES` / Helm `mediaArchive.maxBytes` defaults to 100,000,000
bytes and can be lowered. Supported types include images, office/PDF/text documents,
audio, video and stickers; unsupported types are recorded rather than interpreted.

Inspect attachment `status`, `error_code`, `updated_at` and `queue_id`; inspect
`pgqueuer` rows where `entrypoint='whatsapp_media_archive'` for held/exhausted jobs.
Monitor pending age, failure count, Drive attention status and temporary-disk capacity.
Transient errors retry five executions with exponential backoff. Permanent errors
retain attachment records without continuously retrying a revoked credential.

Reconnect Drive using the same Google account to recover revoked access. Resolve
quota/privacy issues first. `Retry failed transfers` requeues up to 100 recoverable
failed attachments in the authorized onboarding session, without duplicating active
retries. The corresponding endpoint is
`POST /onboarding/sessions/{id}/archive/retry` and requires verified-browser access.
For held jobs after the onboarding browser authorization expires, use PgQueuer's
`Queries.requeue_jobs([job_id])` operator procedure documented for issue jobs, after
checking that the entrypoint and attachment belong to the intended tenant.

`POST /onboarding/sessions/{id}/drive/disconnect` stops new transfers at their next
authorization check; an in-flight request may finish. It does not delete user-owned
files or revoke the Google account's other grants. Users can revoke authorization in
Google. No automatic retention deletion is introduced. For abandoned onboarding,
disable its Drive connection first, inspect outstanding jobs, then explicitly clean
up its dedicated folder and vault credential with operator approval. Do not delete
references before deciding whether their media must be retained.

## Validation

Run `tests/test_media_archive.py` and `tests/test_whatsapp_connections.py` with
`AGENT_WHATSAPP_TEST_DATABASE_URL` pointing to disposable pgvector PostgreSQL. CI
already supplies this database. `bash scripts/run-browser-qa.sh` starts disposable
PostgreSQL, Redis and WireMock and covers opt-out, Google consent, cancellation,
folder creation, refresh, Meta callbacks and provisioning. Live Google/Meta consent
and real media transfer are an explicit manual acceptance gate, not routine CI.
