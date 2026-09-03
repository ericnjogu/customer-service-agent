import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { parsePhoneNumberFromString } from "libphonenumber-js/min";
import {
  BlockTypeSelect,
  BoldItalicUnderlineToggles,
  CreateLink,
  ListsToggle,
  MDXEditor,
  Separator,
  UndoRedo,
  headingsPlugin,
  linkPlugin,
  listsPlugin,
  markdownShortcutPlugin,
  toolbarPlugin,
} from "@mdxeditor/editor";
import "@mdxeditor/editor/style.css";
import "./styles.css";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";
const API_REQUEST_TIMEOUT_MS = 60000;
const REQUIRE_ADMIN_EMAIL_DOMAIN_MATCH = parseBooleanEnv(
  import.meta.env.VITE_REQUIRE_ADMIN_EMAIL_DOMAIN_MATCH,
  true,
);
const TERMS_PATH = "/terms";
const TERMS_VERSION = "beta-2026-08-28";

const steps = [
  "account",
  "username-email-verification",
  "website",
  "website-email-verification",
  "analyzing",
  "analysis",
  "contact",
  "awaiting-review",
  "telegram",
  "complete",
];

function App() {
  const [session, setSession] = useState(null);
  const [step, setStep] = useState("account");
  const [alert, setAlert] = useState(null);
  const [busyAction, setBusyAction] = useState(null);
  const [startForm, setStartForm] = useState({
    username_email: "",
    given_name: "",
    family_name: "",
    admin_phone_number: "",
    admin_role_title: "",
    authority_confirmed: false,
    terms_accepted: false,
  });
  const [websiteForm, setWebsiteForm] = useState({
    website_url: "",
    website_verification_email: "",
  });

  const actionParams = useMemo(() => {
    const params = new URLSearchParams(window.location.search);
    return {
      sessionId: params.get("session_id"),
      token: params.get("token"),
      isTerms: window.location.pathname === TERMS_PATH,
      isTelegramSetup: window.location.pathname.includes("telegram-setup"),
      isUsernameEmailVerification: window.location.pathname.includes(
        "verify-username-email",
      ),
      isWebsiteEmailVerification: window.location.pathname.includes(
        "verify-website-email",
      ),
    };
  }, []);

  const busy = busyAction !== null;

  useEffect(() => {
    const savedSessionId = window.localStorage.getItem("onboarding_session_id");
    const sessionId = actionParams.sessionId || savedSessionId;
    if (sessionId) {
      loadSession(sessionId)
        .then((loaded) => {
          if (actionParams.isUsernameEmailVerification && actionParams.token) {
            if (loaded.username_email_verified) {
              replaceUrlWithResumeLink(loaded.session_id);
              return loaded;
            }
            return verifyUsernameEmail(loaded.session_id, actionParams.token);
          }
          if (actionParams.isWebsiteEmailVerification && actionParams.token) {
            if (loaded.website_email_verified) {
              replaceUrlWithResumeLink(loaded.session_id);
              setStep("analyzing");
              return analyzeWebsiteForSession(loaded.session_id);
            }
            return verifyWebsiteEmail(loaded.session_id, actionParams.token);
          }
          return loaded;
        })
        .catch((error) => showError(error.message));
    }
  }, [actionParams.sessionId]);

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), API_REQUEST_TIMEOUT_MS);
    const startedAt = performance.now();
    const method = options.method || "GET";
    console.info("Onboarding API request started", { method, path });
    try {
      const response = await fetch(`${API_BASE_URL}${path}`, {
        ...options,
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          ...(options.headers || {}),
        },
      });
      const body = await response.json().catch(() => ({}));
      console.info("Onboarding API request completed", {
        method,
        path,
        status: response.status,
        elapsedMs: Math.round(performance.now() - startedAt),
      });
      if (!response.ok) {
        console.warn("Onboarding API request failed", {
          method,
          path,
          status: response.status,
          body,
        });
        throw new Error(readApiError(body));
      }
      return body;
    } catch (error) {
      if (error.name === "AbortError") {
        console.warn("Onboarding API request timed out", {
          method,
          path,
          elapsedMs: Math.round(performance.now() - startedAt),
        });
        throw new Error(
          "The request took too long. Please check the API logs and try again.",
        );
      }
      console.warn("Onboarding API request error", {
        method,
        path,
        message: error.message,
        elapsedMs: Math.round(performance.now() - startedAt),
      });
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function loadSession(sessionId) {
    const loaded = await api(`/onboarding/sessions/${sessionId}`);
    setSession(loaded);
    setWebsiteForm({
      website_url: loaded.website_url || "",
      website_verification_email: loaded.website_verification_email || "",
    });
    window.localStorage.setItem("onboarding_session_id", loaded.session_id);
    setStep(actionParams.isTelegramSetup ? "telegram" : stepFromSession(loaded));
    return loaded;
  }

  function showError(message) {
    setAlert({ type: "error", message });
  }

  function showInfo(message) {
    setAlert({ type: "info", message });
  }

  async function submitStart() {
    setBusyAction("create-session");
    setAlert(null);
    console.info("Submitting onboarding account setup", {
      usernameEmail: startForm.username_email,
    });
    try {
      const created = await api("/onboarding/sessions", {
        method: "POST",
        body: JSON.stringify({
          admin: {
            username_email: startForm.username_email,
            given_name: startForm.given_name,
            family_name: startForm.family_name,
            phone_number: startForm.admin_phone_number,
            role_title: startForm.admin_role_title,
            authority_confirmed: startForm.authority_confirmed,
            terms_accepted: startForm.terms_accepted,
          },
        }),
      });
      window.localStorage.setItem("onboarding_session_id", created.session_id);
      setSession(created);
      setStep("username-email-verification");
      showInfo(
        "Account email verification sent. Please check your inbox to continue.",
      );
      console.info("Onboarding account setup submitted", {
        sessionId: created.session_id,
        status: created.status,
        currentStep: created.current_step,
      });
    } catch (error) {
      console.warn("Onboarding account setup failed", {
        usernameEmail: startForm.username_email,
        message: error.message,
      });
      showError(error.message);
    } finally {
      setBusyAction(null);
    }
  }

  async function verifyUsernameEmail(sessionId, token) {
    setBusyAction("verify-username-email");
    setAlert(null);
    try {
      const verified = await api(
        `/onboarding/sessions/${sessionId}/verify-username-email`,
        {
          method: "POST",
          body: JSON.stringify({ token }),
        },
      );
      replaceUrlWithResumeLink(verified.session_id);
      setSession(verified);
      setStep("website");
      showInfo("Account email verified. Continue with website verification.");
      return verified;
    } catch (error) {
      showError(error.message);
      setStep("username-email-verification");
      throw error;
    } finally {
      setBusyAction(null);
    }
  }

  async function resendUsernameEmailVerification() {
    setBusyAction("resend-username-email");
    setAlert(null);
    try {
      const updated = await api(
        `/onboarding/sessions/${session.session_id}/send-username-email-verification`,
        { method: "POST" },
      );
      setSession(updated);
      showInfo("Account email verification sent again. Please check your inbox.");
    } catch (error) {
      showError(error.message);
    } finally {
      setBusyAction(null);
    }
  }

  async function submitWebsiteVerification() {
    setBusyAction("save-website");
    setAlert(null);
    try {
      const websiteUrl = normalizeWebsiteUrl(websiteForm.website_url);
      const updated = await api(`/onboarding/sessions/${session.session_id}/website`, {
        method: "PATCH",
        body: JSON.stringify({
          website_url: websiteUrl,
          website_verification_email: websiteForm.website_verification_email,
        }),
      });
      setWebsiteForm({
        website_url: updated.website_url || websiteUrl,
        website_verification_email:
          updated.website_verification_email ||
          websiteForm.website_verification_email,
      });
      setSession(updated);
      setStep("website-email-verification");
      showInfo(
        "Website verification email sent. Please check that inbox to continue.",
      );
    } catch (error) {
      showError(error.message);
    } finally {
      setBusyAction(null);
    }
  }

  async function continueFromWebsite() {
    const websiteUrl = normalizeWebsiteUrl(websiteForm.website_url);
    const savedWebsiteUrl = session.website_url || "";
    const savedWebsiteEmail = session.website_verification_email || "";
    const websiteUnchanged =
      websiteUrl === savedWebsiteUrl &&
      websiteForm.website_verification_email === savedWebsiteEmail;

    if (websiteUnchanged && session.website_email_verified && session.analysis) {
      setStep("analysis");
      return;
    }
    if (websiteUnchanged && session.website_email_verified) {
      await analyzeWebsite();
      return;
    }
    await submitWebsiteVerification();
  }

  async function resendWebsiteEmailVerification() {
    setBusyAction("resend-website-email");
    setAlert(null);
    try {
      const updated = await api(
        `/onboarding/sessions/${session.session_id}/send-website-email-verification`,
        { method: "POST" },
      );
      setSession(updated);
      showInfo("Website verification email sent again. Please check your inbox.");
    } catch (error) {
      showError(error.message);
    } finally {
      setBusyAction(null);
    }
  }

  async function verifyWebsiteEmail(sessionId, token) {
    setBusyAction("verify-website-email");
    setAlert(null);
    try {
      const verified = await api(
        `/onboarding/sessions/${sessionId}/verify-website-email`,
        {
          method: "POST",
          body: JSON.stringify({ token }),
        },
      );
      replaceUrlWithResumeLink(verified.session_id);
      setSession(verified);
      setStep("analyzing");
      showInfo("Website email verified. Analyzing the website now.");
      setBusyAction("analyze-website");
      const analyzed = await analyzeWebsiteForSession(verified.session_id);
      showInfo("Website analysis is ready for review.");
      return analyzed;
    } catch (error) {
      showError(error.message);
      setStep("website-email-verification");
      throw error;
    } finally {
      setBusyAction(null);
    }
  }

  async function analyzeWebsite() {
    setBusyAction("analyze-website");
    setAlert(null);
    try {
      if (session?.analysis) {
        setStep("analysis");
        return;
      }
      await analyzeWebsiteForSession(session.session_id);
    } catch (error) {
      showError(error.message);
    } finally {
      setBusyAction(null);
    }
  }

  async function reanalyzeWebsite() {
    setBusyAction("analyze-website");
    setAlert(null);
    try {
      await analyzeWebsiteForSession(session.session_id, { force: true });
    } catch (error) {
      showError(error.message);
    } finally {
      setBusyAction(null);
    }
  }

  async function analyzeWebsiteForSession(sessionId, { force = false } = {}) {
    setStep("analyzing");
    const query = force ? "?force=true" : "";
    const analyzed = await api(
      `/onboarding/sessions/${sessionId}/analyze-website${query}`,
      { method: "POST" },
    );
    setSession(analyzed);
    setStep("analysis");
    return analyzed;
  }

  async function patchSession(update, nextStep) {
    setBusyAction("save-session");
    setAlert(null);
    try {
      const updated = await api(`/onboarding/sessions/${session.session_id}`, {
        method: "PATCH",
        body: JSON.stringify({ ...update, current_step: nextStep }),
      });
      setSession(updated);
      setStep(nextStep);
      return updated;
    } catch (error) {
      showError(error.message);
      throw error;
    } finally {
      setBusyAction(null);
    }
  }

  async function requestTelegramSetup(targetSessionId = session.session_id) {
    setBusyAction("request-telegram-setup");
    setAlert(null);
    try {
      const updated = await api(
        `/onboarding/sessions/${targetSessionId}/request-telegram-setup`,
        { method: "POST" },
      );
      setSession(updated);
      setStep(actionParams.isTelegramSetup ? "telegram" : "awaiting-review");
      showInfo("Your onboarding details were sent for review.");
    } catch (error) {
      showError(error.message);
    } finally {
      setBusyAction(null);
    }
  }

  async function submitTelegramSetup(payload) {
    setBusyAction("submit-telegram-setup");
    setAlert(null);
    try {
      const updated = await api(
        `/onboarding/sessions/${session.session_id}/telegram-setup`,
        {
          method: "POST",
          body: JSON.stringify({
            token: actionParams.token || payload.token,
            bot_token: payload.bot_token,
          }),
        },
      );
      setSession(updated);
      setStep("complete");
      showInfo("Telegram setup saved. You can submit provisioning now.");
    } catch (error) {
      showError(error.message);
    } finally {
      setBusyAction(null);
    }
  }

  async function submitProvisioning() {
    setBusyAction("submit-provisioning");
    setAlert(null);
    try {
      const accepted = await api(`/onboarding/sessions/${session.session_id}/submit`, {
        method: "POST",
      });
      const updated = await api(`/onboarding/sessions/${session.session_id}`);
      setSession(updated);
      setStep("complete");
      showInfo(`Provisioning job accepted: ${accepted.job_id}`);
    } catch (error) {
      showError(error.message);
    } finally {
      setBusyAction(null);
    }
  }

  return (
    <main className="page">
      {actionParams.isTerms ? (
        <TermsScreen />
      ) : (
      <section className="shell">
        <header className="hero">
          <p className="eyebrow">Risto AI CSS</p>
          <h1>Customer-service onboarding</h1>
          <p>
            Review the business profile, approve public details, and hand off
            Telegram setup without losing draft state.
          </p>
        </header>

        <Progress step={step} />

        {alert && (
          <div className={`alert ${alert.type}`}>
            <span>{alert.message}</span>
            <button type="button" onClick={() => setAlert(null)}>
              Dismiss
            </button>
          </div>
        )}

        {step === "account" && (
          <StartScreen
            form={startForm}
            setForm={setStartForm}
            onSubmit={submitStart}
            busy={busy}
          />
        )}

        {session && step === "username-email-verification" && (
          <EmailVerificationScreen
            session={session}
            kind="username"
            onBack={() => setStep("account")}
            onReload={() => loadSession(session.session_id)}
            onResend={resendUsernameEmailVerification}
            onContinue={() => setStep("website")}
            busy={busy}
            busyAction={busyAction}
          />
        )}

        {session && step === "website" && (
          <WebsiteVerificationScreen
            form={websiteForm}
            setForm={setWebsiteForm}
            onSubmit={continueFromWebsite}
            onBack={() => setStep("username-email-verification")}
            busy={busy}
          />
        )}

        {session && step === "website-email-verification" && (
          <EmailVerificationScreen
            session={session}
            kind="website"
            onBack={() => setStep("website")}
            onReload={() => loadSession(session.session_id)}
            onResend={resendWebsiteEmailVerification}
            onContinue={reanalyzeWebsite}
            busy={busy}
            busyAction={busyAction}
          />
        )}

        {session && step === "analyzing" && (
          <AnalyzingScreen session={session} busyAction={busyAction} />
        )}

        {session && step === "analysis" && (
          <AnalysisScreen
            session={session}
            onBack={() => setStep("website")}
            onNext={(draft) => patchSession(draft, "contact")}
            busy={busy}
          />
        )}

        {session && step === "business" && (
          <BusinessScreen
            session={session}
            onBack={() => setStep("analysis")}
            onNext={(business_profile) =>
              patchSession({ business_profile }, "contact")
            }
            busy={busy}
          />
        )}

        {session && step === "contact" && (
          <ContactInfoScreen
            session={session}
            onBack={() => setStep("analysis")}
            onNext={(contact_info) =>
              patchSession({ contact_info }, "awaiting-review").then((updated) =>
                requestTelegramSetup(updated.session_id),
              )
            }
            busy={busy}
          />
        )}

        {session && step === "awaiting-review" && !actionParams.isTelegramSetup && (
          <WaitingForReviewScreen session={session} onBack={() => setStep("contact")} />
        )}

        {session && step === "telegram" && actionParams.isTelegramSetup && (
          <TelegramScreen
            session={session}
            token={actionParams.token}
            onBack={() => setStep("contact")}
            onSubmit={submitTelegramSetup}
            busy={busy}
          />
        )}

        {session && step === "complete" && (
          <CompletionScreen
            session={session}
            onBack={() => setStep("telegram")}
            onSubmit={submitProvisioning}
            busy={busy}
          />
        )}
      </section>
      )}
    </main>
  );
}

