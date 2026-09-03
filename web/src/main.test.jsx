import React from "react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("@mdxeditor/editor", () => {
  const ToolbarButton = ({ label }) => <button type="button">{label}</button>;
  return {
    BlockTypeSelect: () => <select aria-label="Block type" />,
    BoldItalicUnderlineToggles: () => (
      <>
        <ToolbarButton label="Bold" />
        <ToolbarButton label="Italic" />
        <ToolbarButton label="Underline" />
      </>
    ),
    CreateLink: () => <ToolbarButton label="Create link" />,
    ListsToggle: () => <ToolbarButton label="List" />,
    MDXEditor: ({ markdown, onChange, plugins = [], ...props }) => (
      <div>
        <div role="toolbar" aria-label="Markdown formatting toolbar">
          {plugins
            .map((plugin) => plugin?.toolbarContents)
            .filter(Boolean)
            .map((ToolbarContents, index) => (
              <ToolbarContents key={index} />
            ))}
        </div>
        <textarea
          aria-label={props["aria-label"] || "Markdown editor"}
          value={markdown}
          onChange={(event) => onChange(event.target.value)}
        />
      </div>
    ),
    Separator: () => <span aria-hidden="true" />,
    UndoRedo: () => <ToolbarButton label="Undo" />,
    headingsPlugin: () => ({}),
    linkPlugin: () => ({}),
    listsPlugin: () => ({}),
    markdownShortcutPlugin: () => ({}),
    toolbarPlugin: (options) => options,
  };
});

import {
  App,
  ContactInfoScreen,
  StartScreen,
  TelegramScreen,
  TermsScreen,
  WebsiteVerificationScreen,
  isValidAdminPhoneNumber,
  normalizeWebsiteUrl,
  validateAccountForm,
  validateStartForm,
  validateWebsiteVerificationForm,
} from "./main.jsx";

function validStartForm(overrides = {}) {
  return {
    username_email: "admin@example.co.ke",
    given_name: "John",
    family_name: "Doe",
    admin_phone_number: "+254110101010",
    admin_role_title: "Owner",
    authority_confirmed: true,
    terms_accepted: true,
    ...overrides,
  };
}

function validWebsiteForm(overrides = {}) {
  return {
    website_url: "example.co.ke",
    website_verification_email: "admin@example.co.ke",
    ...overrides,
  };
}

function validAdmin(overrides = {}) {
  return {
    username_email: "admin@example.co.ke",
    given_name: "John",
    family_name: "Doe",
    phone_number: "+254110101010",
    role_title: "Owner",
    authority_confirmed: true,
    terms_accepted: true,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.localStorage?.clear();
  window.history.pushState({}, "", "/");
});

