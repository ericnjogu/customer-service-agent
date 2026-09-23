import { expect, test } from "@playwright/test";

const wireMockUrl = process.env.AGENT_WIREMOCK_URL || "http://127.0.0.1:8080";
const apiBaseUrl = process.env.AGENT_E2E_API_URL;
const websiteUrl = `${wireMockUrl}/business-site`;
const telegramBotToken = "123456789:browser-e2e-token";

test.beforeEach(async () => {
  await wireMock("/__admin/reset", { method: "POST" });
  await installExternalServiceStubs();
});

test("a business with a website completes verification and reviews analyzed details", async ({
  page,
}) => {
  const identity = uniqueIdentity("website");
  await startAndVerifyAccount(page, identity);

  await page.getByLabel("Website URL").fill(websiteUrl);
  await page.getByLabel("Website verification email").fill(identity.websiteEmail);
  await page.getByRole("button", { name: "Send website verification code" }).click();

  await expect(page.getByRole("heading", { name: "Verify website email" })).toBeVisible();
  const websiteCode = await verificationCodeFor(identity.websiteEmail);
  await page.getByLabel("Verify website email six-digit code").fill(websiteCode);
  await page.getByRole("button", { name: "Verify code" }).click();

  await expect(page.getByRole("heading", { name: "Business information" })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByLabel("Business name")).toHaveValue("Hustle Bakery");
  await expect(page.getByRole("textbox", { name: "editable markdown" })).toContainText(
    "Neighborhood bakery",
  );

  // Keep this a real browser journey through the review screen: the generated
  // profile is edited, saved through the web/API boundary, then verified from
  // the persisted onboarding session returned by the API.
  await page.getByLabel("Business name").fill(identity.businessName);
  await page.getByRole("button", { name: "Review contact information" }).click();
  await expect(page.getByRole("heading", { name: "Contact information" })).toBeVisible();
  await page.getByRole("button", { name: "Submit for review" }).click();
  await expect(
    page.getByRole("heading", { name: "Onboarding submitted for review" }),
  ).toBeVisible();

  const sessionId = await page.evaluate(() =>
    window.localStorage.getItem("onboarding_session_id"),
  );
  const persistedResponse = await page.request.get(
    `${apiBaseUrl}/onboarding/sessions/${sessionId}`,
  );
  expect(persistedResponse.ok()).toBeTruthy();
  const persisted = await persistedResponse.json();
  expect(persisted.website_url).toBe(websiteUrl);
  expect(persisted.admin.username_email).toBe(identity.accountEmail);
  expect(persisted.website_verification_email).toBe(identity.websiteEmail);
  expect(persisted.website_email_verified).toBe(true);
  expect(persisted.business_profile.business_name).toBe(identity.businessName);
  expect(persisted.business_summary).toContain("Neighborhood bakery");
  expect(persisted.contact_info).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ kind: "instagram", url: "https://instagram.com/hustle_bakery" }),
    ]),
  );
  expect(persisted.status).toBe("awaiting_telegram_setup");

  const reviewEmail = await waitForEmail(
    "reviewer@example.com",
    `Telegram setup needed for ${identity.businessName}`,
  );
  expect(reviewEmail.text).toContain(identity.businessName);
  expect(await emailsFor(identity.accountEmail)).toHaveLength(1);
  expect(await emailsFor(identity.websiteEmail)).toHaveLength(1);
  const resendRequests = await requestsFor("/emails");
  expect(resendRequests).toHaveLength(3);
  expect(
    resendRequests.every(
      (request) => request.headers.Authorization === "Bearer test-resend-key",
    ),
  ).toBe(true);

  const tavilyRequest = await waitForRequest("/search");
  const tavilyPayload = JSON.parse(tavilyRequest.body);
  expect(tavilyRequest.headers.Authorization).toBe("Bearer test-tavily-key");
  expect(tavilyPayload.query).toContain(websiteUrl);
  expect(tavilyPayload.include_domains).toEqual([new URL(websiteUrl).host]);

  const openAiRequest = await waitForRequest("/v1/responses");
  const openAiPayload = JSON.parse(openAiRequest.body);
  expect(openAiRequest.headers.Authorization).toBe("Bearer test-openai-key");
  expect(openAiPayload.input).toContain(`Website URL: ${websiteUrl}`);
  expect(openAiPayload.input).toContain("Platform web search notes");
  expect(openAiPayload.input).toContain("Hustle Bakery");
});