function TermsScreen() {
  return (
    <section className="shell">
      <header className="hero">
        <p className="eyebrow">Risto AI CSS</p>
        <h1>Beta Terms of Service</h1>
        <p>
          Effective date: August 28, 2026 · Version: {TERMS_VERSION}
        </p>
      </header>
      <article className="card terms">
        <p>
          These Beta Terms of Service govern access to and use of Risto AI's
          Customer Service Solution, also called CSS, during its beta testing
          period.
        </p>
        <p>
          By continuing with onboarding, you confirm that you are authorized to
          configure customer-service automation for the business you are
          onboarding and that you accept these Beta Terms.
        </p>

        <h2>1. Beta service</h2>
        <p>
          CSS is currently provided as a beta product. The service may be
          incomplete, unstable, unavailable, or changed without notice. Features
          may be added, changed, limited, or removed during the beta period.
        </p>

        <h2>2. No production guarantee</h2>
        <p>
          CSS is provided for testing and evaluation. You should not rely on it
          as the sole channel for urgent, sensitive, regulated, medical, legal,
          financial, or safety-critical customer support.
        </p>

        <h2>3. No service-level commitment</h2>
        <p>
          During the beta period, Risto AI does not provide uptime, response-time,
          support-time, or availability guarantees. The service may be
          interrupted, delayed, or unavailable.
        </p>

        <h2>4. AI-generated responses</h2>
        <p>
          CSS may use AI models to generate responses. AI-generated responses may
          be incomplete, inaccurate, outdated, or inappropriate. You remain
          responsible for reviewing important customer conversations and the
          business information used to configure the assistant.
        </p>

        <h2>5. Business and customer data</h2>
        <p>
          You may provide business information, website links, contact details,
          chat messages, documents, and configuration data during onboarding and
          testing. You confirm that you have the right to provide this information
          to Risto AI and to use it for customer-service automation.
        </p>

        <h2>6. Third-party services</h2>
        <p>
          CSS may rely on third-party services, including messaging platforms,
          AI model providers, tracing or observability providers, email providers,
          hosting providers, and web-search providers. Use of CSS may therefore
          depend on the availability and rules of those third-party services.
        </p>

        <h2>7. Acceptable use</h2>
        <p>
          You must not use CSS to break the law, send spam or abusive messages,
          impersonate others, process data you are not allowed to use, provide
          harmful customer support, or disrupt the service.
        </p>

        <h2>8. Confidentiality and feedback</h2>
        <p>
          During beta testing, you may receive access to non-public features,
          workflows, designs, documentation, or technical information. Unless
          Risto AI gives written permission, you should not publicly disclose
          non-public beta information. If you provide feedback, Risto AI may use
          it to improve, modify, or commercialize CSS without owing compensation.
        </p>

        <h2>9. Suspension, data loss, and changes</h2>
        <p>
          Risto AI may suspend or stop access to the beta service at any time.
          Because CSS is in beta, data may be lost, changed, delayed, duplicated,
          or unavailable. Risto AI may update these Beta Terms during the beta
          period and may require renewed acceptance for material changes.
        </p>

        <h2>10. No warranty and limitation of liability</h2>
        <p>
          The beta service is provided “as is” and “as available,” without
          warranties of any kind. To the maximum extent permitted by law, Risto AI
          will not be liable for indirect, incidental, special, consequential,
          exemplary, or punitive damages, or for loss of profits, revenue,
          goodwill, data, or business opportunity arising from use of the beta
          service.
        </p>

        <h2>11. Contact</h2>
        <p>
          For questions about these Beta Terms or CSS onboarding, contact the
          Risto AI onboarding team using the email or communication channel
          provided during onboarding.
        </p>
      </article>
    </section>
  );
}

