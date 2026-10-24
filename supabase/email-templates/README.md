# Supabase auth emails

## Customising the confirmation email

1. Open the [Supabase Dashboard](https://supabase.com/dashboard) and select the
   project.
2. Go to **Authentication → Emails → Templates** (older dashboards:
   **Authentication → Email Templates**).
3. Select **Confirm sign up**.
4. Set the subject, for example:

   ```
   Confirm your email for Enterprise AI
   ```

5. Paste the body of [`confirm-signup.html`](./confirm-signup.html) into the
   message editor. Only the `<body>` contents are needed; the dashboard supplies
   the surrounding document.
6. Press **Save**. The next confirmation email uses the new template — no
   redeploy of this application is required.

The template uses `{{ .ConfirmationURL }}` for the button and the copyable
fallback link, `{{ .Email }}` for the recipient, and `{{ .SiteURL }}` in the
footer. The full variable list is in the
[Supabase email templates documentation](https://supabase.com/docs/guides/auth/auth-email-templates).

## The limits that templates do not change

The built-in Supabase SMTP server is for evaluation only:

- **2 messages per hour** per project.
- **Only addresses on the project's organisation team** can receive mail.

Editing the template changes neither limit. A real rollout needs:

- **Custom SMTP** — **Authentication → Emails → SMTP Settings**. Any provider
  (Resend, Postmark, SendGrid, Amazon SES, …) works; verify the sending domain
  first.
- **Rate limits** — **Authentication → Rate Limits**, then raise *Emails per
  hour* to match the provider's quota.

For local testing, it is simpler to turn confirmation off:
**Authentication → Sign In / Providers → Email → Confirm email**. Re-enable it
before production.

## Why the template is dark

It mirrors the product's navy and teal palette (see `frontend/src/theme.css`).
All styling is inline and table-based so it survives Gmail, Outlook and Apple
Mail. Some clients force their own dark or light rendering; the layout stays
readable in both because the text colours are declared explicitly.