describe("website URL validation", () => {
  test("renders the beta terms page", () => {
    render(<TermsScreen />);

    expect(screen.getByRole("heading", { name: /beta terms of service/i }))
      .toBeInTheDocument();
    expect(screen.getByText(/version: beta-2026-08-28/i)).toBeInTheDocument();
    expect(screen.getByText(/provided as a beta product/i)).toBeInTheDocument();
  });

  test("links the required terms checkbox to the beta terms page", () => {
    render(
      <StartScreen
        form={validStartForm({ terms_accepted: false })}
        setForm={vi.fn()}
        onSubmit={vi.fn()}
        busy={false}
      />,
    );

    const termsLink = screen.getByRole("link", {
      name: /beta terms of service/i,
    });
    expect(termsLink).toHaveAttribute("href", "/terms");
    expect(termsLink).toHaveAttribute("target", "_blank");
    expect(screen.getByLabelText(/accept the beta terms of service/i))
      .toBeInTheDocument();
  });

  test("normalizes bare domains to HTTPS URLs", () => {
    expect(normalizeWebsiteUrl("example.co.ke")).toBe("https://example.co.ke");
    expect(normalizeWebsiteUrl(" https://example.com/path ")).toBe(
      "https://example.com/path",
    );
  });

  test("accepts bare domains when the admin email belongs to the same domain", () => {
    expect(
      validateWebsiteVerificationForm(validWebsiteForm()),
    ).toEqual({});
  });

  test("shows field-level and summary errors for mismatched domains", async () => {
    const user = userEvent.setup();
    const setForm = vi.fn();
    const onSubmit = vi.fn();
    render(
      <StartScreen
        form={validStartForm({ username_email: "eric@example.com" })}
        setForm={setForm}
        onSubmit={onSubmit}
        busy={false}
      />,
    );

    await user.click(screen.getByLabelText(/username email/i));
    await user.tab();

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /send account verification email/i }),
    );

    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
  });

  test("allows mismatched domains when domain matching is disabled", () => {
    expect(
      validateStartForm(
        validWebsiteForm({ website_verification_email: "admin@example.net" }),
        { requireAdminEmailDomainMatch: false },
      ),
    ).toEqual({});
  });

  test("requires admin phone, role, authority, and terms", () => {
    expect(
      validateAccountForm(
        validStartForm({
          admin_phone_number: "",
          admin_role_title: "",
          authority_confirmed: false,
          terms_accepted: false,
        }),
      ),
    ).toMatchObject({
      admin_phone_number: "Admin phone number is required.",
      admin_role_title: "Admin role/title is required.",
      authority_confirmed: "Authority confirmation is required.",
      terms_accepted: "Terms of service acceptance is required.",
    });
  });

  test("requires both given name and family name", () => {
    expect(
      validateAccountForm(
        validStartForm({
          given_name: "",
          family_name: "",
        }),
      ),
    ).toMatchObject({
      given_name: "Given name is required.",
      family_name: "Family name is required.",
    });
  });

  test("validates admin phone numbers with libphonenumber metadata", () => {
    expect(isValidAdminPhoneNumber("+254110101010")).toBe(true);
    expect(isValidAdminPhoneNumber("0723921716")).toBe(false);
    expect(isValidAdminPhoneNumber("+123")).toBe(false);
  });

  test("submits account setup with structured names and username email", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        session_id: "00000000-0000-0000-0000-000000000001",
        status: "username_email_verification_pending",
        current_step: "username-email-verification",
        website_url: null,
        website_verification_email: null,
        admin: validAdmin(),
        username_email_verified: false,
        website_email_verified: false,
        contact_info: [],
        provider_projects: {},
        created_at: "2026-08-20T00:00:00Z",
        updated_at: "2026-08-20T00:00:00Z",
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    await user.type(
      screen.getByLabelText(/username email/i),
      "admin@example.co.ke",
    );
    await user.type(screen.getByLabelText(/given name/i), "John");
    await user.type(screen.getByLabelText(/family name/i), "Doe");
    await user.type(screen.getByLabelText(/admin phone number/i), "+254110101010");
    await user.type(screen.getByLabelText(/admin role\/title/i), "Owner");
    await user.click(screen.getByLabelText(/authorized to configure/i));
    await user.click(screen.getByLabelText(/accept the beta terms of service/i));
    await user.click(
      screen.getByRole("button", { name: /send account verification email/i }),
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [_url, options] = fetchMock.mock.calls[0];
    expect(JSON.parse(options.body)).toMatchObject({
      admin: {
        username_email: "admin@example.co.ke",
        given_name: "John",
        family_name: "Doe",
        phone_number: "+254110101010",
        role_title: "Owner",
        authority_confirmed: true,
        terms_accepted: true,
      },
    });
  });

  test("website verification validates domain mismatch on blur and submit", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(
      <WebsiteVerificationScreen
        form={validWebsiteForm({
          website_verification_email: "admin@example.net",
        })}
        setForm={vi.fn()}
        onSubmit={onSubmit}
        onBack={vi.fn()}
        busy={false}
      />,
    );

    await user.click(screen.getByLabelText(/website verification email/i));
    await user.tab();

    expect(
      screen.getAllByText(
        "Website verification email domain must belong to the website domain.",
      ),
    ).toHaveLength(2);

    await user.click(
      screen.getByRole("button", { name: /send website verification email/i }),
    );

    const summary = screen.getByRole("alert");
    await waitFor(() => expect(summary).toHaveFocus());
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe("email verification flow", () => {
  test("clicking a username verification link verifies email and shows the website screen", async () => {
    const sessionId = "00000000-0000-0000-0000-000000000001";
    window.history.pushState(
      {},
      "",
      `/verify-username-email?session_id=${sessionId}&token=email-token`,
    );
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          session_id: sessionId,
          status: "username_email_verification_pending",
          current_step: "username-email-verification",
          website_url: null,
          website_verification_email: null,
          admin: validAdmin(),
          username_email_verified: false,
          website_email_verified: false,
          contact_info: [],
          provider_projects: {},
          created_at: "2026-08-20T00:00:00Z",
          updated_at: "2026-08-20T00:00:00Z",
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          session_id: sessionId,
          status: "draft",
          current_step: "website",
          website_url: null,
          website_verification_email: null,
          admin: validAdmin(),
          username_email_verified: true,
          website_email_verified: false,
          contact_info: [],
          provider_projects: {},
          created_at: "2026-08-20T00:00:00Z",
          updated_at: "2026-08-20T00:00:00Z",
        }),
      });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(
      await screen.findByRole("heading", { name: /website verification/i }),
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
    expect(window.location.search).toBe(`?session_id=${sessionId}`);
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/onboarding/sessions/00000000-0000-0000-0000-000000000001/verify-username-email",
      expect.objectContaining({
        method: "POST",
      }),
    );
  });

  test("clicking a website verification link analyzes the website and shows the analysis result", async () => {
    const sessionId = "00000000-0000-0000-0000-000000000001";
    window.history.pushState(
      {},
      "",
      `/verify-website-email?session_id=${sessionId}&token=email-token`,
    );
    let resolveAnalysis;
    const analysisResponse = new Promise((resolve) => {
      resolveAnalysis = resolve;
    });
    const verifiedSession = {
      session_id: sessionId,
      status: "draft",
      current_step: "analyzing",
      website_url: "https://example.co.ke",
      website_verification_email: "admin@example.co.ke",
      admin: validAdmin(),
      username_email_verified: true,
      website_email_verified: true,
      contact_info: [],
      provider_projects: {},
      created_at: "2026-08-20T00:00:00Z",
      updated_at: "2026-08-20T00:00:00Z",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          ...verifiedSession,
          status: "website_verification_pending",
          current_step: "website-email-verification",
          website_email_verified: false,
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => verifiedSession,
      })
      .mockImplementationOnce(() => analysisResponse);
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByText(/analyzing website/i)).toBeInTheDocument();
    resolveAnalysis({
      ok: true,
      json: async () => ({
        ...verifiedSession,
        status: "ready_for_review",
        current_step: "analysis",
        business_profile: {
          business_name: "John",
        },
        business_summary: "## Summary\n\nUse approved business details.",
        contact_info: [
          {
            kind: "website",
            label: "Website",
            url: "https://example.co.ke",
          },
        ],
        provider_projects: {},
        created_at: "2026-08-20T00:00:00Z",
        updated_at: "2026-08-20T00:00:00Z",
      }),
    });
    expect(await screen.findByLabelText(/business summary \/ faq/i)).toHaveValue(
      "## Summary\n\nUse approved business details.",
    );
    expect(screen.getByRole("toolbar", { name: /markdown formatting toolbar/i }))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: /bold/i })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
    expect(window.location.search).toBe(`?session_id=${sessionId}`);
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/onboarding/sessions/00000000-0000-0000-0000-000000000001/verify-website-email",
      expect.objectContaining({
        method: "POST",
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/api/onboarding/sessions/00000000-0000-0000-0000-000000000001/analyze-website",
      expect.objectContaining({
        method: "POST",
      }),
    );
    await userEvent.clear(screen.getByLabelText(/business summary \/ faq/i));
    await userEvent.type(
      screen.getByLabelText(/business summary \/ faq/i),
      "## FAQ\n\nCustomers can ask about bookings.",
    );
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        session_id: sessionId,
        status: "ready_for_review",
        current_step: "contact",
        website_url: "https://example.co.ke",
        website_verification_email: "admin@example.co.ke",
        admin: validAdmin(),
        username_email_verified: true,
        website_email_verified: true,
        business_profile: {
          business_name: "John",
        },
        business_summary: "## FAQ\n\nCustomers can ask about bookings.",
        contact_info: [
          {
            kind: "website",
            label: "Website",
            url: "https://example.co.ke",
          },
        ],
        provider_projects: {},
        created_at: "2026-08-20T00:00:00Z",
        updated_at: "2026-08-20T00:00:00Z",
      }),
    });
    await userEvent.click(screen.getByRole("button", { name: /review contact information/i }));
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/onboarding/sessions/00000000-0000-0000-0000-000000000001",
      expect.objectContaining({
        method: "PATCH",
        body: expect.stringContaining("Customers can ask about bookings."),
      }),
    );
  });

  test("refreshing an already-verified email link restores the saved step without reusing the token", async () => {
    const sessionId = "00000000-0000-0000-0000-000000000001";
    window.history.pushState(
      {},
      "",
      `/verify-username-email?session_id=${sessionId}&token=already-used-token`,
    );
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        session_id: sessionId,
        status: "awaiting_telegram_setup",
        current_step: "telegram-setup",
        website_url: "https://example.co.ke",
        website_verification_email: "admin@example.co.ke",
        admin: validAdmin(),
        username_email_verified: true,
        website_email_verified: true,
        business_profile: {
          business_name: "John",
        },
        business_summary: "Use approved business details.",
        contact_info: [
          {
            kind: "website",
            label: "Website",
            url: "https://example.co.ke",
          },
        ],
        provider_projects: {},
        created_at: "2026-08-20T00:00:00Z",
        updated_at: "2026-08-20T00:00:00Z",
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(
      await screen.findByText(/onboarding submitted for review/i),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      `/api/onboarding/sessions/${sessionId}`,
    ]);
    expect(window.location.pathname).toBe("/");
    expect(window.location.search).toBe(`?session_id=${sessionId}`);
  });

  test("back from analysis and forward again does not re-run website verification or analysis", async () => {
    const user = userEvent.setup();
    const sessionId = "00000000-0000-0000-0000-000000000001";
    window.history.pushState({}, "", `/?session_id=${sessionId}`);
    const analyzedSession = {
      session_id: sessionId,
      status: "ready_for_review",
      current_step: "analysis",
      website_url: "https://example.co.ke",
      website_verification_email: "admin@example.co.ke",
      admin: validAdmin(),
      username_email_verified: true,
      website_email_verified: true,
      analysis: {
        business_profile: {
          business_name: "John",
          website_url: "https://example.co.ke",
        },
        business_summary: "## Summary\n\nUse approved business details.",
        contact_info: [],
      },
      business_profile: {
        business_name: "John",
      },
      business_summary: "## Summary\n\nUse approved business details.",
      contact_info: [],
      provider_projects: {},
      created_at: "2026-08-20T00:00:00Z",
      updated_at: "2026-08-20T00:00:00Z",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => analyzedSession,
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByLabelText(/business summary \/ faq/i)).toHaveValue(
      "## Summary\n\nUse approved business details.",
    );
    await user.click(screen.getByRole("button", { name: /back/i }));
    expect(await screen.findByRole("heading", { name: /website verification/i }))
      .toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: /send website verification email/i }),
    );

    expect(await screen.findByLabelText(/business summary \/ faq/i)).toHaveValue(
      "## Summary\n\nUse approved business details.",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  test("continue from verified website email screen re-runs analysis", async () => {
    const user = userEvent.setup();
    const sessionId = "00000000-0000-0000-0000-000000000001";
    window.history.pushState({}, "", `/?session_id=${sessionId}`);
    const analyzedSession = {
      session_id: sessionId,
      status: "ready_for_review",
      current_step: "website-email-verification",
      website_url: "https://example.co.ke",
      website_verification_email: "admin@example.co.ke",
      admin: validAdmin(),
      username_email_verified: true,
      website_email_verified: true,
      analysis: {
        business_profile: {
          business_name: "John",
          website_url: "https://example.co.ke",
        },
        business_summary: "## Summary\n\nUse approved business details.",
        contact_info: [],
      },
      business_profile: {
        business_name: "John",
      },
      business_summary: "## Summary\n\nUse approved business details.",
      contact_info: [],
      provider_projects: {},
      created_at: "2026-08-20T00:00:00Z",
      updated_at: "2026-08-20T00:00:00Z",
    };
    const refreshedSession = {
      ...analyzedSession,
      current_step: "analysis",
      business_summary: "## Summary\n\nFresh analysis.",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => analyzedSession,
      })
      .mockResolvedValueOnce({
        ok: true,
        json: async () => refreshedSession,
      });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByRole("heading", { name: /verify website email/i }))
      .toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(await screen.findByLabelText(/business summary \/ faq/i)).toHaveValue(
      "## Summary\n\nFresh analysis.",
    );
    expect(fetchMock).toHaveBeenLastCalledWith(
      `/api/onboarding/sessions/${sessionId}/analyze-website?force=true`,
      expect.objectContaining({ method: "POST" }),
    );
  });
});