function Progress({ step }) {
  const index = Math.max(0, steps.indexOf(step));
  return (
    <div className="progress">
      <div className="progress-bar" style={{ width: `${((index + 1) / steps.length) * 100}%` }} />
      <span>
        Step {index + 1} of {steps.length}: {step.replace("-", " ")}
      </span>
    </div>
  );
}

function StartScreen({ form, setForm, onSubmit, busy }) {
  const validation = useFormValidation(form, validateAccountForm);
  function handleSubmit(event) {
    event.preventDefault();
    if (!validation.validateForSubmit()) return;
    onSubmit();
  }

  return (
    <form className="card form" onSubmit={handleSubmit} noValidate>
      <h2>Account setup</h2>
      <FormErrorSummary errors={validation.errors} ref={validation.summaryRef} />
      <Field
        label="Username email"
        name="username_email"
        error={validation.errorFor("username_email")}
        help="This will become the admin's dashboard login email later, so we verify it separately from the website contact."
      >
        <input
          type="email"
          aria-invalid={Boolean(validation.errorFor("username_email"))}
          aria-describedby={fieldErrorId("username_email")}
          value={form.username_email}
          onChange={(event) =>
            setForm({ ...form, username_email: event.target.value })
          }
          onBlur={() => validation.validateField("username_email")}
          placeholder="you@example.com"
        />
      </Field>
      <Field
        label="Given name"
        name="given_name"
        error={validation.errorFor("given_name")}
        help="This is used to identify and greet the onboarding admin."
      >
        <input
          aria-invalid={Boolean(validation.errorFor("given_name"))}
          aria-describedby={fieldErrorId("given_name")}
          value={form.given_name}
          onChange={(event) =>
            setForm({ ...form, given_name: event.target.value })
          }
          onBlur={() => validation.validateField("given_name")}
        />
      </Field>
      <Field
        label="Family name"
        name="family_name"
        error={validation.errorFor("family_name")}
        help="This keeps future dashboard identity structured and auditable."
      >
        <input
          aria-invalid={Boolean(validation.errorFor("family_name"))}
          aria-describedby={fieldErrorId("family_name")}
          value={form.family_name}
          onChange={(event) =>
            setForm({ ...form, family_name: event.target.value })
          }
          onBlur={() => validation.validateField("family_name")}
        />
      </Field>
      <Field
        label="Admin phone number"
        name="admin_phone_number"
        error={validation.errorFor("admin_phone_number")}
        help="We use this as a backup contact for onboarding and future account recovery or urgent setup issues."
      >
        <input
          type="tel"
          aria-invalid={Boolean(validation.errorFor("admin_phone_number"))}
          aria-describedby={fieldErrorId("admin_phone_number")}
          value={form.admin_phone_number}
          onChange={(event) =>
            setForm({ ...form, admin_phone_number: event.target.value })
          }
          onBlur={() => validation.validateField("admin_phone_number")}
          placeholder="+254723921716"
        />
      </Field>
      <Field
        label="Admin role/title"
        name="admin_role_title"
        error={validation.errorFor("admin_role_title")}
        help="This helps the SaaS team understand the admin's authority and relationship to the business."
      >
        <input
          aria-invalid={Boolean(validation.errorFor("admin_role_title"))}
          aria-describedby={fieldErrorId("admin_role_title")}
          value={form.admin_role_title}
          onChange={(event) =>
            setForm({ ...form, admin_role_title: event.target.value })
          }
          onBlur={() => validation.validateField("admin_role_title")}
          placeholder="Owner, manager, operations lead"
        />
      </Field>
      <Field
        label="Authority confirmation"
        name="authority_confirmed"
        error={validation.errorFor("authority_confirmed")}
        help="This records that the submitter is allowed to configure customer-service automation for the business."
      >
        <div className="checkbox-row">
          <input
            type="checkbox"
            aria-invalid={Boolean(validation.errorFor("authority_confirmed"))}
            aria-describedby={fieldErrorId("authority_confirmed")}
            checked={form.authority_confirmed}
            onChange={(event) =>
              setForm({ ...form, authority_confirmed: event.target.checked })
            }
            onBlur={() => validation.validateField("authority_confirmed")}
          />
          <span>I am authorized to configure customer-service automation for this business.</span>
        </div>
      </Field>
      <Field
        label="Terms of service"
        name="terms_accepted"
        error={validation.errorFor("terms_accepted")}
        help="This confirms the admin accepts the service terms before we process onboarding information."
      >
        <div className="checkbox-row">
          <input
            type="checkbox"
            aria-invalid={Boolean(validation.errorFor("terms_accepted"))}
            aria-describedby={fieldErrorId("terms_accepted")}
            checked={form.terms_accepted}
            onChange={(event) =>
              setForm({ ...form, terms_accepted: event.target.checked })
            }
            onBlur={() => validation.validateField("terms_accepted")}
          />
          <span>
            I accept the{" "}
            <a href={TERMS_PATH} target="_blank" rel="noreferrer">
              Beta Terms of Service
            </a>
            .
          </span>
        </div>
      </Field>
      <button disabled={busy}>
        {busy ? "Sending..." : "Send account verification email"}
      </button>
    </form>
  );
}

