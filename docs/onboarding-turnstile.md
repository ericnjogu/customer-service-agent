# Signup protection with Cloudflare Turnstile

The first account screen renders Turnstile. The API verifies its single-use token
before creating a session or sending the initial verification email. Rejected,
expired, missing, or wrong-action/hostname tokens cannot create a session. Provider
failures fail closed. Resumes and later onboarding steps do not create new sessions.
This is an abuse mitigation, not a guarantee that all automated signups are blocked.

## Configuration before deployment

Create a Managed widget in Cloudflare Turnstile and allow `css.ristoh.co.ke`.
Add these keys to the SOPS-encrypted `app-configs` Secret:

- `AGENT_TURNSTILE_SITE_KEY`: the widget's public site key.
- `AGENT_TURNSTILE_SECRET_KEY`: its private server verification secret.

Do not commit plaintext keys. Production Helm values enable
`onboarding.turnstileEnabled`; `onboarding.turnstileSecretName` defaults to
`app-configs`. Populate both keys before deploying; missing keys intentionally
prevent public signup. Only the site key is returned by `/onboarding/config`.
`WEB_PUBLIC_BASE_URL` must match the browser origin: the API checks the returned
hostname as well as the `onboarding` action. Allow the challenge script/frame from
`https://challenges.cloudflare.com` in any externally configured CSP.

For local running set `AGENT_ONBOARDING_TURNSTILE_ENABLED=true` and supply the same
two key variables through the local application configuration. Use a dedicated
widget allowing your local/ngrok hostname and set `WEB_PUBLIC_BASE_URL` accordingly.
For the local Kubernetes deployment, put both keys in the local `app-configs`
Secret and run:

```bash
AGENT_ONBOARDING_TURNSTILE_ENABLED=true \
WEB_PUBLIC_BASE_URL=https://my-site.ngrok-free.dev \
bash scripts/deploy-local.sh
```

The script forwards the enable flag into Helm; the chart reads the keys from
`app-configs`. After rollout, `/api/onboarding/config` should report
`turnstile_enabled: true` and a populated `turnstile_site_key`.

Local defaults and container browser QA leave protection disabled so they do not
depend on Cloudflare or attempt to solve a real challenge. Focused tests mock the
widget and Siteverify responses, including denial and outage behavior.

The browser resets the challenge after every submission attempt because tokens
expire after five minutes and can be consumed only once. Tokens are sent in a
request header, never persisted with session data or returned by API responses.

See [Cloudflare Siteverify documentation](https://developers.cloudflare.com/turnstile/get-started/server-side-validation/).