test("a business without a website can submit manual details and complete Telegram setup", async ({
  page,
}) => {
  const identity = uniqueIdentity("manual");
  await startAndVerifyAccount(page, identity);

  await page.getByRole("button", { name: "Continue without a website" }).click();
  await expect(page.getByRole("heading", { name: "Business information" })).toBeVisible();
  await expect(page.getByText(/offerings, customers, service area, hours/i)).toBeVisible();

  await page.getByLabel("Business name").fill(identity.businessName);
  await page.getByRole("textbox", { name: "editable markdown" }).fill(
    "A neighborhood bakery serving sourdough and pastries. Open daily from 7am to 6pm.",
  );
  await page.getByRole("button", { name: "Review contact information" }).click();

  await expect(page.getByRole("heading", { name: "Contact information" })).toBeVisible();
  await page.getByLabel("Type").fill("instagram");
  await page.getByLabel("Label").fill("Instagram");
  await page.getByLabel("URL or value").fill("https://instagram.com/hustle_bakery_e2e");
  await page.getByRole("button", { name: "Submit for review" }).click();

  await expect(
    page.getByRole("heading", { name: "Onboarding submitted for review" }),
  ).toBeVisible();
  const reviewEmail = await waitForEmail(
    "reviewer@example.com",
    `Telegram setup needed for ${identity.businessName}`,
  );
  expect(reviewEmail.text).toContain(identity.businessName);

  const sessionId = await page.evaluate(() =>
    window.localStorage.getItem("onboarding_session_id"),
  );
  const persistedResponse = await page.request.get(
    `${apiBaseUrl}/onboarding/sessions/${sessionId}`,
  );
  expect(persistedResponse.ok()).toBeTruthy();
  const persisted = await persistedResponse.json();
  expect(persisted.website_url).toBeNull();
  expect(persisted.admin.username_email).toBe(identity.accountEmail);
  expect(persisted.website_verification_email).toBeNull();
  expect(persisted.username_email_verified).toBe(true);
  expect(persisted.website_email_verified).toBe(false);
  expect(persisted.business_profile.business_name).toBe(identity.businessName);
  expect(persisted.business_summary).toContain("serving sourdough and pastries");
  expect(persisted.contact_info).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        kind: "instagram",
        url: "https://instagram.com/hustle_bakery_e2e",
      }),
    ]),
  );
  expect(persisted.status).toBe("awaiting_telegram_setup");

  const setupUrl = reviewEmail.text.match(/Setup link: (http:\/\/[^\s]+)/)?.[1];
  expect(setupUrl).toBeTruthy();
  await page.goto(setupUrl);
  await expect(page.getByRole("heading", { name: "SaaS-admin Telegram setup" })).toBeVisible();
  await expect(page.getByLabel("Onboarding summary")).toContainText(identity.businessName);
  await expect(page.getByLabel("Onboarding summary")).toContainText(
    "instagram.com/hustle_bakery_e2e",
  );
  await expect(page.getByLabel("Onboarding summary")).not.toContainText("Website");
  await expect(page.getByLabel("Onboarding summary")).not.toContainText("None");

  await page.getByLabel("Telegram bot token").fill(telegramBotToken);
  await page.getByRole("button", { name: "Save Telegram setup" }).click();
  await expect(page.getByRole("heading", { name: "Completion" })).toBeVisible({
    timeout: 30_000,
  });

  const setWebhook = await waitForRequest(`/bot${telegramBotToken}/setWebhook`);
  const webhookPayload = JSON.parse(setWebhook.body);
  expect(webhookPayload.url).toContain("/api/webhooks/telegram?tenant_id=");
  expect(new URL(webhookPayload.url).origin).toBe(new URL(page.url()).origin);
  expect(webhookPayload.allowed_updates).toEqual(["message"]);
  await waitForRequest(`/bot${telegramBotToken}/getMe`);

  const completionEmail = await waitForEmail(
    identity.accountEmail,
    "Customer-service onboarding completed",
  );
  expect(completionEmail.text).toContain("https://t.me/hustle_bakery_e2e_bot");
  expect(await emailsFor(identity.accountEmail)).toHaveLength(2);
  await waitForEmail("reviewer@example.com", "Customer-service onboarding completed");
  expect(await emailsFor("reviewer@example.com")).toHaveLength(2);

  expect(await requestsFor("/search")).toHaveLength(0);
  expect(await requestsFor("/v1/responses")).toHaveLength(0);
  expect(await emailsFor(identity.websiteEmail)).toHaveLength(0);
  expect((await requestsFor(`/bot${telegramBotToken}/getMe`))).toHaveLength(1);
  expect((await requestsFor(`/bot${telegramBotToken}/setWebhook`))).toHaveLength(1);
});