function WebsiteVerificationScreen({ form, setForm, onSubmit, onBack, busy }) {
  const validation = useFormValidation(
    form,
    (values) =>
      validateWebsiteVerificationForm(values, {
        requireAdminEmailDomainMatch: REQUIRE_ADMIN_EMAIL_DOMAIN_MATCH,
      }),
  );
  function handleSubmit(event) {
    event.preventDefault();
    if (!validation.validateForSubmit()) return;
    onSubmit();
  }

  return (
    <form className="card form" onSubmit={handleSubmit} noValidate>
      <h2>Website verification</h2>
      <p>
        Verify a website-domain email so we know the onboarding request is tied to
        the business website.
      </p>
      <FormErrorSummary errors={validation.errors} ref={validation.summaryRef} />
      <Field
        label="Website URL"
        name="website_url"
        error={validation.errorFor("website_url")}
        help="We use this to identify the business, check for duplicate onboarding, and prepare an initial business profile."
      >
        <input
          aria-invalid={Boolean(validation.errorFor("website_url"))}
          aria-describedby={fieldErrorId("website_url")}
          value={form.website_url}
          onChange={(event) =>
            setForm({ ...form, website_url: event.target.value })
          }
          onBlur={() => validation.validateField("website_url")}
          placeholder="https://example.com"
        />
      </Field>
      <Field
        label="Website verification email"
        name="website_verification_email"
        error={validation.errorFor("website_verification_email")}
        help="We send a verification link here to confirm control of the website domain. This can also act as a secondary onboarding contact."
      >
        <input
          type="email"
          aria-invalid={Boolean(validation.errorFor("website_verification_email"))}
          aria-describedby={fieldErrorId("website_verification_email")}
          value={form.website_verification_email}
          onChange={(event) =>
            setForm({ ...form, website_verification_email: event.target.value })
          }
          onBlur={() => validation.validateField("website_verification_email")}
          placeholder="admin@example.com"
        />
      </Field>
      <NavButtons
        onBack={onBack}
        busy={busy}
        submitLabel="Send website verification email"
      />
    </form>
  );
}

function EmailVerificationScreen({
  session,
  kind,
  onBack,
  onReload,
  onResend,
  onContinue,
  busy,
  busyAction,
}) {
  const isUsername = kind === "username";
  const verified = isUsername
    ? session.username_email_verified
    : session.website_email_verified;
  const email = isUsername
    ? session.admin.username_email
    : session.website_verification_email;
  const title = isUsername ? "Verify account email" : "Verify website email";
  const verifiedCopy = isUsername
    ? `${email} has been verified. Continue to website verification.`
    : `${email} has been verified. Continue to analyze the business website.`;
  const pendingCopy = isUsername
    ? `We sent a verification link to ${email}. This wizard is paused until that inbox link is opened.`
    : `We sent a website verification link to ${email}. This wizard is paused until that inbox link is opened.`;
  const resendAction = isUsername ? "resend-username-email" : "resend-website-email";
  const continueLabel = isUsername ? "Continue" : "Continue";
  return (
    <section className="card">
      <h2>{title}</h2>
      <p>{verified ? verifiedCopy : pendingCopy}</p>
      <div className="nav">
        <button type="button" className="secondary" onClick={onBack} disabled={busy}>
          Back
        </button>
        <button type="button" className="secondary" onClick={onReload} disabled={busy}>
          Refresh status
        </button>
        {!verified && (
          <button type="button" className="secondary" onClick={onResend} disabled={busy}>
            {busyAction === resendAction ? "Sending..." : "Resend email"}
          </button>
        )}
        <button
          type="button"
          onClick={onContinue}
          disabled={busy || !verified}
        >
          {busyAction === "analyze-website" ? "Working..." : continueLabel}
        </button>
      </div>
    </section>
  );
}