describe("contact information form", () => {
  test("deduplicates equivalent URLs with different labels", () => {
    render(
      <ContactInfoScreen
        session={{
          contact_info: [
            {
              kind: "website",
              label: "Website",
              url: "[https://example.co.ke/](https://example.co.ke/)",
            },
            {
              kind: "website",
              label: "Example website",
              url: "https://example.co.ke",
            },
            {
              kind: "website",
              label: "Start onboarding",
              url: "https://example.co.ke/onboarding",
            },
            {
              kind: "facebook",
              label: "Facebook",
              url: "[https://www.facebook.com/example](https://www.facebook.com/example)",
            },
          ],
          website_url: "https://example.co.ke",
        }}
        onBack={vi.fn()}
        onNext={vi.fn()}
        busy={false}
      />,
    );

    expect(screen.getByDisplayValue("https://example.co.ke/")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("Example website")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("https://example.co.ke")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("Start onboarding")).not.toBeInTheDocument();
    expect(screen.getByDisplayValue("https://www.facebook.com/example")).toBeInTheDocument();
  });

  test("renders found contact points as editable rows and allows adding more", async () => {
    const user = userEvent.setup();
    const onNext = vi.fn();
    render(
      <ContactInfoScreen
        session={{
          contact_info: [
            {
              kind: "website",
              label: "Website",
              url: "https://example.co.ke/",
            },
            {
              kind: "email",
              label: "Reservations",
              url: "mailto:hello@example.co.ke",
              value: "hello@example.co.ke",
            },
          ],
        }}
        onBack={vi.fn()}
        onNext={onNext}
        busy={false}
      />,
    );

    expect(screen.getByDisplayValue("website")).toBeInTheDocument();
    expect(screen.getByDisplayValue("mailto:hello@example.co.ke")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /delete contact point 1/i }),
    );
    expect(screen.queryByDisplayValue("https://example.co.ke/")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /add another contact/i }));

    const typeInputs = screen.getAllByLabelText(/type/i);
    const labelInputs = screen.getAllByLabelText(/label/i);
    const contactInputs = screen.getAllByLabelText(/url or value/i);
    await user.type(typeInputs[1], "whatsapp");
    await user.type(labelInputs[1], "WhatsApp");
    await user.type(contactInputs[1], "+254 700 000000");

    await user.click(screen.getByRole("button", { name: /submit for review/i }));

    expect(onNext).toHaveBeenCalledWith([
      {
        kind: "email",
        label: "Reservations",
        url: "mailto:hello@example.co.ke",
      },
      {
        kind: "whatsapp",
        label: "WhatsApp",
        url: "https://wa.me/254700000000",
        value: "+254 700 000000",
      },
    ]);
  });
});

describe("Telegram setup form", () => {
  test("does not expose system-generated secret fields for editing", () => {
    render(
      <TelegramScreen
        session={{
          telegram_setup_url: "https://example.co.ke/telegram-setup",
          business_profile: {
            business_name: "John",
          },
          admin: validAdmin(),
          contact_info: [],
        }}
        token="telegram-setup-token"
        onBack={vi.fn()}
        onSubmit={vi.fn()}
        busy={false}
      />,
    );

    expect(screen.getByLabelText(/telegram bot token/i)).toBeInTheDocument();
    expect(
      screen.queryByLabelText(/telegram webhook secret token/i),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/tenant secret name/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/kubernetes secret/i)).not.toBeInTheDocument();
  });
});
