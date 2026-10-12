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
- business_summary: markdown string containing a comprehensive account from the website information. it should be in first person narrative and given from the stand point of sales person who wants to present the most useful and outstanding information about the business that will draw a customer to initiate a purchase. It should focus on the products, selling points, people and blogs that carry product information.
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