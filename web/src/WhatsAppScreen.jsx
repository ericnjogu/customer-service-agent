import React, { useEffect, useRef, useState } from "react";

const META_ORIGINS = new Set(["https://www.facebook.com", "https://web.facebook.com"]);

export function parseSignupEvent(event) {
  if (!META_ORIGINS.has(event.origin)) return null;
  try {
    const data = typeof event.data === "string" ? JSON.parse(event.data) : event.data;
    if (data?.type !== "WA_EMBEDDED_SIGNUP") return null;
    if (!["FINISH", "CANCEL", "ERROR"].includes(data.event)) return null;
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
  const [connecting, setConnecting] = useState(false);
  const [generation, setGeneration] = useState(0);
  const current = useRef(null);
  const apiRef = useRef(api);
  apiRef.current = api;
  const base = `/onboarding/sessions/${session.session_id}/whatsapp`;

  useEffect(() => {
    let disposed = false;
    const attempt = { active: false, code: null, assets: null, sent: false, parameters: null };
    current.current = attempt;
    setReady(false);
    setConnecting(false);
    async function prepare() {
      if (!config.whatsapp_enabled) return;
      try {
        const status = await apiRef.current(`${base}/status`);
        if (disposed) return;
        setConnection(status);
        if (["connected", "active"].includes(status.status)) return;
        await loadMeta(config);
        attempt.parameters = await apiRef.current(`${base}/start`, { method: "POST" });
        if (!disposed) setReady(true);
      } catch (error) { if (!disposed) setMessage(error.message); }
    }
    prepare();
    async function finish() {
      if (!attempt.active || attempt.sent || !attempt.code || !attempt.assets) return;
      attempt.sent = true;
      try {
        const status = await apiRef.current(`${base}/complete`, { method: "POST", body: JSON.stringify({
          attempt_id: attempt.parameters.attempt_id, state: attempt.parameters.state,
          code: attempt.code, ...attempt.assets,
        }) });
        if (!disposed) { setConnection(status); setMessage("WhatsApp connected and verified."); }
      } catch (error) { if (!disposed) setMessage(error.message); }
      finally { attempt.code = null; attempt.active = false; if (!disposed) setConnecting(false); }
    }
    attempt.finish = finish;
    function receive(event) {
      if (!attempt.active || disposed) return;
      const data = parseSignupEvent(event);
      if (!data) return;
      if (data.event !== "FINISH") {
        attempt.active = false;
        attempt.code = null;
        setConnecting(false);
        setReady(false);
        setMessage(data.event === "CANCEL" ? "Connection cancelled. You can retry."
          : "Meta could not complete signup. Please retry.");
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

  function connect() {
    const attempt = current.current;
    if (!ready || !attempt?.parameters) return;
    attempt.active = true;
    setConnecting(true); setReady(false); setMessage("Complete the Meta signup window.");
    window.FB.login((response) => {
      if (current.current !== attempt || !attempt.active) return;
      const code = response.authResponse?.code;
      if (!code) {
        attempt.active = false; setConnecting(false);
        setMessage("Authorization was cancelled or declined. You can retry.");
        return;
      }
      attempt.code = code;
      attempt.finish();
    }, { config_id: config.meta_signup_configuration_id, response_type: "code",
      override_default_response_type: true,
      extras: { setup: {}, sessionInfoVersion: "3" } });
  }

  const connected = ["connected", "active"].includes(connection?.status);
  return <section className="card">
    <h2>Connect WhatsApp</h2>
    <p>Authorize Ristoh CSS through Meta to access your WhatsApp Business account, register
      your selected number, and send and receive customer messages. Connect one number for this business.</p>
    <p>Automated replies begin only after you submit and provisioning succeeds.</p>
    {!config.whatsapp_enabled && <p role="alert">WhatsApp signup is not available yet. Your draft is saved.</p>}
    <p role="status" aria-live="polite">{message}</p>
    {connected ? <p>Verified connected number: <strong>{connection.display_number}</strong></p>
      : <button type="button" onClick={connect} disabled={!ready || connecting || busy}>Connect with Meta</button>}
    {!connected && !connecting && config.whatsapp_enabled &&
      <button type="button" onClick={() => { setMessage(""); setGeneration(generation + 1); }}>Retry connection</button>}
    <div className="actions">
      <button type="button" onClick={onBack} disabled={connecting || busy}>Back</button>
      <button type="button" onClick={onSubmit} disabled={!connected || busy}>Submit and provision</button>
    </div>
  </section>;
}
