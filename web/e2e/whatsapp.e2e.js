import { expect, test } from "@playwright/test";

const wire = process.env.AGENT_WIREMOCK_URL || "http://127.0.0.1:8080";
async function stub(method, path, result) {
  const response = await fetch(`${wire}/__admin/mappings`, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      request: { method, urlPathPattern: path }, response: { status: 200,
        headers: { "Content-Type": "application/json" }, jsonBody: result },
    }) });
  expect(response.ok).toBeTruthy();
}
async function journal() {
  return (await (await fetch(`${wire}/__admin/requests`)).json()).requests || [];
}

async function prepare(page, mode = "finish", importMode = null) {
  await fetch(`${wire}/__admin/reset`, { method: "POST" });
  const phone = String(Date.now());
  await stub("POST", "/emails", { id: "email" });
  await stub("POST", "/v1/auth/kubernetes/login", { auth: { client_token: "test-bao-token", lease_duration: 600 } });
  await stub("POST", "/v1/tenant-credentials/data/whatsapp/.*", { data: { version: 1 } });
  await stub("POST", "/v25.0/oauth/access_token", { access_token: "test-customer-token" });
  await stub("GET", "/v25.0/debug_token", { data: { is_valid: true, app_id: "123", expires_at: 0,
    scopes: ["whatsapp_business_management", "whatsapp_business_messaging"],
    granular_scopes: [{ scope: "whatsapp_business_management", target_ids: ["456"] }],
  } });
  await stub("GET", "/v25.0/456", { id: "456" });
  await stub("GET", "/v25.0/456/phone_numbers", { data: [{ id: phone,
    display_phone_number: "+254700000001", code_verification_status: "VERIFIED",
    is_on_biz_app: true, platform_type: "CLOUD_API" }] });
  await stub("POST", "/v25.0/456/subscribed_apps", { success: true });
  await stub("POST", `/v25.0/${phone}/smb_app_data`, { success: true });
  await stub("POST", "/google/token", { access_token: "google-access", refresh_token: "google-refresh",
    scope: "https://www.googleapis.com/auth/drive.file" });
  await stub("GET", "/drive/v3/about", { user: { permissionId: "google-account" } });
  await stub("POST", "/v1/tenant-credentials/data/google-drive/.*", { data: { version: 1 } });
  for (let i = 0; i < 7; i++) {
    const id = `folder-${phone}-${i}`;
    const mapping = async (data) => {
      const response = await fetch(`${wire}/__admin/mappings`, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
      expect(response.ok).toBeTruthy();
    };
    await mapping({ scenarioName: "generate-folder-ids", requiredScenarioState: i === 0 ? "Started" : `id-${i}`,
      newScenarioState: `id-${i + 1}`, request: { method: "GET", urlPath: "/drive/v3/files/generateIds" },
      response: { status: 200, jsonBody: { ids: [id] }, headers: { "Content-Type": "application/json" } } });
    await mapping({ scenarioName: id, requiredScenarioState: "Started",
      request: { method: "GET", urlPath: `/drive/v3/files/${id}` }, response: { status: 404 } });
    await mapping({ scenarioName: id, requiredScenarioState: "Started", newScenarioState: "created",
      request: { method: "POST", urlPath: "/drive/v3/files", bodyPatterns: [{ matchesJsonPath: { expression: "$.id", equalTo: id } }] },
      response: { status: 200, jsonBody: { id }, headers: { "Content-Type": "application/json" } } });
    await mapping({ scenarioName: id, requiredScenarioState: "created",
      request: { method: "GET", urlPath: `/drive/v3/files/${id}` },
      response: { status: 200, jsonBody: { id, ownedByMe: true, permissions: [{ role: "owner", type: "user" }] }, headers: { "Content-Type": "application/json" } } });
  }
  await page.route("https://accounts.google.com/o/oauth2/v2/auth?**", async (route) => {
    if (importMode === "back") {
      await route.fulfill({ contentType: "text/html", body: "<h1>Google consent</h1>" });
      return;
    }
    const authorization = new URL(route.request().url());
    expect(authorization.searchParams.get("scope")).toBe("https://www.googleapis.com/auth/drive.file");
    const callback = new URL(authorization.searchParams.get("redirect_uri"));
    callback.searchParams.set("state", authorization.searchParams.get("state"));
    callback.searchParams.set(importMode === "cancel" ? "error" : "code", importMode === "cancel" ? "access_denied" : "test-google-code");
    await route.fulfill({ status: 302, headers: { location: callback.href }, body: "" });
  });
  await page.addInitScript(({ phone, mode }) => {
    window.FB = { init() {}, login(callback) {
      callback({ authResponse: { code: "test-single-use-code" } });
      window.dispatchEvent(new MessageEvent("message", {
        origin: mode === "untrusted" ? "https://www.facebook.com.attacker.test" : "https://www.facebook.com",
        data: JSON.stringify({ type: "WA_EMBEDDED_SIGNUP", event: mode === "cancel" ? "CANCEL" : "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING",
          data: { waba_id: "456", phone_number_id: phone } }),
      }));
    } };
  }, { phone, mode });
  await page.goto("/");
  const email = `whatsapp-${phone}@example.com`;
  await page.getByLabel(/^Email/).fill(email);
  await page.getByLabel("Given name").fill("Amina");
  await page.getByLabel("Family name").fill("Kamau");
  await page.getByLabel("Phone number").fill("+254712345678");
  await expect(page.getByLabel("Admin role/title")).toHaveCount(0);
  await page.getByLabel(/I accept the/).check();
  await page.getByRole("button", { name: "Send account verification code to email" }).click();
  let code;
  await expect.poll(async () => {
    const entry = (await journal()).find(({ request }) => request.url === "/emails" && request.body.includes(email));
    code = entry && JSON.parse(entry.request.body).text.match(/(?:^|\n)(\d{6})(?:\n|$)/)?.[1];
    return Boolean(code);
  }).toBe(true);
  await page.getByLabel("Verify account email six-digit code").fill(code);
  await page.getByRole("button", { name: "Verify code" }).click();
  await expect(page.getByRole("heading", { name: "Business information" })).toBeVisible();
  await page.getByLabel("Business name").fill(`WhatsApp Bakery ${phone}`);
  await page.getByRole("textbox", { name: "editable markdown" }).fill("A bakery selling bread daily from 7am to 6pm.");
  await page.getByRole("button", { name: "Continue to connection" }).click();
  await expect(page.getByRole("heading", { name: "Connect WhatsApp" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Submit and provision" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Connect WhatsApp" })).toBeEnabled();
  const sessionId = await page.evaluate(() => localStorage.getItem("onboarding_session_id"));
  const persisted = await page.request.get(`/api/onboarding/sessions/${sessionId}`);
  expect((await persisted.json()).username_email_verified).toBe(true);
  await page.reload();
  await expect(page.getByRole("button", { name: "Connect WhatsApp" })).toBeEnabled();
  await expect(page.getByLabel("Import historical chats and media")).not.toBeChecked();
  expect((await journal()).some(({ request }) => request.url === "/google/token")).toBe(false);
  if (importMode) {
    // This checkbox immediately navigates to Google; assert the persisted value after return.
    await page.getByLabel("Import historical chats and media").click();
    if (importMode === "back") {
      await expect(page.getByRole("heading", { name: "Google consent" })).toBeVisible();
      await page.goBack();
      return phone;
    }
    if (importMode === "cancel") {
      await expect(page.getByText(/Google authorization was cancelled/)).toBeVisible();
      return phone;
    }
    await expect(page.getByRole("link", { name: /Ristoh CSS/ })).toBeVisible();
    await page.reload();
    await expect(page.getByRole("link", { name: /Ristoh CSS/ })).toBeVisible();
    await expect(page.getByLabel("Import historical chats and media")).toBeChecked();
  }
  const authorization = await page.request.get(`/api/onboarding/sessions/${sessionId}/whatsapp/status`);
  expect(authorization.status()).toBe(200);
  return phone;
}

test("browser signup reaches Graph and vault mocks, then automatically provisions", async ({ page }) => {
  const phone = await prepare(page);
  await page.getByRole("button", { name: "Connect WhatsApp" }).click();
  await expect(page.getByText("+254700000001")).toBeVisible();
  const records = await journal();
  expect(records.some(({ request }) => request.url === `/v25.0/${phone}/smb_app_data`)).toBe(false);
  expect(records.some(({ request }) => request.url === `/v25.0/${phone}/register`)).toBe(false);
  expect(records.some(({ request }) => request.url.startsWith("/v1/tenant-credentials/data/whatsapp/"))).toBe(true);
  const session = await page.evaluate(() => localStorage.getItem("onboarding_session_id"));
  const status = await page.request.get(`/api/onboarding/sessions/${session}/whatsapp/status`);
  expect((await status.json()).status).toBe("connected");
  await page.getByRole("button", { name: "Submit and provision" }).click();
  await expect(page.getByRole("heading", { name: "Setup complete" })).toBeVisible({ timeout: 30000 });
  const active = await page.request.get(`/api/onboarding/sessions/${session}/whatsapp/status`);
  expect((await active.json()).status).toBe("active");
  const publicSession = await page.request.get(`/api/onboarding/sessions/${session}`);
  expect(await publicSession.text()).not.toContain("test-customer-token");
});

test("Drive consent creates private folders and enables history without blocking submission", async ({ page }) => {
  const phone = await prepare(page, "finish", "finish");
  await page.getByRole("button", { name: "Connect WhatsApp" }).click();
  await expect(page.getByText("+254700000001")).toBeVisible();
  const records = await journal();
  expect(records.some(({ request }) => request.url === `/v25.0/${phone}/smb_app_data`)).toBe(true);
  expect(records.filter(({ request }) => request.method === "POST" && request.url.startsWith("/drive/v3/files?"))).toHaveLength(7);
  expect(records.some(({ request }) => request.url.startsWith("/v1/tenant-credentials/data/google-drive/"))).toBe(true);
  await page.getByRole("button", { name: "Submit and provision" }).click();
  await expect(page.getByRole("heading", { name: "Setup complete" })).toBeVisible({ timeout: 30000 });
});

test("declined Drive consent permits opting out without launching Meta", async ({ page }) => {
  await prepare(page, "finish", "cancel");
  await expect(page.getByRole("button", { name: "Connect WhatsApp" })).toBeDisabled();
  expect((await journal()).some(({ request }) => request.url === "/google/token")).toBe(false);
  await page.getByLabel("Import historical chats and media").uncheck();
  await expect(page.getByRole("button", { name: "Connect WhatsApp" })).toBeEnabled();
});

test("missing browser cookie can be recovered without resetting the verified draft", async ({ page }) => {
  await prepare(page, "finish");
  const sessionId = await page.evaluate(() => localStorage.getItem("onboarding_session_id"));
  const before = await (await page.request.get(`/api/onboarding/sessions/${sessionId}`)).json();
  await page.context().clearCookies();
  await page.goto(`/?session_id=${sessionId}`);
  await expect(page.getByRole("heading", { name: "Verify this browser" })).toBeVisible();
  await page.getByRole("button", { name: "Send browser verification code" }).click();
  let code;
  await expect.poll(async () => {
    const entry = (await journal()).find(({ request }) => request.url === "/emails" &&
      request.body.includes("Verify this browser to resume onboarding") && request.body.includes(before.admin.username_email));
    code = entry && JSON.parse(entry.request.body).text.match(/Enter (\d{6})/)?.[1];
    return Boolean(code);
  }).toBe(true);
  const during = await (await page.request.get(`/api/onboarding/sessions/${sessionId}`)).json();
  expect(during.username_email_verified).toBe(true);
  expect(during.current_step).toBe(before.current_step);
  await page.getByLabel("Browser verification code", { exact: true }).fill(code);
  await page.getByRole("button", { name: "Verify and resume" }).click();
  await expect(page.getByRole("heading", { name: "Connect WhatsApp" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Connect WhatsApp" })).toBeEnabled();
  await page.reload();
  await expect(page.getByRole("button", { name: "Connect WhatsApp" })).toBeEnabled();
  const after = await (await page.request.get(`/api/onboarding/sessions/${sessionId}`)).json();
  expect(after).toEqual(before);
});

test("browser Back from Google leaves import editable and aligned", async ({ page }) => {
  await prepare(page, "finish", "back");
  const checkbox = page.getByRole("checkbox", { name: "Import historical chats and media" });
  await expect(checkbox).toBeChecked();
  await expect(checkbox).toBeEnabled();
  await expect(page.getByText(/Google Drive not connected/)).toBeVisible();
  const box = await checkbox.boundingBox();
  const label = await page.locator(".checkbox-row span").filter({ hasText: "Import historical chats and media" }).boundingBox();
  expect(box.x + box.width).toBeLessThan(label.x);
  expect(Math.abs(box.y - label.y)).toBeLessThan(12);
  await checkbox.uncheck();
  await expect(page.getByRole("button", { name: "Connect WhatsApp" })).toBeEnabled();
  expect((await journal()).some(({ request }) => request.url === "/google/token")).toBe(false);
});

for (const mode of ["cancel", "untrusted"]) {
  test(`browser ${mode} callback cannot complete a connection`, async ({ page }) => {
    await prepare(page, mode);
    await page.getByRole("button", { name: "Connect WhatsApp" }).click();
    if (mode === "cancel") {
      await expect(page.getByText("Connection cancelled. You can retry.")).toBeVisible();
      await expect(page.getByRole("checkbox", { name: "Import historical chats and media" })).toBeEnabled();
      await page.reload();
      await expect(page.getByRole("checkbox", { name: "Import historical chats and media" })).toBeEnabled();
    }
    await expect(page.getByRole("button", { name: "Submit and provision" })).toHaveCount(0);
    expect((await journal()).some(({ request }) => request.url === "/v25.0/oauth/access_token")).toBe(false);
  });
}
