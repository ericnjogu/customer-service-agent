import React from "react";
import { afterEach, expect, test, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import WhatsAppScreen, { parseSignupEvent } from "./WhatsAppScreen";

afterEach(() => { cleanup(); delete window.FB; });

function setup() {
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
    meta_app_id: "123", meta_signup_configuration_id: "config", meta_graph_api_version: "v25.0" }}
    api={api} onSubmit={submit} onBack={vi.fn()} busy={false} />);
  return { api, submit, authorize: () => callback({ authResponse: { code: "one-time-code" } }) };
}

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
  await waitFor(() => expect(screen.getByRole("button", { name: "Connect WhatsApp" })).toBeEnabled());
  expect(screen.queryByRole("button", { name: "Retry connection" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Submit and provision" })).not.toBeInTheDocument();
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
