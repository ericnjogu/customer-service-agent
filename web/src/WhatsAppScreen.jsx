import React, { useEffect, useRef, useState } from "react";

const META_ORIGINS = new Set(["https://www.facebook.com", "https://web.facebook.com"]);

export function parseSignupEvent(event) {
  if (!META_ORIGINS.has(event.origin)) return null;
  try {
    const data = typeof event.data === "string" ? JSON.parse(event.data) : event.data;
    if (data?.type !== "WA_EMBEDDED_SIGNUP") return null;
    if (!["FINISH", "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING", "CANCEL", "ERROR"].includes(data.event)) return null;
    return data;
  } catch { return null; }
}

async function loadMeta(config) {
  if (!window.FB) {
    await new Promise((resolve, reject) => {
      let script = document.getElementById("meta-signup-sdk");
      const isNew = !script;
      if (!script) {
        script = document.createElement("script");
        script.id = "meta-signup-sdk";
        script.src = "https://connect.facebook.net/en_US/sdk.js";
        script.async = true;
      }
      function cleanup() {
        clearTimeout(timer);
        script.removeEventListener("load", loaded);
        script.removeEventListener("error", failed);
      }
      function loaded() { cleanup(); resolve(); }
      function failed() {
        cleanup(); script.remove();
        reject(new Error("Meta could not load. Please retry."));
      }
      const timer = setTimeout(failed, 15000);
      script.addEventListener("load", loaded, { once: true });
      script.addEventListener("error", failed, { once: true });
      if (isNew) document.head.appendChild(script);
    });
  }
  window.FB.init({ appId: config.meta_app_id, version: config.meta_graph_api_version,
    autoLogAppEvents: false, xfbml: false });
}

