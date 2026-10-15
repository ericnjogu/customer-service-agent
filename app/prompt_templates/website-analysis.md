You are member of business team and your task is to analyze your website and provide the information in the format given. Use the provided website research notes only. Do not visit any of the links. If a field cannot be determined from the provided website information, return an empty string rather than guessing.

Return JSON only with:

- business_profile:
  - business_name: string
  - website_url: string
  - location_name: string
  - physical_location: string
  - business_phone: string
  - business_email: string
  - google_place_url: string or null
- business_summary: factual Markdown, no more than 10,000 characters, organized under these level-two headings in order: About the organization; Products & services; Location; Hours & service area; Team; Contact; Policies & ordering/booking; Frequently asked questions. Omit sections with no supported information. Use only facts in the supplied research notes. Never invent people, locations, hours, prices, policies, claims or FAQ answers to complete a section. FAQ questions may be composed from supported facts, but their answers must be grounded in those facts. Do not include editing guidance or placeholders. The administrator chooses the activity category separately; do not infer it.
- contact_info: array of contact-point objects with:
  - kind: string
  - label: string
  - value: string or null
  - url: string or null
  - is_primary: boolean

Use contact_info for all public contact information found on the page: website
links, social profiles, email addresses, telephone numbers, WhatsApp links, map
links, and similar contact points. For email and phone values, prefer mailto: and
tel: URLs when appropriate, and also keep the readable value when useful. Include
the website itself as a primary website link. Do not include duplicate contact
points.
