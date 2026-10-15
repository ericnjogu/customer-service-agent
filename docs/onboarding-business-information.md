# Business information during onboarding

The Business information screen requires one nature-of-business/activity category.
Selecting Other requires a description (up to 200 characters). Government, religious,
and community organizations are supported alongside commercial categories.
Existing submitted jobs are reusable without retroactive classification; unfinished
drafts must choose a category before submission.

Activity category and custom description are stored in explicit columns on
`onboarding_sessions` and `business_profiles`. Startup schema setup adds these
nullable columns idempotently; existing rows are not inferred or backfilled.

Website analysis uses eight Markdown sections where research supports them: About
the organization, Products & services, Location, Hours & service area, Team, Contact,
Policies & ordering/booking, and Frequently asked questions. Unsupported sections
are omitted. The administrator selects the category rather than the analysis model.

Without a website, an empty editor starts with these headings and marked guidance.
Only exact application guidance lines are removed before saving, including escaped
brackets emitted by the Markdown editor. User-authored text is retained. A template
without business content is rejected. The cleaned summary limit is 10,000 characters.
Previously saved summaries are not replaced by the template.

The backend and frontend JSON definitions are intentionally packaged independently
because the web image builds from the web directory. A parity test ensures their
categories and guidance stay identical. Update both definitions together.

Contact rows alternate themed backgrounds and animate insertion/removal, honoring
reduced-motion preferences. The existing optional marketing consent is unchanged.
