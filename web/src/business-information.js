import definition from "./business-information.json";

export const BUSINESS_CATEGORIES = definition.categories;
export const BUSINESS_TEMPLATE = definition.sections
  .map(([heading, guidance]) => `## ${heading}\n\n[Guidance: ${guidance}]`)
  .join("\n\n");

const guidanceLines = new Set(definition.sections.map(([, text]) => `[Guidance: ${text}]`));
const headings = new Set(definition.sections.map(([heading]) => `## ${heading}`));

export function cleanBusinessSummary(value) {
  return String(value || "").split("\n")
    .filter(line => !guidanceLines.has(line.trim().replace(/\\([\[\]])/g, "$1")))
    .join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

export function hasBusinessContent(value) {
  return cleanBusinessSummary(value).split("\n")
    .some(line => line.trim() && !headings.has(line.trim()));
}