export default function WhatsAppScreen({ session, config, api, onSubmit, onBack, busy }) {
  const [connection, setConnection] = useState(null);
  const [message, setMessage] = useState("");
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [generation, setGeneration] = useState(0);
  const [archive, setArchive] = useState(null);
  const [preparing, setPreparing] = useState(false);
  const [driveBusy, setDriveBusy] = useState(false);
  const [needsBrowser, setNeedsBrowser] = useState(false);
  const [browserCode, setBrowserCode] = useState("");
  const [codeSent, setCodeSent] = useState(false);
  const [recoveryBusy, setRecoveryBusy] = useState(false);
  const callbackHandled = useRef(false);
  const current = useRef(null);
  const apiRef = useRef(api);
  apiRef.current = api;
  const base = `/onboarding/sessions/${session.session_id}/whatsapp`;
  const sessionBase = `/onboarding/sessions/${session.session_id}`;

  useEffect(() => {
    function restorePage(event) {
      if (!event.persisted) return;
      // Browser Back may restore the page with the OAuth navigation still busy.
      // Reload authoritative preferences without starting either provider flow.
      setDriveBusy(false);
      setGeneration((value) => value + 1);
    }
    window.addEventListener("pageshow", restorePage);
    return () => window.removeEventListener("pageshow", restorePage);
  }, []);

  useEffect(() => {
    let disposed = false;
    const attempt = { active: false, code: null, assets: null, sent: false, parameters: null };
    current.current = attempt;
    setReady(false);
    setConnecting(false);
    setFailed(false);
    setConnection(null);
    setMessage("Loading connection status…");
    async function prepare() {
      if (!config.whatsapp_enabled) return;
      try {
        const status = await apiRef.current(`${base}/status`);
        if (disposed) return;
        setConnection(status);
        const archiveStatus = await apiRef.current(`${sessionBase}/archive`);
        if (!disposed) { setArchive(archiveStatus); setMessage(""); }
        const fragment = new URLSearchParams(window.location.hash.slice(1));
        if (fragment.has("drive_state") && !callbackHandled.current) {
          callbackHandled.current = true;
          window.history.replaceState(null, "", window.location.pathname + window.location.search);
          if (fragment.get("drive_error")) {
            setMessage("Google authorization was cancelled. Connect Google Drive to retry.");
            return;
          }
          setDriveBusy(true);
          try {
            const saved = await apiRef.current(`${sessionBase}/drive/complete`, { method: "POST", body: JSON.stringify({
              state: fragment.get("drive_state"), code: fragment.get("drive_code"),
            }) });
            if (!disposed) setArchive(saved);
          } catch (error) { if (!disposed) setMessage(error.message); }
          finally { if (!disposed) setDriveBusy(false); }
        }
      } catch (error) { if (!disposed) {
        setMessage(error.message); setFailed(true);
        if (error.code === "browser_verification_required") setNeedsBrowser(true);
      } }
    }
    prepare();
    async function finish() {
      if (!attempt.active || attempt.sent || !attempt.code || !attempt.assets || !attempt.parameters) return;
      attempt.sent = true;
      try {
        const status = await apiRef.current(`${base}/complete`, { method: "POST", body: JSON.stringify({
          attempt_id: attempt.parameters.attempt_id, state: attempt.parameters.state,
          code: attempt.code, ...attempt.assets,
        }) });
        if (!disposed) { setConnection(status); setMessage("WhatsApp connected and verified."); }
      } catch (error) { if (!disposed) { setMessage(error.message); setFailed(true); await releaseAttempt(attempt); } }
      finally { attempt.code = null; attempt.active = false; if (!disposed) setConnecting(false); }
    }
    attempt.finish = finish;
    function receive(event) {
      if (!attempt.active || disposed) return;
      const data = parseSignupEvent(event);
      if (!data) return;
      if (!["FINISH", "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"].includes(data.event)) {
        attempt.active = false;
        attempt.code = null;
        setConnecting(false);
        setReady(false);
        setFailed(true);
        setMessage(data.event === "CANCEL" ? "Connection cancelled. You can retry."
          : "Meta could not complete signup. Please retry.");
        releaseAttempt(attempt);
        return;
      }
      const { waba_id, phone_number_id } = data.data || {};
      if (!/^\d{1,32}$/.test(waba_id || "") || !/^\d{1,32}$/.test(phone_number_id || "")) return;
      attempt.assets = { waba_id, phone_number_id };
      finish();
    }
    window.addEventListener("message", receive);
    return () => {
      disposed = true; attempt.active = false; attempt.code = null;
      window.removeEventListener("message", receive);
    };
  }, [session.session_id, config.whatsapp_enabled, generation]);

  async function releaseAttempt(attempt) {
    setPreparing(true);
    try {
      await attempt.startPromise;
      if (!attempt.parameters) return;
      const result = await apiRef.current(`${base}/cancel`, { method: "POST",
        body: JSON.stringify({ attempt_id: attempt.parameters.attempt_id, state: attempt.parameters.state }) });
      if (current.current === attempt) setArchive((value) => ({ ...value, locked: result.locked }));
    } catch {
      if (current.current === attempt) setMessage("Could not release signup settings. Please retry connection.");
    } finally { if (current.current === attempt) setPreparing(false); }
  }

  async function prepareConnection() {
    if (!archive) { setGeneration((value) => value + 1); return; }
    Object.assign(current.current, { active: false, code: null, assets: null, sent: false, parameters: null });
    setPreparing(true); setFailed(false); setMessage("Preparing WhatsApp connection…");
    try {
      await loadMeta(config);
      setReady(true); setMessage("");
    } catch (error) { setMessage(error.message); setFailed(true); }
    finally { setPreparing(false); }
  }

  useEffect(() => {
    if (config.whatsapp_enabled && archive && connection && !needsBrowser && !driveBusy &&
        !preparing && !ready && !failed && !connecting &&
        !["connected", "active"].includes(connection.status) &&
        (!archive.import_enabled || archive.drive_status === "ready")) {
      prepareConnection();
    }
  }, [config.whatsapp_enabled, archive, connection, needsBrowser, driveBusy, preparing, ready, failed, connecting]);

  async function connectDrive() {
    setDriveBusy(true); setMessage("");
    try {
      const result = await apiRef.current(`${sessionBase}/drive/start`, { method: "POST" });
      window.location.assign(result.authorization_url);
    } catch (error) { setMessage(error.message); setDriveBusy(false); }
  }

  async function chooseImport(event) {
    const enabled = event.target.checked;
    setReady(false);
    const previous = archive;
    setArchive((value) => ({ ...value, import_enabled: enabled }));
    setDriveBusy(true); setMessage("");
    try {
      const saved = await apiRef.current(`${sessionBase}/archive`, { method: "PATCH",
        body: JSON.stringify({ import_enabled: enabled }) });
      setArchive(saved);
      if (enabled && saved.drive_status !== "ready") { await connectDrive(); return; }
    } catch (error) { setArchive(previous); setMessage(error.message); }
    setDriveBusy(false);
  }

  useEffect(() => {
    if (!archive?.import_enabled || driveBusy) return;
    let disposed = false;
    const timer = setInterval(async () => {
      try {
        const value = await apiRef.current(`${sessionBase}/archive`);
        if (!disposed) setArchive(value);
      } catch { /* A failed status poll must not interrupt WhatsApp setup. */ }
    }, 5000);
    return () => { disposed = true; clearInterval(timer); };
  }, [session.session_id, archive?.import_enabled, driveBusy]);

  function connect() {
    const attempt = current.current;
    if (!ready || !attempt || driveBusy || (archive?.import_enabled && archive.drive_status !== "ready")) return;
    attempt.active = true;
    setConnecting(true); setReady(false); setMessage("Complete the Meta signup window.");
    // Start the server attempt in parallel; do not await before opening the popup.
    attempt.startPromise = apiRef.current(`${base}/start`, { method: "POST" }).then((parameters) => {
      attempt.parameters = parameters;
      if (current.current !== attempt || !attempt.active) return;
      setArchive((value) => ({ ...value, locked: true }));
      attempt.finish();
    }).catch((error) => {
      if (current.current !== attempt || !attempt.active) return;
      attempt.active = false; attempt.code = null;
      setConnecting(false); setFailed(true); setMessage(error.message);
      if (error.code === "browser_verification_required") setNeedsBrowser(true);
    });
    window.FB.login((response) => {
      if (current.current !== attempt || !attempt.active) return;
      const code = response.authResponse?.code;
      if (!code) {
        attempt.active = false; setConnecting(false);
        setFailed(true);
        setMessage("Authorization was cancelled or declined. You can retry.");
        releaseAttempt(attempt);
        return;
      }
      attempt.code = code;
      attempt.finish();
    }, { config_id: config.meta_signup_configuration_id, response_type: "code",
      override_default_response_type: true,
      extras: { setup: {}, version: "v4", sessionInfoVersion: "3", featureType: "whatsapp_business_app_onboarding" } });
  }

  const connected = ["connected", "active"].includes(connection?.status);
  if (needsBrowser) return <section className="card">
    <h2>Verify this browser</h2>
    <p>Your account is already verified and your draft is saved. Send a fresh code to your existing account email to continue in this browser.</p>
    <p role="status" aria-live="polite">{message}</p>
    <button type="button" disabled={recoveryBusy} onClick={async () => {
      setRecoveryBusy(true);
      try {
        await api(`${sessionBase}/browser/send-code`, { method: "POST" });
        setCodeSent(true); setMessage("Code sent to your account email. It expires in 10 minutes.");
      } catch (error) { setMessage(error.message); }
      finally { setRecoveryBusy(false); }
    }}>{codeSent ? "Resend browser verification code" : "Send browser verification code"}</button>
    {codeSent && <form onSubmit={async (event) => {
      event.preventDefault(); setRecoveryBusy(true);
      try {
        await api(`${sessionBase}/browser/verify-code`, { method: "POST", body: JSON.stringify({ code: browserCode }) });
        setNeedsBrowser(false); setBrowserCode(""); setCodeSent(false);
        setGeneration((value) => value + 1);
      } catch (error) { setMessage(error.message); }
      finally { setRecoveryBusy(false); }
    }}>
      <label>Browser verification code<input inputMode="numeric" autoComplete="one-time-code"
        pattern="[0-9]{6}" maxLength={6} required value={browserCode}
        onChange={(event) => setBrowserCode(event.target.value)} /></label>
      <button type="submit" disabled={recoveryBusy}>Verify and resume</button>
    </form>}
  </section>;
  return <section className="card">
    <h2>Connect WhatsApp</h2>
    <p>Connect your existing WhatsApp Business app number through Meta. You can keep using
      the app while Ristoh CSS sends and receives messages on the same number.</p>

    {!config.whatsapp_enabled && <p role="alert">WhatsApp signup is not available yet. Your draft is saved.</p>}
    {config.whatsapp_enabled && (failed && message
      ? <div className="form-error-summary" role="alert">
          <strong>Connection unsuccessful</strong>
          <p>{message}</p>
        </div>
      : <p role="status" aria-live="polite">{message}</p>)}
    {config.whatsapp_enabled && archive && <fieldset disabled={archive.locked || preparing || connecting || driveBusy || busy}>
      <legend>Optional chat archive</legend>
      <label className="checkbox-row"><input type="checkbox" checked={Boolean(archive.import_enabled)}
        disabled={!archive.available} onChange={chooseImport} /><span>Import historical chats and media</span></label>
      <p>Connect your Google Drive to archive available historical chats and media, and future WhatsApp media.
        You must also approve history sharing in Meta. Transfers run in the background; no content is extracted or added to the knowledge base.</p>
      {!archive.available && <p>Google Drive archival is not configured. You can continue without importing history.</p>}
    </fieldset>}
    {archive?.import_enabled && <div className="nav whatsapp-actions">
      <button type="button" className="secondary" disabled={driveBusy || preparing || connecting || busy}
        onClick={connectDrive}>{driveBusy ? "Connecting Google Drive…" : "Connect Google Drive"}</button>
      {archive.drive_status === "ready" && <span role="status">✓ <a href={archive.folder_url} target="_blank" rel="noopener noreferrer">{archive.folder_name}</a></span>}
      {archive.drive_status !== "ready" && !driveBusy && <p>Google Drive not connected. Connect Google Drive to retry, or uncheck the import option to continue without an archive.</p>}
      {archive.error_code && <p role="alert">Archive needs attention: {archive.error_code}</p>}
      {archive.drive_status === "ready" && Boolean(archive.transfers?.failed) && <button type="button"
        className="secondary" disabled={driveBusy || busy} onClick={async () => {
          try { setArchive(await apiRef.current(`${sessionBase}/archive/retry`, { method: "POST" })); }
          catch (error) { setMessage(error.message); }
        }}>Retry failed transfers</button>}
      <p>History: {archive.history_status || "not_requested"}. Only media available from Meta can be archived.</p>
      <p>Media transfers: {archive.transfers?.complete || 0} complete, {archive.transfers?.pending || 0} pending, {archive.transfers?.failed || 0} failed.</p>
    </div>}
    {connected && <p>Verified connected number: <strong>{connection.display_number}</strong></p>}
    <div className="nav whatsapp-actions">
      <button type="button" className="secondary" onClick={onBack} disabled={connecting || preparing || driveBusy || busy}>Back</button>
      {config.whatsapp_enabled && (connected
        ? <button type="button" onClick={onSubmit} disabled={busy}>{busy ? "Submitting…" : "Submit and provision"}</button>
        : connecting
          ? <button type="button" disabled>Connecting…</button>
          : ready
            ? <button type="button" onClick={connect} disabled={busy || driveBusy || (archive?.import_enabled && archive.drive_status !== "ready")}>Connect WhatsApp</button>
            : <button type="button" onClick={prepareConnection}
                disabled={busy || preparing || driveBusy || !failed || (archive?.import_enabled && archive.drive_status !== "ready")}>
                {preparing ? "Preparing…" : failed ? "Retry connection" : "Connect WhatsApp"}</button>)}
    </div>
  </section>;
}