test("a user sees browser validation, recovers from a wrong code, and cannot submit half a website", async ({
  page,
}) => {
  const identity = uniqueIdentity("recovery");
  await fillAccount(page, identity);
  await page.getByRole("button", { name: "Send account verification code" }).click();

  const code = await verificationCodeFor(identity.accountEmail);
  const wrongCode = code === "000000" ? "000001" : "000000";
  await page.getByLabel("Verify account email six-digit code").fill(wrongCode);
  await page.getByRole("button", { name: "Verify code" }).click();
  await expect(page.getByText(/code is missing, expired, invalid, or already used/i)).toBeVisible();

  await page.getByLabel("Verify account email six-digit code").fill(code);
  await page.getByRole("button", { name: "Verify code" }).click();
  await expect(page.getByRole("heading", { name: "Website verification" })).toBeVisible();

  await page.getByLabel("Website URL").fill(websiteUrl);
  await page.getByRole("button", { name: "Send website verification code" }).click();
  await expect(page.locator("#website_verification_email-error")).toHaveText(
    "Website verification email is required when a website is supplied.",
  );
  expect(await emailsFor(identity.websiteEmail)).toHaveLength(0);
});

async function startAndVerifyAccount(page, identity) {
  await fillAccount(page, identity);
  await page.getByRole("button", { name: "Send account verification code" }).click();
  await expect(page.getByRole("heading", { name: "Verify account email" })).toBeVisible();
  const code = await verificationCodeFor(identity.accountEmail);
  await page.getByLabel("Verify account email six-digit code").fill(code);
  await page.getByRole("button", { name: "Verify code" }).click();
  await expect(page.getByRole("heading", { name: "Website verification" })).toBeVisible();
}

async function fillAccount(page, identity) {
  await page.goto("/");
  await page.getByLabel("Username email").fill(identity.accountEmail);
  await page.getByLabel("Given name").fill("Amina");
  await page.getByLabel("Family name").fill("Kamau");
  await page.getByLabel("Admin phone number").fill("+254712345678");
  await page.getByLabel("Admin role/title").fill("Owner");
  const checkboxes = page.locator('input[type="checkbox"]');
  await checkboxes.nth(0).check();
  await checkboxes.nth(1).check();
}

function uniqueIdentity(scenario) {
  const suffix = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  return {
    accountEmail: `amina+${scenario}-${suffix}@example.com`,
    websiteEmail: `owner+${scenario}-${suffix}@example.com`,
    businessName: `Manual Bakery ${suffix}`,
  };
}