function AnalyzingScreen({ session }) {
  return (
    <section className="card wait-card" aria-live="polite">
      <div className="spinner" aria-hidden="true" />
      <h2>Analyzing website</h2>
      <p>
        We are reviewing {session.website_url} and preparing the business profile,
        business summary, FAQ, and contact information found on the site.
      </p>
    </section>
  );
}

function AnalysisScreen({ session, onBack, onNext, busy }) {
  const [draft, setDraft] = useState(() => ({
    business_profile: {
      ...(session.business_profile || {}),
    },
    business_summary: session.business_summary || "",
  }));
  const validation = useFormValidation(draft, validateAnalysisDraft);

  function updateProfile(field, value) {
    setDraft((current) => ({
      ...current,
      business_profile: {
        ...current.business_profile,
        [field]: value,
      },
    }));
  }

  function updateDraft(field, value) {
    setDraft((current) => ({ ...current, [field]: value }));
  }

  function handleSubmit(event) {
    event.preventDefault();
    if (!validation.validateForSubmit()) return;
    onNext({
      business_profile: draft.business_profile,
      business_summary: draft.business_summary,
    });
  }

  return (
    <form className="card form" onSubmit={handleSubmit} noValidate>
      <h2>Analysis summary</h2>
      <p>This is the editable draft generated from the website.</p>
      <FormErrorSummary errors={validation.errors} ref={validation.summaryRef} />
      <Field
        label="Business name"
        name="business_profile.business_name"
        error={validation.errorFor("business_profile.business_name")}
        help="This becomes the tenant display name and the public business name used by the assistant."
      >
        <input
          aria-invalid={Boolean(
            validation.errorFor("business_profile.business_name"),
          )}
          aria-describedby={fieldErrorId("business_profile.business_name")}
          value={draft.business_profile.business_name || ""}
          onChange={(event) => updateProfile("business_name", event.target.value)}
          onBlur={() => validation.validateField("business_profile.business_name")}
        />
      </Field>
      <MarkdownField
        label="Business summary / FAQ"
        name="business_summary"
        error={validation.errorFor("business_summary")}
        help="This Markdown summary becomes business context for customer answers and can include frequently asked questions."
      >
        <MarkdownRichEditor
          id="business-summary-faq-editor"
          ariaLabel="Business summary / FAQ"
          aria-invalid={Boolean(validation.errorFor("business_summary"))}
          aria-describedby={fieldErrorId("business_summary")}
          value={draft.business_summary}
          onChange={(value) => updateDraft("business_summary", value)}
          onBlur={() => validation.validateField("business_summary")}
        />
      </MarkdownField>
      <NavButtons onBack={onBack} busy={busy} submitLabel="Review contact information" />
    </form>
  );
}

function MarkdownRichEditor({
  id,
  value,
  onChange,
  onBlur,
  ariaLabel,
  ...ariaProps
}) {
  const plugins = useMemo(
    () => [
      headingsPlugin(),
      listsPlugin(),
      linkPlugin(),
      markdownShortcutPlugin(),
      toolbarPlugin({
        toolbarContents: () => (
          <>
            <UndoRedo />
            <Separator />
            <BlockTypeSelect />
            <Separator />
            <BoldItalicUnderlineToggles />
            <Separator />
            <ListsToggle />
            <Separator />
            <CreateLink />
          </>
        ),
      }),
    ],
    [],
  );

  return (
    <div id={id} className="markdown-editor" onBlur={onBlur} {...ariaProps}>
      <MDXEditor
        aria-label={ariaLabel}
        markdown={value}
        onChange={(markdown) => onChange(markdown || "")}
        plugins={plugins}
        contentEditableClassName="markdown-editor-input"
      />
    </div>
  );
}

function BusinessScreen({ session, onBack, onNext, busy }) {
  const [profile, setProfile] = useState(session.business_profile);
  const validation = useFormValidation(profile || {}, validateBusinessProfile);
  function handleSubmit(event) {
    event.preventDefault();
    if (!validation.validateForSubmit()) return;
    onNext(profile);
  }

  return (
    <form className="card form" onSubmit={handleSubmit} noValidate>
      <h2>Business details</h2>
      <FormErrorSummary errors={validation.errors} ref={validation.summaryRef} />
      {["business_name", "location_name", "physical_location", "business_phone", "business_email"].map((field) => (
        <Field
          key={field}
          label={field.replaceAll("_", " ")}
          name={field}
          error={validation.errorFor(field)}
          help={businessFieldHelp(field)}
        >
          <input
            aria-invalid={Boolean(validation.errorFor(field))}
            aria-describedby={fieldErrorId(field)}
            value={profile?.[field] || ""}
            onChange={(event) =>
              setProfile({ ...profile, [field]: event.target.value })
            }
            onBlur={() => validation.validateField(field)}
          />
        </Field>
      ))}
      <Field
        label="Google place URL"
        name="google_place_url"
        error={validation.errorFor("google_place_url")}
        help="This lets the assistant share the approved map/location link without guessing."
      >
        <input
          aria-invalid={Boolean(validation.errorFor("google_place_url"))}
          aria-describedby={fieldErrorId("google_place_url")}
          value={profile?.google_place_url || ""}
          onChange={(event) =>
            setProfile({ ...profile, google_place_url: event.target.value || null })
          }
          onBlur={() => validation.validateField("google_place_url")}
        />
      </Field>
      <NavButtons onBack={onBack} busy={busy} submitLabel="Save contact information" />
    </form>
  );
}

