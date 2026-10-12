# Standard Cloud API app-review testing

Standard Cloud API is the default until app review is complete:
`AGENT_ONBOARDING_WHATSAPP_MODE=cloud_api` (Helm `onboarding.whatsappMode`).
Local deployment accepts this environment variable through `scripts/deploy-local.sh`.
To opt into coexistence explicitly, set the variable or Helm value to `coexistence`.
Completing app review will not automatically change this setting.

This mode omits the Business App `featureType` from the Meta SDK launch, changes
the onboarding explanation, and disables the combined history/media import
option. It does not bypass email verification, browser authorization, token
ownership/permissions, WABA membership, or verified-phone checks. The number
must already report `platform_type=CLOUD_API`; a verified but pending number
cannot be connected. Register it with Meta separately if necessary. Do not
register a number still used in the Business App through this mode.

Tokens from the console test tool are not authorization codes and cannot be
pasted into the public completion endpoint. An operator-assisted local test may
validate the token using the same Meta adapter, store it in local OpenBao, and
call the normal connection/provisioning services for a verified session. This
does not test the browser authorization-code exchange; record that limitation
when preparing app-review evidence. Never commit tokens or registration PINs.

Existing deployments are not changed by editing these defaults; the next deployment
using them selects Cloud API unless explicitly overridden. A local
successful setup does not establish Meta app-review approval or prove webhook
delivery: separately verify the public HTTPS callback, signature validation,
inbound routing and outbound replies with the intended test account.
