import { expect, test } from "vitest";
import { BUSINESS_TEMPLATE, cleanBusinessSummary, hasBusinessContent } from "./business-information";

test("template does not count as business content", () => {
  expect(hasBusinessContent(BUSINESS_TEMPLATE)).toBe(false);
  expect(cleanBusinessSummary(BUSINESS_TEMPLATE)).not.toContain("Guidance:");
  expect(hasBusinessContent(BUSINESS_TEMPLATE + "\nFresh bread.")).toBe(true);
});

test("preserves user-authored text", () => {
  expect(cleanBusinessSummary("[Guidance: My policy.]\nBread.")).toBe("[Guidance: My policy.]\nBread.");
});
