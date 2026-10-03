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

async function prepare(page, mode = "finish") {
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
    display_phone_number: "+254700000001", code_verification_status: "VERIFIED" }] });
  await stub("POST", "/v25.0/456/subscribed_apps", { success: true });
  await stub("POST", `/v25.0/${phone}/register`, { success: true });
  await page.addInitScript(({ phone, mode }) => {
    window.FB = { init() {}, login(callback) {
      callback({ authResponse: { code: "test-single-use-code" } });
      window.dispatchEvent(new MessageEvent("message", {
        origin: mode === "untrusted" ? "https://www.facebook.com.attacker.test" : "https://www.facebook.com",
        data: JSON.stringify({ type: "WA_EMBEDDED_SIGNUP", event: mode === "cancel" ? "CANCEL" : "FINISH",
          data: { waba_id: "456", phone_number_id: phone } }),
      }));
    } };
  }, { phone, mode });
  await page.goto("/");
  const email = `whatsapp-${phone}@example.com`;
  await page.getByLabel("Username email").fill(email);
  await page.getByLabel("Given name").fill("Amina");
  await page.getByLabel("Family name").fill("Kamau");
  await page.getByLabel("Admin phone number").fill("+254712345678");
  await page.getByLabel("Admin role/title").fill("Owner");
  await page.locator('input[type="checkbox"]').nth(0).check();
  await page.locator('input[type="checkbox"]').nth(1).check();
  await page.getByRole("button", { name: "Send account verification code" }).click();
  let code;
  await expect.poll(async () => {
    const entry = (await journal()).find(({ request }) => request.url === "/emails" && request.body.includes(email));
    code = entry && JSON.parse(entry.request.body).text.match(/(?:^|\n)(\d{6})(?:\n|$)/)?.[1];
    return Boolean(code);
  }).toBe(true);
  await page.getByLabel("Verify account email six-digit code").fill(code);
  await page.getByRole("button", { name: "Verify code" }).click();
  await page.getByRole("button", { name: "Continue without a website" }).click();
  await page.getByLabel("Business name").fill(`WhatsApp Bakery ${phone}`);
  await page.getByRole("textbox", { name: "editable markdown" }).fill("A bakery selling bread daily from 7am to 6pm.");
  await page.getByRole("button", { name: "Review contact information" }).click();
  await page.getByRole("button", { name: "Continue to connection" }).click();
  await expect(page.getByRole("heading", { name: "Connect WhatsApp" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Submit and provision" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Connect with Meta" })).toBeEnabled();
  return phone;
}

test("browser signup reaches Graph and vault mocks, then automatically provisions", async ({ page }) => {
  const phone = await prepare(page);
  await page.getByRole("button", { name: "Connect with Meta" }).click();
  await expect(page.getByText("+254700000001")).toBeVisible();
  const records = await journal();
  expect(records.some(({ request }) => request.url === `/v25.0/${phone}/register`)).toBe(true);
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

for (const mode of ["cancel", "untrusted"]) {
  test(`browser ${mode} callback cannot complete a connection`, async ({ page }) => {
    await prepare(page, mode);
    await page.getByRole("button", { name: "Connect with Meta" }).click();
    if (mode === "cancel") await expect(page.getByText("Connection cancelled. You can retry.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Submit and provision" })).toBeDisabled();
    expect((await journal()).some(({ request }) => request.url === "/v25.0/oauth/access_token")).toBe(false);
  });
}
