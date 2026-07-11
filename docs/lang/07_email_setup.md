# Email Setup Guide

SpeakesQuery sends email alerts when a saved search returns results. This guide walks you through configuring outbound email so those alerts actually reach your inbox.

---

## How it works

When a saved search runs on its cron schedule and produces results, SpeakesQuery connects to an **SMTP relay** (an email-sending server) and delivers the alert. It does **not** run its own mail server - it hands the message to a relay that has the reputation and DNS records needed for reliable delivery.

The recommended (and default) relay is **Gmail SMTP**, which is free for any Gmail account.

---

## Quick start - Gmail with App Password

This is the fastest path and works for any Gmail or Google Workspace account.

### Step 1 - Enable 2-Step Verification

App Passwords require 2-Step Verification on your Google account.

1. Go to [myaccount.google.com/security](https://myaccount.google.com/security).
2. Under **"How you sign in to Google"**, click **2-Step Verification**.
3. Follow the prompts to enable it (if not already on).

### Step 2 - Generate an App Password

1. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
2. Under **"App name"**, type `SpeakesQuery` (or anything you like).
3. Click **Create**.
4. Google shows a 16-character password (e.g. `abcd efgh ijkl mnop`). Copy it - you will not see it again.

### Step 3 - Enter credentials in SpeakesQuery

Open the **Settings** tab in SpeakesQuery and scroll to the **Email (SMTP)** section:

| Field | Value |
|-------|-------|
| SMTP Server | `smtp.gmail.com` (default) |
| Port | `587` (default) |
| Username | Your full Gmail address, e.g. `you@gmail.com` |
| Password / App Password | The 16-character App Password from Step 2 |
| From Address | Leave blank (defaults to your Gmail address) |
| Use STARTTLS | Checked (default) |

Click **Save Settings**, then click **Send Test Email** to verify.

---

## Alternative SMTP providers

Any SMTP relay works. Here are a few common free options:

| Provider | Server | Port | Notes |
|----------|--------|------|-------|
| **Gmail** | `smtp.gmail.com` | 587 | App Password required |
| **Outlook / Hotmail** | `smtp-mail.outlook.com` | 587 | Regular password works |
| **Yahoo Mail** | `smtp.mail.yahoo.com` | 587 | App Password required |
| **Zoho Mail** | `smtp.zoho.com` | 587 | Regular password works |
| **SendGrid** (free tier) | `smtp.sendgrid.net` | 587 | API key as password, `apikey` as username |
| **Mailgun** (free tier) | `smtp.mailgun.org` | 587 | Sandbox domain included |

To use any of these, simply change the **SMTP Server**, **Port**, **Username**, and **Password** fields on the Settings page.

---

## Configuration methods

SpeakesQuery reads SMTP settings from two sources, with this precedence:

1. **Environment variables** (`.env` file) - take priority if set **and not a known placeholder**.
2. **Settings page** (`global_settings.yaml`) - used as fallback.

Most users should just use the Settings page. Environment variables are useful for Docker deployments or CI/CD where you want config injected externally.

> ⚠️ **`.env` auto-loads in Docker and PyCharm.** `desktop_app/docker-compose.yml` pulls the project-root `.env` into the container via `env_file:`, and PyCharm's default Python run config also loads it. Any uncommented `SMTP_*` line in `.env` will therefore **silently override** whatever you save in the UI. If you don't want that, either leave every `SMTP_*` line commented out in `.env` (the shipped default) or keep them uncommented with real values that match what's in the UI.

### Placeholder detection (`535 BadCredentials` safety net)

The shipped `.env.example` contains literal placeholder values like `SMTP_USER=you@gmail.com` and `SMTP_PASSWORD=your_16_char_app_password`. Before this safety net, `install.sh`'s `cp .env.example .env` would produce a `.env` whose placeholders got loaded by docker-compose and silently overrode any UI-saved credentials, producing `535 5.7.8 BadCredentials` on every send.

The shipped template is now lead-`#`-commented so a straight copy does not inject anything. In addition, the resolver in `query_engine/Alert.py` exact-matches a short list of known placeholders and treats them as unset - a one-time `[!]` WARN is logged on first use so a misconfigured `.env` shouts instead of silently breaking AUTH. The list is exact-match and conservative (real credentials never collide with literals like `your_16_char_app_password`).

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SMTP_SERVER` | `smtp.gmail.com` | SMTP relay hostname |
| `SMTP_PORT` | `587` | SMTP port |
| `SMTP_USER` | *(none)* | Username / email address |
| `SMTP_PASSWORD` | *(none)* | Password or App Password |
| `SMTP_FROM` | Same as `SMTP_USER` | From address |
| `SMTP_STARTTLS` | `true` | Use STARTTLS encryption |

---

## Splitting customer recipients from admin error notices (Wave 5, 2026-04-26)

In production, the customer-facing recipient list (`email_address`) is often a paid mailing list, and you do **not** want them to receive failure / diagnostic emails like "alert group dispatch failed: Claude API rate-limited." Wave 5 added a per-AG and per-search **Admin Error Email** field that splits the routing:

| Field | Recipient of | Set when |
|---|---|---|
| `email_address` | The analyst brief (success path), or the prompt (in `prompt_only` mode) | Always - required for delivery |
| `admin_error_email` | Failure / diagnostic notices only | Optional - set in production |

**How the routing decides where errors go** (alert groups today; saved-search alert paths will follow when their delivery is next refactored):

1. The AG's `admin_error_email` field, if set
2. The global `alert_group_failure_email_to` setting (Settings → Alert Groups), if set
3. `smtp_from` (whoever Gmail is sending from)
4. `smtp_user` as a last resort

If all four are empty, the failure notice is logged + skipped - never silently sent to the customer list.

**Where to configure:**
- **Per AG** - Alert Groups tab → Edit → "Admin Error Email" field directly below "Email Address"
- **Per saved search** - Create Search tab → "Admin Error Email" field directly below "To Email Address"
- **Global fallback** - Settings → Alert Groups → "Failure Email To"

The customer-facing field never receives error emails when an admin override is set. This invariant is pinned by `tests/test_wave5_admin_error_email.py::TestAGFailureRouting`.

---

## Troubleshooting

### "SMTP credentials incomplete - missing: …"

The error message tells you exactly which field is empty (username, password, or both). Fill in the Email section on the Settings page and click **Send Test Email** - the button auto-saves your SMTP settings before sending, so there is no need to click Save first. Alternatively, set `SMTP_USER` and `SMTP_PASSWORD` in `.env`.

### Test email fails with authentication error

- **Gmail**: Make sure you are using an **App Password**, not your regular Google password. Regular passwords are blocked for SMTP.
- **Other providers**: Verify the username and password are correct. Some providers require you to enable "less secure app access" or generate a separate SMTP password.
- **`.env` override check (post‑fix):** if the server log shows `user=you@gmail.com pw_shape=len=25 ws=n alnum=n` or similar nonsense, the `.env` file has uncommented `SMTP_*` lines with placeholder values that are overriding your UI settings. Starting with the 2026-04-18 fix those are auto-detected and ignored with a `[!]` WARN in the server log; to silence the warning, open `.env` and comment out or delete the `SMTP_USER=` / `SMTP_PASSWORD=` lines. `POST /api/email/diagnose` returns `saved_config.env_placeholders_ignored` showing exactly which placeholders were skipped.

### Test email succeeds but alert never arrives

- Check the **Saved Searches** tab - is the search enabled (not disabled)?
- Does the query actually return results? Run it manually on the Query tab.
- Check your spam/junk folder.
- Verify the `email_address` on the saved search is correct.

### Connection timeout

- Confirm the SMTP server and port are correct.
- Port `587` requires STARTTLS (the default). Port `465` uses implicit SSL - uncheck "Use STARTTLS" for that.
- If you are behind a corporate firewall, outbound port 587 may be blocked. Try port 465, or ask your network admin.

### Can I send email without a registered domain?

Technically yes - you could connect directly to a recipient's mail server without a relay. Practically, **no**. Without SPF, DKIM, and DMARC DNS records (which require a domain you control), Gmail, Outlook, and virtually every other provider will reject the message or send it to spam. Using a relay like Gmail solves this because Google's servers already have established reputation and valid DNS records.

---

## Security notes

- SMTP credentials are stored in `global_settings.yaml`, which is **gitignored** and local to your machine.
- Environment variables (`.env`) are also gitignored.
- Emails are sent over TLS-encrypted connections (STARTTLS on port 587, or implicit TLS on port 465).
- App Passwords can be revoked at any time from your Google account.
