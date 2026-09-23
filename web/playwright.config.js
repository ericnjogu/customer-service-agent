import { defineConfig, devices } from "@playwright/test";
import { createServer } from "node:net";

// Reserve both ports together so the OS cannot return the same port twice.
// Release them before Playwright launches the servers. Workers inherit the URLs.
const reservations = [];
async function localUrl(variable) {
  if (process.env[variable]) {
    const url = new URL(process.env[variable]);
    if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" ||
        !url.port || url.pathname !== "/" || url.search || url.hash ||
        url.username || url.password) {
      throw new Error(`${variable} must be an http://127.0.0.1:<port> origin.`);
    }
    return url.origin;
  }
  const server = createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  reservations.push(server);
  return `http://127.0.0.1:${server.address().port}`;
}

let webBaseUrl;
let apiBaseUrl;
try {
  webBaseUrl = await localUrl("AGENT_E2E_WEB_URL");
  apiBaseUrl = await localUrl("AGENT_E2E_API_URL");
  if (webBaseUrl === apiBaseUrl) {
    throw new Error("The browser QA web and API servers must use different ports.");
  }
} finally {
  await Promise.all(reservations.map((server) => new Promise((resolve) => server.close(resolve))));
}
process.env.AGENT_E2E_WEB_URL = webBaseUrl;
process.env.AGENT_E2E_API_URL = apiBaseUrl;
const webPort = Number(new URL(webBaseUrl).port);
const apiPort = Number(new URL(apiBaseUrl).port);
const wireMockUrl = process.env.AGENT_WIREMOCK_URL || "http://127.0.0.1:8080";
const slowMo = Number(process.env.AGENT_E2E_SLOW_MO_MS || "0");
if (!Number.isSafeInteger(slowMo) || slowMo < 0) {
  throw new Error("AGENT_E2E_SLOW_MO_MS must be a non-negative integer (milliseconds).");
}

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.e2e.js",
  // Allow time for deliberate pauses during interactive QA.
  timeout: 60_000 + slowMo * 100,
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [["line"], ["html", { open: "never" }]] : "list",
  outputDir: "test-results",
  use: {
    baseURL: webBaseUrl,
    launchOptions: { slowMo },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      command: `uv run uvicorn app.main:app --host 127.0.0.1 --port ${apiPort}`,
      cwd: "..",
      url: `${apiBaseUrl}/healthz`,
      timeout: 120_000,
      reuseExistingServer: false,
      env: {
        ...process.env,
        AGENT_DEPLOYMENT_ENVIRONMENT: "test",
        AGENT_DATABASE_URL:
          process.env.AGENT_E2E_DATABASE_URL ||
          "postgresql://postgres:postgres@127.0.0.1:5432/customer_service_e2e",
        AGENT_RETRIEVAL_PROVIDER: "pgvector",
        AGENT_EMBEDDING_PROVIDER: "local",
        AGENT_EMBEDDING_DIMENSIONS: "64",
        AGENT_TENANT_CONFIG_CACHE_PROVIDER: "redis",
        AGENT_REDIS_URL:
          process.env.AGENT_E2E_REDIS_URL || "redis://127.0.0.1:6379/0",
        AGENT_EMAIL_PROVIDER: "resend",
        AGENT_EMAIL_FROM: "Ristoh AI <hello@example.com>",
        AGENT_ONBOARDING_REVIEW_EMAIL: "reviewer@example.com",
        RESEND_API_KEY: "test-resend-key",
        AGENT_RESEND_API_BASE_URL: wireMockUrl,
        OPENAI_API_KEY: "test-openai-key",
        AGENT_OPENAI_BASE_URL: `${wireMockUrl}/v1`,
        AGENT_ONBOARDING_WEBSITE_ANALYSIS_PROVIDER: "openai",
        AGENT_ONBOARDING_WEBSITE_FETCH_TIMEOUT_SECONDS: "3",
        AGENT_PLATFORM_WEB_SEARCH_PROVIDER: "tavily",
        AGENT_PLATFORM_WEB_SEARCH_API_KEY: "test-tavily-key",
        AGENT_TAVILY_API_BASE_URL: wireMockUrl,
        AGENT_RUNTIME_WEB_SEARCH_PROVIDER: "none",
        AGENT_PROVIDER_PROJECT_PROVISIONER: "metadata",
        AGENT_TELEGRAM_API_BASE_URL: wireMockUrl,
        AGENT_WEB_PUBLIC_BASE_URL: webBaseUrl,
        AGENT_ONBOARDING_REQUIRE_ADMIN_EMAIL_DOMAIN_MATCH: "false",
        AGENT_ONBOARDING_VERIFICATION_CODE_SECRET:
          "browser-e2e-verification-secret-32-characters",
        AGENT_CORS_ALLOW_ORIGINS: webBaseUrl,
        AGENT_TELEMETRY_ENABLED: "false",
        LANGSMITH_TRACING: "false",
      },
    },
    {
      command: `npm run dev -- --host 127.0.0.1 --port ${webPort} --strictPort`,
      url: webBaseUrl,
      timeout: 120_000,
      reuseExistingServer: false,
      env: {
        ...process.env,
        VITE_API_BASE_URL: apiBaseUrl,
        VITE_REQUIRE_ADMIN_EMAIL_DOMAIN_MATCH: "false",
      },
    },
  ],
});