function ContactInfoScreen({ session, onBack, onNext, busy }) {
  const [links, setLinks] = useState(() =>
    normalizeContactInfo(session.contact_info || [], session.website_url),
  );
  const validation = useFormValidation({ contact_info: links }, validateContactInfo);

  function updateLink(index, field, value) {
    setLinks((current) =>
      current.map((link, linkIndex) =>
        linkIndex === index ? { ...link, [field]: value } : link,
      ),
    );
  }

  function addLink() {
    setLinks((current) => [...current, blankSocialLink()]);
  }

  function removeLink(index) {
    setLinks((current) => {
      const next = current.filter((_link, linkIndex) => linkIndex !== index);
      return next.length ? next : [blankSocialLink()];
    });
  }

  function linksForSubmit() {
    return links
      .map(contactPointForSubmit)
      .filter((link) => link.kind || link.url || link.value);
  }

  function handleSubmit(event) {
    event.preventDefault();
    if (!validation.validateForSubmit()) return;
    onNext(linksForSubmit());
  }

  return (
    <form className="card form" onSubmit={handleSubmit} noValidate>
      <h2>Contact information</h2>
      <FormErrorSummary errors={validation.errors} ref={validation.summaryRef} />
      <p>
        Review the contact details found on the website, correct them, or add more
        manually.
      </p>
      <div className="repeater">
        {links.map((link, index) => {
          const baseName = `contact_info.${index}`;
          return (
            <fieldset className="repeater-row" key={link.id || index}>
              <legend className="sr-only">Contact point {index + 1}</legend>
              <div className="repeater-row-header">
                <span>Contact point {index + 1}</span>
                <button
                  type="button"
                  className="icon-button danger"
                  aria-label={`Delete contact point ${index + 1}`}
                  title="Delete contact point"
                  onClick={() => removeLink(index)}
                  disabled={busy}
                >
                  ×
                </button>
              </div>
              <Field
                label="Type"
                name={`${baseName}.kind`}
                error={validation.errorFor(`${baseName}.kind`)}
                help="Classifies the contact point, such as website, email, phone, WhatsApp, Facebook, or map."
              >
                <input
                  aria-invalid={Boolean(validation.errorFor(`${baseName}.kind`))}
                  aria-describedby={fieldErrorId(`${baseName}.kind`)}
                  value={link.kind}
                  placeholder="website, email, phone, whatsapp"
                  onChange={(event) => updateLink(index, "kind", event.target.value)}
                  onBlur={() => validation.validateField(`${baseName}.kind`)}
                />
              </Field>
              <Field
                label="Label"
                name={`${baseName}.label`}
                error={validation.errorFor(`${baseName}.label`)}
                help="A human-friendly name for this contact point so admins and customers can tell links apart."
              >
                <input
                  aria-invalid={Boolean(validation.errorFor(`${baseName}.label`))}
                  aria-describedby={fieldErrorId(`${baseName}.label`)}
                  value={link.label}
                  placeholder="Main phone, Instagram, WhatsApp"
                  onChange={(event) => updateLink(index, "label", event.target.value)}
                  onBlur={() => validation.validateField(`${baseName}.label`)}
                />
              </Field>
              <Field
                label="URL or value"
                name={`${baseName}.url`}
                error={validation.errorFor(`${baseName}.url`)}
                help="The exact contact value the assistant may share, such as a URL, email address, or phone number."
              >
                <input
                  aria-invalid={Boolean(validation.errorFor(`${baseName}.url`))}
                  aria-describedby={fieldErrorId(`${baseName}.url`)}
                  value={link.url}
                  placeholder="https://..., hello@example.com, +254..."
                  onChange={(event) => updateLink(index, "url", event.target.value)}
                  onBlur={() => validation.validateField(`${baseName}.url`)}
                />
              </Field>
            </fieldset>
          );
        })}
      </div>
      <button type="button" className="secondary" onClick={addLink} disabled={busy}>
        Add another contact
      </button>
      <NavButtons onBack={onBack} busy={busy} submitLabel="Submit for review" />
    </form>
  );
}

function WaitingForReviewScreen({ session, onBack }) {
  return (
    <section className="card form">
      <h2>Onboarding submitted for review</h2>
      <p>
        Thanks — your business details have been saved. Please wait for an email after
        the onboarding has been reviewed and the configuration is complete.
      </p>
      <p className="muted">
        We will send updates to{" "}
        {session.admin?.username_email || "the onboarding admin email"}.
      </p>
      <div className="nav">
        <button type="button" className="secondary" onClick={onBack}>
          Back
        </button>
      </div>
    </section>
  );
}

function TelegramScreen({ session, token, onBack, onSubmit, busy }) {
  const [form, setForm] = useState({
    token: token || "",
    bot_token: "",
  });
  const validation = useFormValidation(
    form,
    (values) => validateTelegramSetup(values, Boolean(token)),
  );
  function handleSubmit(event) {
    event.preventDefault();
    if (!validation.validateForSubmit()) return;
    onSubmit(form);
  }

  return (
    <form className="card form" onSubmit={handleSubmit} noValidate>
      <h2>SaaS-admin Telegram setup</h2>
      <FormErrorSummary errors={validation.errors} ref={validation.summaryRef} />
      {session.telegram_setup_url && (
        <p className="muted">Setup link prepared for SaaS admin.</p>
      )}
      <OnboardingSummary session={session} />
      {!token && (
        <Field
          label="One-time token"
          name="token"
          error={validation.errorFor("token")}
          help="This proves the Telegram setup form was opened from the signed SaaS-admin email link."
        >
          <input
            aria-invalid={Boolean(validation.errorFor("token"))}
            aria-describedby={fieldErrorId("token")}
            value={form.token}
            onChange={(event) => setForm({ ...form, token: event.target.value })}
            onBlur={() => validation.validateField("token")}
          />
        </Field>
      )}
      <Field
        label="Telegram bot token"
        name="bot_token"
        error={validation.errorFor("bot_token")}
        help="This token lets the app send customer replies through the tenant's Telegram bot."
      >
        <input
          aria-invalid={Boolean(validation.errorFor("bot_token"))}
          aria-describedby={fieldErrorId("bot_token")}
          value={form.bot_token}
          onChange={(event) => setForm({ ...form, bot_token: event.target.value })}
          onBlur={() => validation.validateField("bot_token")}
        />
      </Field>
      <NavButtons onBack={onBack} busy={busy} submitLabel="Save Telegram setup" />
    </form>
  );
}