async function installExternalServiceStubs() {
  await stub({
    request: { method: "POST", urlPath: "/emails" },
    response: {
      status: 200,
      headers: { "Content-Type": "application/json" },
      jsonBody: { id: "email-browser-e2e" },
    },
  });
  await stub({
    request: { method: "GET", urlPath: "/business-site" },
    response: {
      status: 200,
      headers: { "Content-Type": "text/html; charset=utf-8" },
      body: [
        "<!doctype html><html><head><title>Hustle Bakery</title></head><body>",
        "<h1>Hustle Bakery</h1><p>Fresh bread daily from 7am to 6pm.</p>",
        '<a href="mailto:hello@hustle.example.com">Email us</a>',
        '<a href="https://instagram.com/hustle_bakery">Instagram</a>',
        "</body></html>",
      ].join(""),
    },
  });
  await stub({
    request: { method: "POST", urlPath: "/search" },
    response: {
      status: 200,
      headers: { "Content-Type": "application/json" },
      jsonBody: {
        answer: null,
        results: [
          {
            title: "Hustle Bakery opening hours",
            url: `${websiteUrl}/hours`,
            content: "Neighborhood bakery open daily from 7am to 6pm.",
            raw_content: "Neighborhood bakery open daily from 7am to 6pm.",
          },
        ],
      },
    },
  });
  const analysis = {
    business_profile: {
      business_name: "Hustle Bakery",
      website_url: websiteUrl,
      location_name: "Westlands",
      physical_location: "Westlands, Nairobi",
      business_phone: "+254700000000",
      business_email: "hello@hustle.example.com",
    },
    business_summary: "Neighborhood bakery serving fresh bread daily from 7am to 6pm.",
    contact_info: [
      { kind: "website", label: "Website", url: websiteUrl },
      { kind: "instagram", label: "Instagram", url: "https://instagram.com/hustle_bakery" },
    ],
    knowledge_sources: [],
  };
  await stub({
    request: { method: "POST", urlPath: "/v1/responses" },
    response: {
      status: 200,
      headers: { "Content-Type": "application/json" },
      jsonBody: {
        id: "resp_browser_e2e",
        object: "response",
        created_at: 1,
        status: "completed",
        model: "gpt-4.1-mini",
        output: [
          {
            id: "msg_browser_e2e",
            type: "message",
            status: "completed",
            role: "assistant",
            content: [
              {
                type: "output_text",
                text: JSON.stringify(analysis),
                annotations: [],
              },
            ],
          },
        ],
        usage: { input_tokens: 10, output_tokens: 20, total_tokens: 30 },
      },
    },
  });
  await stub({
    request: { method: "GET", urlPath: `/bot${telegramBotToken}/getMe` },
    response: {
      status: 200,
      headers: { "Content-Type": "application/json" },
      jsonBody: {
        ok: true,
        result: {
          id: 123456789,
          username: "hustle_bakery_e2e_bot",
          first_name: "Hustle Bakery",
        },
      },
    },
  });
  await stub({
    request: { method: "POST", urlPath: `/bot${telegramBotToken}/setWebhook` },
    response: {
      status: 200,
      headers: { "Content-Type": "application/json" },
      jsonBody: { ok: true, result: true },
    },
  });
}

async function stub(mapping) {
  await wireMock("/__admin/mappings", { method: "POST", body: mapping });
}

async function wireMock(path, options = {}) {
  const response = await fetch(`${wireMockUrl}${path}`, {
    method: options.method || "GET",
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!response.ok) {
    throw new Error(`WireMock ${options.method || "GET"} ${path} returned ${response.status}`);
  }
  return response;
}

async function requestJournal() {
  const response = await wireMock("/__admin/requests?unmatched=false");
  return (await response.json()).requests || [];
}

async function requestsFor(path) {
  return (await requestJournal())
    .map((entry) => entry.request)
    .filter((request) => request.url.split("?", 1)[0] === path);
}

async function waitForRequest(path, predicate = () => true) {
  return poll(async () => (await requestsFor(path)).find(predicate), `request ${path}`);
}

async function emailsFor(recipient, subject) {
  const requests = await requestsFor("/emails");
  return requests
    .map((request) => ({ request, payload: JSON.parse(request.body) }))
    .filter(({ payload }) => {
      const recipientMatches = payload.to?.includes(recipient);
      const subjectMatches = !subject || payload.subject === subject;
      return recipientMatches && subjectMatches;
    });
}

async function waitForEmail(recipient, subject) {
  const match = await poll(
    async () => (await emailsFor(recipient, subject))[0],
    `email to ${recipient} with subject ${subject}`,
  );
  return match.payload;
}

async function verificationCodeFor(recipient) {
  const email = await poll(
    async () => (await emailsFor(recipient))[0],
    `verification email to ${recipient}`,
  );
  const code = email.payload.text.match(/(?:^|\n)(\d{6})(?:\n|$)/)?.[1];
  expect(code).toMatch(/^\d{6}$/);
  return code;
}

async function poll(action, description, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  let result;
  while (Date.now() < deadline) {
    result = await action();
    if (result) return result;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`Timed out waiting for ${description}`);
}
