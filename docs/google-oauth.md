# Google OAuth setup

**This project ships with zero credentials of any kind.** Every user needs their own — it takes about 5 minutes and costs nothing.

## Create your credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create (or select) a project.
2. **APIs & Services → Library** → search **"YouTube Data API v3"** → **Enable**.
3. **APIs & Services → Credentials** → **+ Create Credentials** → **OAuth client ID**.
   - If prompted, configure the OAuth consent screen first (choose "External," fill in the required fields — this is fine for personal use, you don't need to publish/verify the app).
   - Application type: **Desktop app**. Name it anything.
   - Click **Create** — a popup shows your **Client ID** and **Client Secret**. Copy both (or find them again anytime under Credentials).
4. In this project folder:
   ```bash
   cp .env.example .env
   ```
   Paste your Client ID and Client Secret into `.env`.

## Sign in

```bash
python scripts/youtube_subscriptions.py auth
python scripts/youtube_subscriptions.py test   # confirms the token works
```

The token (including the refresh token) is stored in `scripts/.credentials/youtube_token.json` — `chmod 600`, gitignored. Access tokens expire after about an hour and refresh automatically.

## When the refresh token dies (`invalid_grant`)

Google revokes refresh tokens occasionally (consent-screen changes, inactivity, user revocation at [myaccount.google.com/permissions](https://myaccount.google.com/permissions)). The `auth` command then needs the interactive browser flow again.

**Known pitfall:** `InstalledAppFlow.authorization_url()` can build the consent URL *without* a `redirect_uri` parameter even when one is configured in the client JSON — Google then rejects it with `Fehler 400: invalid_request` ("Missing required parameter: redirect_uri"). If that happens, build the authorization URL manually (PKCE: generate a code verifier + S256 challenge, put `redirect_uri=http://localhost` explicitly in the query string), have the user open it, and exchange the returned `code` at `https://oauth2.googleapis.com/token` with the `code_verifier`. The resulting token JSON goes into `scripts/.credentials/youtube_token.json` with the same shape the `auth` command writes.
