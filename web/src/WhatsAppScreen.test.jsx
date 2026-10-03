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
    if (path.endsWith("/status")) return { status: "not_connected" };
    if (path.endsWith("/start")) return { attempt_id: "attempt", state: "browser-state" };
    if (path.endsWith("/complete")) return { status: "connected", display_number: "+254700000001" };
  });
  const submit = vi.fn();
  render(<WhatsAppScreen session={{ session_id: "session" }} config={{ whatsapp_enabled: true,
    meta_app_id: "123", meta_signup_configuration_id: "config", meta_graph_api_version: "v25.0" }}
    api={api} onSubmit={submit} onBack={vi.fn()} busy={false} />);
  return { api, submit, authorize: () => callback({ authResponse: { code: "one-time-code" } }) };
}

function event(eventName = "FINISH", origin = "https://www.facebook.com") {
  window.dispatchEvent(new MessageEvent("message", { origin, data: JSON.stringify({
    type: "WA_EMBEDDED_SIGNUP", event: eventName,
    data: { waba_id: "456", phone_number_id: "789" },
  }) }));
}

test.each([true, false])("requires both Meta results and backend confirmation; code first=%s", async (codeFirst) => {
  const { api, submit, authorize } = setup();
  expect(screen.getByRole("button", { name: "Submit and provision" })).toBeDisabled();
  await waitFor(() => expect(screen.getByRole("button", { name: "Connect with Meta" })).toBeEnabled());
  await userEvent.click(screen.getByRole("button", { name: "Connect with Meta" }));
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
  await waitFor(() => expect(screen.getByRole("button", { name: "Connect with Meta" })).toBeEnabled());
  await userEvent.click(screen.getByRole("button", { name: "Connect with Meta" }));
  await act(async () => { authorize(); event("FINISH", "https://www.facebook.com.attacker.test"); });
  expect(api.mock.calls.some(([path]) => path.endsWith("/complete"))).toBe(false);
  await act(async () => event("CANCEL"));
  expect(screen.getByText("Connection cancelled. You can retry.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Submit and provision" })).toBeDisabled();
});

test("malformed events are ignored", () => {
  expect(parseSignupEvent({ origin: "https://www.facebook.com", data: "not-json" })).toBeNull();
  expect(parseSignupEvent({ origin: "https://attacker.test", data: {} })).toBeNull();
});

test("rollout gate does not load Meta or accept submission", () => {
  const api = vi.fn();
  render(<WhatsAppScreen session={{ session_id: "session" }} config={{ whatsapp_enabled: false }}
    api={api} onSubmit={vi.fn()} onBack={vi.fn()} />);
  expect(api).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Submit and provision" })).toBeDisabled();
  expect(screen.getByRole("alert")).toHaveTextContent("not available yet");
});
