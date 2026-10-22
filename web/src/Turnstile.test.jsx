import React from "react";
import { afterEach, expect, test, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Turnstile from "./Turnstile";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test("renders explicit signup challenge, clears expired tokens and supports retry", async () => {
  const api = { render: vi.fn(() => "widget"), remove: vi.fn() };
  vi.stubGlobal("turnstile", api);
  const onToken = vi.fn();
  const view = render(<Turnstile siteKey="public-key" onToken={onToken} />);
  await waitFor(() => expect(api.render).toHaveBeenCalledOnce());
  const options = api.render.mock.calls[0][1];
  expect(options).toMatchObject({ sitekey: "public-key", action: "onboarding" });
  act(() => options.callback("single-use-token"));
  expect(onToken).toHaveBeenLastCalledWith("single-use-token");
  act(() => options["expired-callback"]());
  expect(onToken).toHaveBeenLastCalledWith("");
  expect(screen.getByRole("alert")).toHaveTextContent("expired");
  await userEvent.click(screen.getByRole("button", { name: "Retry security verification" }));
  expect(api.remove).toHaveBeenCalledWith("widget");
  await waitFor(() => expect(api.render).toHaveBeenCalledTimes(2));
  act(() => api.render.mock.calls[1][1]["error-callback"]());
  expect(onToken).toHaveBeenLastCalledWith("");
  expect(screen.getByRole("alert")).toHaveTextContent("failed");
  view.unmount();
  expect(api.remove).toHaveBeenCalledTimes(2);
});

test("missing public key does not pretend security verification succeeded", () => {
  const onToken = vi.fn();
  render(<Turnstile onToken={onToken} />);
  expect(onToken).toHaveBeenLastCalledWith("");
  expect(screen.getByRole("status")).toHaveTextContent("not ready");
});
