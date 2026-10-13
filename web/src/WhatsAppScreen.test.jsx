import React from "react";
import { afterEach, expect, test, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import WhatsAppScreen, { parseSignupEvent } from "./WhatsAppScreen";

afterEach(() => { cleanup(); delete window.FB; vi.restoreAllMocks(); });

test("redirect preparation does not load the SDK or start authorization", async () => {
  window.FB = { init: vi.fn(), login: vi.fn() };
  const api = vi.fn(async (path) => path.endsWith("/status")
    ? { status: "not_connected" } : { import_enabled: false, locked: false });
  render(<WhatsAppScreen session={{ session_id: "session" }}
    config={{ whatsapp_enabled: true, whatsapp_transport: "redirect", whatsapp_mode: "cloud_api" }}
    api={api} onSubmit={vi.fn()} onBack={vi.fn()} busy={false} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Connect WhatsApp" })).toBeEnabled());
  expect(window.FB.init).not.toHaveBeenCalled();
  expect(window.FB.login).not.toHaveBeenCalled();
  expect(api.mock.calls.map(([path]) => path)).toEqual([
    "/onboarding/sessions/session/whatsapp/status", "/onboarding/sessions/session/archive",
  ]);
  api.mockRejectedValue(new Error("Connection temporarily unavailable"));
  await userEvent.click(screen.getByRole("button", { name: "Connect WhatsApp" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Connection temporarily unavailable");
  expect(api).toHaveBeenLastCalledWith("/onboarding/sessions/session/whatsapp/redirect/start", { method: "POST" });
  await userEvent.click(screen.getByRole("button", { name: "Retry connection" }));
  expect(api.mock.calls.filter(([path]) => path.endsWith("/redirect/start"))).toHaveLength(2);
});

function setup(mode = "coexistence") {
  let callback;
  window.FB = { init: vi.fn(), login: vi.fn((fn) => { callback = fn; }) };
  const api = vi.fn(async (path) => {
    if (path.endsWith("/cancel")) return { locked: false };
    if (path.endsWith("/status")) return { status: "not_connected" };
    if (path.endsWith("/archive")) return { import_enabled: false, available: true, locked: false };
    if (path.endsWith("/start")) return { attempt_id: "attempt", state: "browser-state" };
    if (path.endsWith("/complete")) return { status: "connected", display_number: "+254700000001" };
  });
  const submit = vi.fn();
  render(<WhatsAppScreen session={{ session_id: "session" }} config={{ whatsapp_enabled: true,
    whatsapp_mode: mode,
    meta_app_id: "123", meta_signup_configuration_id: "config", meta_graph_api_version: "v25.0" }}
    api={api} onSubmit={submit} onBack={vi.fn()} busy={false} />);
  return { api, submit, authorize: () => callback({ authResponse: { code: "one-time-code" } }) };
}

test.each(["cloud_api", null])("Cloud API launch omits coexistence and history (mode=%s)", async (mode) => {
  const { authorize } = setup(mode);
  await prepareMeta();
  expect(screen.queryByText("Import historical chats and media")).not.toBeInTheDocument();
  expect(screen.getByText(/registered WhatsApp Cloud API number/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Connect WhatsApp" }));
  expect(window.FB.login.mock.calls[0][1].extras).not.toHaveProperty("featureType");
  await act(async () => { authorize(); event("FINISH"); });
  expect(await screen.findByText("+254700000001")).toBeInTheDocument();
});

function event(eventName = "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING", origin = "https://www.facebook.com") {
  window.dispatchEvent(new MessageEvent("message", { origin, data: JSON.stringify({
    type: "WA_EMBEDDED_SIGNUP", event: eventName,
    data: { waba_id: "456", phone_number_id: "789" },
  }) }));
}

async function prepareMeta() {
  await waitFor(() => expect(screen.getByRole("button", { name: "Connect WhatsApp" })).toBeEnabled());
  expect(window.FB.init).toHaveBeenCalled();
  expect(window.FB.login).not.toHaveBeenCalled();
}

test.each([true, false])("requires both Meta results and backend confirmation; code first=%s", async (codeFirst) => {
  const { api, submit, authorize } = setup();
  expect(screen.queryByRole("button", { name: "Submit and provision" })).not.toBeInTheDocument();
  await prepareMeta();
  expect(screen.queryByRole("button", { name: "Retry connection" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Connect WhatsApp" }));
  expect(window.FB.login.mock.calls[0][1].extras).toEqual({
    setup: {}, version: "v4", sessionInfoVersion: "3",
    featureType: "whatsapp_business_app_onboarding",
  });
  expect(screen.getByRole("button", { name: "Connecting…" })).toBeDisabled();
  await act(async () => { if (codeFirst) authorize(); else event(); });
  expect(api.mock.calls.filter(([path]) => path.endsWith("/complete"))).toHaveLength(0);
  await act(async () => { if (codeFirst) event(); else authorize(); });
  expect(await screen.findByText("+254700000001")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Submit and provision" }));
  expect(submit).toHaveBeenCalledOnce();
  const completion = api.mock.calls.find(([path]) => path.endsWith("/complete"));
  expect(JSON.parse(completion[1].body)).toEqual({ attempt_id: "attempt", state: "browser-state",
    code: "one-time-code", waba_id: "456", phone_number_id: "789" });
  await act(async () => event());
  expect(api.mock.calls.filter(([path]) => path.endsWith("/complete"))).toHaveLength(1);
});

test("rejects untrusted origins and supports cancellation", async () => {
  const { api, authorize } = setup();
  await prepareMeta();
  await userEvent.click(screen.getByRole("button", { name: "Connect WhatsApp" }));
  await act(async () => { authorize(); event("FINISH", "https://www.facebook.com.attacker.test"); });
  expect(api.mock.calls.some(([path]) => path.endsWith("/complete"))).toBe(false);
  await act(async () => event("CANCEL"));
  expect(screen.getByText("Connection cancelled. You can retry.")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("checkbox")).toBeEnabled());
  expect(screen.queryByRole("button", { name: "Connect WhatsApp" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Retry connection" }));
  expect(window.FB.login).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("button", { name: "Connecting…" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Retry connection" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Submit and provision" })).not.toBeInTheDocument();
});

test("retry clears failed completion data and completes a fresh signup in one click", async () => {
  const { api, authorize } = setup();
  await prepareMeta();
  const original = api.getMockImplementation();
  let failCompletion = true;
  api.mockImplementation(async (path, options) => {
    if (path.endsWith("/complete") && failCompletion) throw new Error("Try again");
    return original(path, options);
  });
  await userEvent.click(screen.getByRole("button", { name: "Connect WhatsApp" }));
  await act(async () => { authorize(); event(); });
  await waitFor(() => expect(screen.getByRole("button", { name: "Retry connection" })).toBeEnabled());
  failCompletion = false;
  await userEvent.click(screen.getByRole("button", { name: "Retry connection" }));
  expect(window.FB.login).toHaveBeenCalledTimes(2);
  await act(async () => authorize());
  expect(api.mock.calls.filter(([path]) => path.endsWith("/complete"))).toHaveLength(1);
  await act(async () => event());
  expect(await screen.findByText("+254700000001")).toBeInTheDocument();
});

test("preloads without locking preferences; completion waits for the server attempt", async () => {
  const { api, authorize } = setup();
  await prepareMeta();
  expect(api.mock.calls.some(([path]) => path.endsWith("/start"))).toBe(false);
  expect(screen.getByRole("checkbox")).toBeEnabled();
  const original = api.getMockImplementation();
  let resolveStart;
  api.mockImplementation((path, options) => path.endsWith("/start")
    ? new Promise((resolve) => { resolveStart = resolve; }) : original(path, options));
  await userEvent.click(screen.getByRole("button", { name: "Connect WhatsApp" }));
  expect(window.FB.login).toHaveBeenCalledOnce();
  await act(async () => { authorize(); event(); });
  expect(api.mock.calls.some(([path]) => path.endsWith("/complete"))).toBe(false);
  await act(async () => resolveStart({ attempt_id: "attempt", state: "browser-state" }));
  expect(await screen.findByText("+254700000001")).toBeInTheDocument();
});

test("malformed events are ignored", () => {
  expect(parseSignupEvent({ origin: "https://www.facebook.com", data: "not-json" })).toBeNull();
  expect(parseSignupEvent({ origin: "https://attacker.test", data: {} })).toBeNull();
});

test("Meta failures are highlighted alerts and retry restores neutral progress", async () => {
  setup();
  await prepareMeta();
  await userEvent.click(screen.getByRole("button", { name: "Connect WhatsApp" }));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  await act(async () => event("ERROR"));
  const alert = screen.getByRole("alert");
  expect(alert).toHaveClass("form-error-summary");
  expect(alert).toHaveTextContent("Connection unsuccessful");
  expect(alert).toHaveTextContent("Meta could not complete signup. Please retry.");
  await userEvent.click(screen.getByRole("button", { name: "Retry connection" }));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByRole("status")).not.toHaveClass("form-error-summary");
});

test("callback diagnostics expose only allowlisted fields, never provider payloads", async () => {
  const log = vi.spyOn(console, "info").mockImplementation(() => {});
  const { authorize } = setup();
  await prepareMeta();
  await userEvent.click(screen.getByRole("button", { name: "Connect WhatsApp" }));
  await act(async () => {
    authorize();
    window.dispatchEvent(new MessageEvent("message", {
      origin: "https://www.facebook.com",
      data: { type: "WA_EMBEDDED_SIGNUP", event: "ERROR", data: {
        error_code: 12345, error_message: "private provider text",
        access_token: "secret-token", code: "secret-code",
      } },
    }));
  });
  expect(screen.getByRole("alert")).toHaveTextContent("Meta error code: 12345");
  expect(log).toHaveBeenCalledWith("WhatsApp signup diagnostic", expect.objectContaining({
    stage: "ERROR", errorCode: "12345", authorizationReceived: true,
  }));
  const output = JSON.stringify(log.mock.calls);
  for (const secret of ["private provider text", "secret-token", "secret-code", "one-time-code", "browser-state"]) {
    expect(output).not.toContain(secret);
    expect(screen.getByRole("alert")).not.toHaveTextContent(secret);
  }
});

test("non-numeric provider error codes are not displayed or logged", async () => {
  const log = vi.spyOn(console, "info").mockImplementation(() => {});
  setup();
  await prepareMeta();
  await userEvent.click(screen.getByRole("button", { name: "Connect WhatsApp" }));
  await act(async () => window.dispatchEvent(new MessageEvent("message", {
    origin: "https://www.facebook.com", data: { type: "WA_EMBEDDED_SIGNUP", event: "ERROR",
      data: { error_code: "secret-provider-value" } },
  })));
  expect(screen.getByRole("alert")).toHaveTextContent("did not provide a numeric error code");
  expect(JSON.stringify(log.mock.calls)).not.toContain("secret-provider-value");
});

test("missing authorization explains the tab handoff and does not complete signup", async () => {
  const log = vi.spyOn(console, "info").mockImplementation(() => {});
  const { api } = setup();
  await prepareMeta();
  await userEvent.click(screen.getByRole("button", { name: "Connect WhatsApp" }));
  await act(async () => window.FB.login.mock.calls[0][0]({
    error: { code: 190, message: "private-provider-message" },
  }));
  expect(screen.getByRole("alert")).toHaveTextContent("did not return authorization to this tab");
  expect(screen.getByRole("alert")).toHaveTextContent("Meta error code: 190");
  expect(api.mock.calls.some(([path]) => path.endsWith("/complete"))).toBe(false);
  expect(JSON.stringify(log.mock.calls)).not.toContain("private-provider-message");
});

test.each([false, true])("cached-page return refreshes Drive state and preserves server lock=%s", async (locked) => {
  const { api } = setup();
  const checkbox = await screen.findByRole("checkbox", { name: "Import historical chats and media" });
  let enabled = false;
  api.mockImplementation(async (path, options) => {
    if (path.endsWith("/status")) return { status: "not_connected" };
    if (path.endsWith("/archive")) {
      if (options?.method === "PATCH") enabled = JSON.parse(options.body).import_enabled;
      return { import_enabled: enabled, available: true, locked, drive_status: "pending" };
    }
    if (path.endsWith("/drive/start")) return new Promise(() => {});
  });
  await userEvent.click(checkbox);
  await waitFor(() => expect(checkbox).toBeDisabled());
  const restored = new Event("pageshow");
  Object.defineProperty(restored, "persisted", { value: true });
  await act(async () => window.dispatchEvent(restored));
  await screen.findByText(/Google Drive not connected/);
  expect(checkbox).toBeChecked();
  expect(window.FB.login).not.toHaveBeenCalled();
  if (locked) expect(checkbox).toBeDisabled();
  else {
    expect(checkbox).toBeEnabled();
    await userEvent.click(checkbox);
    await waitFor(() => expect(checkbox).not.toBeChecked());
    expect(screen.getByRole("button", { name: "Connect WhatsApp" })).toBeEnabled();
  }
});

test("preparation failure offers only Back and Retry", async () => {
  render(<WhatsAppScreen session={{ session_id: "session" }}
    config={{ whatsapp_enabled: true }}
    api={vi.fn().mockRejectedValue(new Error("Connection unavailable"))}
    onSubmit={vi.fn()} onBack={vi.fn()} busy={false} />);
  expect(screen.getByRole("status")).toHaveTextContent("Loading connection status");
  expect(screen.getAllByRole("button")).toHaveLength(2);
  expect(await screen.findByRole("button", { name: "Retry connection" })).toBeEnabled();
  expect(screen.getAllByRole("button")).toHaveLength(2);
  expect(screen.queryByRole("button", { name: "Connect with Meta" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Submit and provision" })).not.toBeInTheDocument();
});

test("rollout gate does not load Meta or accept submission", () => {
  const api = vi.fn();
  render(<WhatsAppScreen session={{ session_id: "session" }} config={{ whatsapp_enabled: false }}
    api={api} onSubmit={vi.fn()} onBack={vi.fn()} />);
  expect(api).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Submit and provision" })).not.toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("not available yet");
});
