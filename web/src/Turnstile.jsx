import React, { useEffect, useRef, useState } from "react";

const SCRIPT_URL = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
let loading;
function loadTurnstile() {
  if (window.turnstile) return Promise.resolve(window.turnstile);
  if (!loading) {
    loading = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = SCRIPT_URL;
      script.async = true;
      const fail = () => {
        window.clearTimeout(timeout);
        script.remove();
        loading = null;
        reject(new Error("Security verification could not load."));
      };
      const timeout = window.setTimeout(fail, 15000);
      script.onload = () => {
        window.clearTimeout(timeout);
        if (!window.turnstile) { fail(); return; }
        resolve(window.turnstile);
      };
      script.onerror = fail;
      document.head.appendChild(script);
    });
  }
  return loading;
}

export default function Turnstile({ siteKey, onToken }) {
  const element = useRef(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let cancelled = false;
    let widget;
    onToken("");
    setError("");
    if (!siteKey) return;
    loadTurnstile().then((api) => {
      if (cancelled) return;
      widget = api.render(element.current, {
        sitekey: siteKey,
        action: "onboarding",
        size: "flexible",
        callback: (token) => { setError(""); onToken(token); },
        "expired-callback": () => { onToken(""); setError("Verification expired. Please retry."); },
        "error-callback": () => { onToken(""); setError("Security verification failed. Please retry."); },
      });
    }).catch(() => {
      if (!cancelled) setError("Security verification could not load. Please retry.");
    });
    return () => {
      cancelled = true;
      if (widget !== undefined) window.turnstile?.remove(widget);
    };
  }, [siteKey, retry, onToken]);
  return <section aria-label="Security verification">
    <div ref={element} />
    {!siteKey && <p role="status">Security verification is not ready. Please try again later.</p>}
    {error && <><p role="alert">{error}</p><button type="button" onClick={() => setRetry((value) => value + 1)}>Retry security verification</button></>}
  </section>;
}