function OnboardingSummary({ session }) {
  const profile = session.business_profile || {};
  const projects = session.provider_projects || {};
  const fields = [
    ["Website", session.website_url],
    ["Tenant admin", `${adminDisplayName(session.admin)} <${session.admin?.username_email || ""}>`],
    ["Admin phone", session.admin?.phone_number],
    ["Admin role/title", session.admin?.role_title],
    ["Business name", profile.business_name],
    ["Location name", profile.location_name],
    ["Physical location", profile.physical_location],
    ["Business phone", profile.business_phone],
    ["Business email", profile.business_email],
    ["Google place URL", profile.google_place_url],
    ["Business summary / FAQ", session.business_summary],
    ["AI provider project", projects.llm_project_name],
    ["LangSmith project", projects.langsmith_project],
  ].filter(([, value]) => value);

  return (
    <section className="summary-panel" aria-label="Onboarding summary">
      <h3>Onboarding summary</h3>
      <dl className="summary">
        {fields.map(([label, value]) => (
          <React.Fragment key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </React.Fragment>
        ))}
      </dl>
      {session.contact_info?.length > 0 && (
        <>
          <h4>Contact information</h4>
          <ul>
            {session.contact_info.map((link) => (
              <li key={`${link.kind}-${link.url || link.value}`}>
                {link.label || link.kind}: {link.url || link.value}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function CompletionScreen({ session, onBack, onSubmit, busy }) {
  return (
    <section className="card">
      <h2>Completion</h2>
      <p>Status: {session.status}</p>
      {session.submitted_job_id ? (
        <p>Provisioning job: {session.submitted_job_id}</p>
      ) : (
        <p>Review complete. Submit to create tenant records and configuration.</p>
      )}
      <NavButtons
        onBack={onBack}
        onNext={onSubmit}
        busy={busy}
        nextLabel={session.submitted_job_id ? "Refresh status" : "Submit provisioning"}
      />
    </section>
  );
}

const FormErrorSummary = React.forwardRef(function FormErrorSummary({ errors }, ref) {
  const errorList = Object.values(errors).filter(Boolean);
  if (!errorList.length) return null;

  return (
    <div
      className="form-error-summary"
      ref={ref}
      tabIndex="-1"
      role="alert"
      aria-live="assertive"
    >
      <h3>Please fix the following</h3>
      <ul>
        {errorList.map((error) => (
          <li key={error}>{error}</li>
        ))}
      </ul>
    </div>
  );
});

function Field({ label, name, error, help, children }) {
  return (
    <label>
      <span className="field-label">
        <span>{label}</span>
        {help && <InfoTooltip text={help} />}
      </span>
      {children}
      {error && (
        <small className="field-error" id={fieldErrorId(name)}>
          {error}
        </small>
      )}
    </label>
  );
}

function MarkdownField({ label, name, error, help, children }) {
  return (
    <div className="field">
      <span className="field-label">
        <span>{label}</span>
        {help && <InfoTooltip text={help} />}
      </span>
      {children}
      {error && (
        <small className="field-error" id={fieldErrorId(name)}>
          {error}
        </small>
      )}
    </div>
  );
}

function InfoTooltip({ text }) {
  return (
    <span className="info-tooltip" tabIndex="0" aria-label="Field help">
      i
      <span className="info-tooltip-bubble" role="tooltip">
        {text}
      </span>
    </span>
  );
}

function NavButtons({ onBack, onNext, busy, nextLabel = "Next", submitLabel }) {
  return (
    <div className="nav">
      {onBack && (
        <button type="button" className="secondary" onClick={onBack} disabled={busy}>
          Back
        </button>
      )}
      {onNext ? (
        <button type="button" onClick={onNext} disabled={busy}>
          {busy ? "Working..." : nextLabel}
        </button>
      ) : (
        <button disabled={busy}>{busy ? "Working..." : submitLabel || "Next"}</button>
      )}
    </div>
  );
}

function useFormValidation(values, validate) {
  const [touched, setTouched] = useState({});
  const [submitted, setSubmitted] = useState(false);
  const summaryRef = useRef(null);
  const allErrors = validate(values);
  const errors = Object.fromEntries(
    Object.entries(allErrors).filter(([field]) => submitted || touched[field]),
  );

  function validateField(field) {
    setTouched((current) => ({ ...current, [field]: true }));
  }

  function validateForSubmit() {
    setSubmitted(true);
    const hasErrors = Object.values(allErrors).some(Boolean);
    if (hasErrors) {
      window.requestAnimationFrame(() => summaryRef.current?.focus());
      return false;
    }
    return true;
  }

  function errorFor(field) {
    return errors[field] || "";
  }

  return {
    errors,
    errorFor,
    summaryRef,
    validateField,
    validateForSubmit,
  };
}

function validateAccountForm(form) {
  const errors = {};
  if (!form.username_email.trim()) {
    errors.username_email = "Username email is required.";
  } else if (!isValidEmail(form.username_email)) {
    errors.username_email = "Username email must be valid.";
  }

  if (!form.given_name.trim()) {
    errors.given_name = "Given name is required.";
  }

  if (!form.family_name.trim()) {
    errors.family_name = "Family name is required.";
  }

  if (!form.admin_phone_number.trim()) {
    errors.admin_phone_number = "Admin phone number is required.";
  } else if (!isValidAdminPhoneNumber(form.admin_phone_number)) {
    errors.admin_phone_number = (
      "Admin phone number must use international format, for example +254723921716."
    );
  }

  if (!form.admin_role_title.trim()) {
    errors.admin_role_title = "Admin role/title is required.";
  }

  if (!form.authority_confirmed) {
    errors.authority_confirmed = "Authority confirmation is required.";
  }

  if (!form.terms_accepted) {
    errors.terms_accepted = "Terms of service acceptance is required.";
  }

  return errors;
}

function validateWebsiteVerificationForm(
  form,
  { requireAdminEmailDomainMatch = true } = {},
) {
  const errors = {};
  let parsedWebsiteUrl = null;
  if (!form.website_url.trim()) {
    errors.website_url = "Website URL is required.";
  } else {
    try {
      parsedWebsiteUrl = new URL(normalizeWebsiteUrl(form.website_url));
      if (!["http:", "https:"].includes(parsedWebsiteUrl.protocol)) {
        errors.website_url = "Website URL must use HTTP or HTTPS.";
      }
    } catch {
      errors.website_url = "Website URL must be a valid URL.";
    }
  }

  if (!form.website_verification_email.trim()) {
    errors.website_verification_email = "Website verification email is required.";
  } else if (!isValidEmail(form.website_verification_email)) {
    errors.website_verification_email = "Website verification email must be valid.";
  } else if (
    requireAdminEmailDomainMatch &&
    !errors.website_url &&
    parsedWebsiteUrl
  ) {
    const websiteDomain = stripWww(parsedWebsiteUrl.hostname);
    const emailDomain = stripWww(form.website_verification_email.split("@").pop());
    if (!domainsMatch(emailDomain, websiteDomain)) {
      errors.website_verification_email = (
        "Website verification email domain must belong to the website domain."
      );
    }
  }

  return errors;
}

const validateStartForm = validateWebsiteVerificationForm;

function parseBooleanEnv(value, defaultValue) {
  if (value === undefined || value === null || value === "") return defaultValue;
  return !["0", "false", "no", "off"].includes(String(value).toLowerCase());
}

function validateBusinessProfile(profile) {
  const errors = {};
  for (const field of [
    "business_name",
    "location_name",
    "physical_location",
    "business_phone",
    "business_email",
  ]) {
    if (!String(profile?.[field] || "").trim()) {
      errors[field] = `${field.replaceAll("_", " ")} is required.`;
    }
  }

  if (profile?.business_email && !isValidEmail(profile.business_email)) {
    errors.business_email = "Business email must be valid.";
  }

  if (profile?.google_place_url && !isValidHttpUrl(profile.google_place_url)) {
    errors.google_place_url = "Google place URL must be a valid HTTP/HTTPS URL.";
  }

  return errors;
}

function validateAnalysisDraft(draft) {
  const errors = {};
  if (!String(draft.business_profile?.business_name || "").trim()) {
    errors["business_profile.business_name"] = "Business name is required.";
  }
  if (!draft.business_summary.trim()) {
    errors.business_summary = "Business summary / FAQ is required.";
  }
  return errors;
}

function validateContactInfo(values) {
  const errors = {};
  values.contact_info.forEach((link, index) => {
    const hasAnyValue = Boolean(
      link.kind.trim() || link.label.trim() || link.url.trim(),
    );
    if (!hasAnyValue) return;

    if (!link.kind.trim()) {
      errors[`contact_info.${index}.kind`] = "Type is required.";
    }
    if (!link.url.trim()) {
      errors[`contact_info.${index}.url`] = "Contact URL or value is required.";
    } else if (!isValidContactValue(link.url)) {
      errors[`contact_info.${index}.url`] = (
        "Enter a valid URL, email address, phone number, or contact URI."
      );
    }
  });
  return errors;
}

function validateTelegramSetup(form, tokenFromUrl) {
  const errors = {};
  if (!tokenFromUrl && !form.token.trim()) {
    errors.token = "One-time token is required.";
  }
  if (!form.bot_token.trim()) {
    errors.bot_token = "Telegram bot token is required.";
  }
  return errors;
}

function fieldErrorId(name) {
  return `${name}-error`;
}

function isValidEmail(value) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
}

function isValidAdminPhoneNumber(value) {
  const trimmed = String(value || "").trim();
  const parsed = parsePhoneNumberFromString(trimmed);
  return Boolean(trimmed.startsWith("+") && parsed?.isValid());
}

function isValidHttpUrl(value) {
  try {
    const parsed = new URL(normalizeWebsiteUrl(value));
    return ["http:", "https:"].includes(parsed.protocol);
  } catch {
    return false;
  }
}

function isValidContactValue(value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) return false;
  if (isValidEmail(trimmed)) return true;
  if (isLikelyPhoneNumber(trimmed)) return true;

  try {
    const parsed = new URL(normalizeContactUrl(trimmed));
    return Boolean(parsed.protocol);
  } catch {
    return false;
  }
}

function normalizeWebsiteUrl(value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) return trimmed;
  if (/^[a-z][a-z\d+\-.]*:\/\//i.test(trimmed)) return trimmed;
  return `https://${trimmed}`;
}

function normalizeContactUrl(value, kind = "") {
  const trimmed = unwrapMarkdownLink(value);
  if (!trimmed) return trimmed;
  if (/^[a-z][a-z\d+\-.]*:/i.test(trimmed)) return trimmed;
  if (isValidEmail(trimmed)) return `mailto:${trimmed}`;
  if (isLikelyPhoneNumber(trimmed)) return phoneContactUrl(trimmed, kind);
  if (isLikelyBareDomain(trimmed)) return normalizeWebsiteUrl(trimmed);
  return trimmed;
}

function contactPointForSubmit(link) {
  const kind = link.kind.trim();
  const label = link.label.trim() || kind;
  const rawContact = link.url.trim();
  const normalizedContact = normalizeContactUrl(rawContact, kind);
  const contact = { kind, label };

  if (normalizedContact && /^[a-z][a-z\d+\-.]*:/i.test(normalizedContact)) {
    contact.url = normalizedContact;
    if (
      !/^[a-z][a-z\d+\-.]*:/i.test(rawContact) &&
      (isValidEmail(rawContact) || isLikelyPhoneNumber(rawContact))
    ) {
      contact.value = rawContact;
    }
  } else if (normalizedContact) {
    contact.value = normalizedContact;
  }

  return contact;
}

function normalizeContactInfo(links, baseUrl = "") {
  const seen = new Set();
  const normalized = links
    .filter((link) => link.url || link.value)
    .map((link, index) => {
      const contactValue = unwrapMarkdownLink(link.url || link.value || "");
      return {
        id: `link-${index}-${link.kind || "contact"}`,
        kind: link.kind || "",
        label: link.label || link.kind || "",
        url: contactValue,
      };
    })
    .filter(
      (link) =>
        link.kind !== "website" ||
        !sameSiteUrl(link.url, baseUrl) ||
        isHomepageUrl(link.url),
    )
    .filter((link) => {
      const identity = contactValueIdentity(link.url);
      if (!identity || seen.has(identity)) return false;
      seen.add(identity);
      return true;
    });
  return normalized.length ? normalized : [blankSocialLink()];
}

function contactValueIdentity(value) {
  const trimmed = unwrapMarkdownLink(value);
  if (!trimmed) return "";

  try {
    const parsed = new URL(normalizeContactUrl(trimmed));
    if (["http:", "https:"].includes(parsed.protocol)) {
      const pathname = parsed.pathname.replace(/\/+$/, "");
      return (
        `${parsed.protocol.toLowerCase()}//${parsed.hostname.toLowerCase()}` +
        `${parsed.port ? `:${parsed.port}` : ""}${pathname}${parsed.search}${parsed.hash}`
      );
    }
    return parsed.toString().toLowerCase();
  } catch {
    return trimmed.toLowerCase().replace(/\s+/g, " ");
  }
}

function unwrapMarkdownLink(value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) return "";
  const match = trimmed.match(/^\[([^\]]*)\]\(([^)]+)\)$/);
  return match ? match[2].trim() : trimmed;
}

function sameSiteUrl(url, baseUrl) {
  if (!url || !baseUrl) return false;
  try {
    const parsed = new URL(normalizeContactUrl(url));
    const base = new URL(normalizeWebsiteUrl(baseUrl));
    return stripWww(parsed.hostname) === stripWww(base.hostname);
  } catch {
    return false;
  }
}

function isHomepageUrl(url) {
  try {
    const parsed = new URL(normalizeContactUrl(url));
    return parsed.pathname.replace(/\/+$/, "") === "" && !parsed.search && !parsed.hash;
  } catch {
    return false;
  }
}

function blankSocialLink() {
  return {
    id: `new-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    kind: "",
    label: "",
    url: "",
  };
}

function isLikelyBareDomain(value) {
  return /^[^\s@]+\.[^\s@]+([/?#].*)?$/i.test(value);
}

function isLikelyPhoneNumber(value) {
  return Boolean(parsePhoneNumberFromString(String(value || "").trim())?.isValid());
}

function phoneContactUrl(value, kind = "") {
  const parsed = parsePhoneNumberFromString(String(value || "").trim());
  const e164 = parsed?.number || String(value || "").replace(/[^\d+]/g, "");
  if (kind.trim().toLowerCase() === "whatsapp") {
    return `https://wa.me/${e164.replace(/^\+/, "")}`;
  }
  return `tel:${e164}`;
}

function adminDisplayName(admin) {
  return [admin?.given_name, admin?.family_name].filter(Boolean).join(" ");
}

function stripWww(value) {
  return String(value || "").trim().toLowerCase().replace(/^www\./, "");
}

function domainsMatch(emailDomain, websiteDomain) {
  return (
    emailDomain === websiteDomain ||
    emailDomain.endsWith(`.${websiteDomain}`) ||
    websiteDomain.endsWith(`.${emailDomain}`)
  );
}

function readApiError(body) {
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) {
    return body.detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
  }
  return "Request failed.";
}

function businessFieldHelp(field) {
  const help = {
    business_name: "The approved business name the assistant uses with customers.",
    location_name: "A short name for the business location, branch, or service area.",
    physical_location: "The address or location description customers can rely on.",
    business_phone: "The approved phone number the assistant may share with customers.",
    business_email: "The approved business email the assistant may share with customers.",
  };
  return help[field] || "Review and correct this business profile field.";
}

function stepFromSession(session) {
  if (session.current_step === "business") return "contact";
  if (session.current_step === "social") return "contact";
  if (session.current_step === "telegram-setup") return "awaiting-review";
  if (session.current_step === "submit") return "complete";
  if (session.current_step === "start") return "account";
  if (session.current_step === "email-verification") return "username-email-verification";
  return session.current_step || "account";
}

function replaceUrlWithResumeLink(sessionId) {
  const params = new URLSearchParams({ session_id: sessionId });
  window.history.replaceState({}, "", `/?${params.toString()}`);
}

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(<App />);
}

export {
  App,
  ContactInfoScreen,
  StartScreen,
  TelegramScreen,
  TermsScreen,
  WebsiteVerificationScreen,
  isValidAdminPhoneNumber,
  isValidHttpUrl,
  normalizeWebsiteUrl,
  validateAccountForm,
  validateStartForm,
  validateWebsiteVerificationForm,
};
