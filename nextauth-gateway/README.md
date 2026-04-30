# NextAuth Shadow OAuth Gateway

This service acts as a dedicated OAuth gateway for the Shadow OAuth system. It runs separately from the FastAPI app and posts OAuth credentials to the vault callback endpoint.

## Quick Start

1) Copy `.env.example` to `.env` and fill in values.
2) Install dependencies:

```bash
npm install
```

3) Run locally:

```bash
npm run dev
```

## Required Environment Variables

- `NEXTAUTH_URL`
- `NEXTAUTH_SECRET`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GITHUB_CLIENT_ID` (optional)
- `GITHUB_CLIENT_SECRET` (optional)
- `NEXTAUTH_VAULT_CALLBACK_URL` (e.g. https://api.fgvideoschool.com/oauth/callback)
- `NEXTAUTH_CALLBACK_SECRET` (shared secret header for the vault callback)
- `NEXTAUTH_SIGNATURE_SECRET` (primary HMAC signing secret)

## OAuth Request ID Handling

The FastAPI service initiates OAuth and redirects to:

```
/api/auth/signin/google?state=<oauth_request_id>&callbackUrl=<post-auth-url>
```

This gateway extracts the `oauth_request_id` from the `state` query or the stored callback URL. If it cannot determine the request id, it will not post to the vault callback.

## Notes

- Only Google provider results are sent to the vault callback. Other providers are ignored by default.
- The vault callback enforces HMAC + timestamp validation.
- OAuth request validation rules are loaded from ../contract/oauth_contract.json. Keep this file in sync when deploying to a separate repo.
