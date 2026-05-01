import NextAuth from "next-auth";
import GoogleProvider from "next-auth/providers/google";
import GitHubProvider from "next-auth/providers/github";
import { cookies } from "next/headers";
import crypto from "crypto";
import {
  getOAuthErrorCode,
  getOAuthMetric,
  getOAuthRequestIdRegex
} from "../../../../lib/oauthContract";

const handler = NextAuth({
  providers: [
    GoogleProvider({
      clientId: process.env.GOOGLE_CLIENT_ID || "",
      clientSecret: process.env.GOOGLE_CLIENT_SECRET || "",
      authorization: {
        params: {
          access_type: "offline",
          prompt: "consent"
        }
      }
    }),
    GitHubProvider({
      clientId: process.env.GITHUB_CLIENT_ID || "",
      clientSecret: process.env.GITHUB_CLIENT_SECRET || ""
    })
  ],
  secret: process.env.NEXTAUTH_SECRET,
  callbacks: {
    async signIn({ account }) {
      if (!account) {
        return true;
      }
      if (account.provider !== "google") {
        return true;
      }
      console.warn("shadow_signin_entry", {
        provider: account.provider,
        has_account_state: Boolean((account as { state?: string }).state)
      });
      try {
        const callbackUrl = cookies().get("next-auth.callback-url")?.value;
        let callbackRequestId = "";
        if (callbackUrl) {
          try {
            const parsed = new URL(callbackUrl);
            callbackRequestId = parsed.searchParams.get("oauth_request_id")?.trim() || "";
            if (parsed.searchParams.has("state") || parsed.searchParams.has("oauth_request_id")) {
              console.warn("callback_url_state_ignored");
            }
            console.warn("shadow_callback_url", {
              has_callback_url: true,
              has_callback_request_id: Boolean(callbackRequestId)
            });
          } catch {
            console.warn("callback_url_parse_failed");
          }
        }

        const rawState = (account as { state?: string }).state || "";
        let state = rawState.trim();
        let cookieState = "";
        if (!state) {
          if (callbackRequestId) {
            state = callbackRequestId;
          }
        }
        if (!state) {
          cookieState = cookies().get("shadow_oauth_request_id")?.value?.trim() || "";
          state = cookieState;
        }
        if (!state) {
          console.warn("shadow_state_missing", {
            has_account_state: Boolean(rawState),
            has_cookie_state: Boolean(cookieState),
            has_callback_request_id: Boolean(callbackRequestId)
          });
        }
        const stateRegex = getOAuthRequestIdRegex();
        if (!state || !stateRegex.test(state)) {
          console.warn(getOAuthErrorCode("invalid"));
          console.warn(getOAuthMetric("invalid_state_rejected"));
          return true;
        }
        const payload = {
          oauth_request_id: state,
          provider: "google",
          provider_account_id: account.providerAccountId,
          account_email: (account as { email?: string }).email || null,
          access_token: account.access_token || null,
          refresh_token: account.refresh_token || null,
          token_type: account.token_type || null,
          scope: account.scope || null,
          expires_at: account.expires_at || null
        };
        const body = JSON.stringify(payload);
        const timestamp = Math.floor(Date.now() / 1000).toString();
        const message = `${timestamp}.${body}`;
        const signature = crypto
          .createHmac("sha256", process.env.NEXTAUTH_SIGNATURE_SECRET || "")
          .update(message)
          .digest("hex");

        const vaultUrl = process.env.NEXTAUTH_VAULT_CALLBACK_URL || "";
        console.warn("vault_callback_attempt", {
          has_vault_url: Boolean(vaultUrl),
          has_oauth_request_id: Boolean(state)
        });
        const response = await fetch(vaultUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-NextAuth-Secret": process.env.NEXTAUTH_CALLBACK_SECRET || "",
            "X-Timestamp": timestamp,
            "X-Signature": signature
          },
          body
        });
        console.warn("vault_callback_response", {
          status: response.status
        });
        if (!response.ok) {
          try {
            const payload = await response.json();
            if (payload?.detail === getOAuthErrorCode("unknown")) {
              console.warn(getOAuthErrorCode("unknown"));
              console.warn(getOAuthMetric("unknown_oauth_request_id"));
            }
          } catch {
            console.warn("vault_callback_non_json_error");
          }
        }
      } catch (error) {
        console.warn("vault_callback_exception", {
          message: error instanceof Error ? error.message : String(error)
        });
        return true;
      }
      return true;
    }
  }
});

export { handler as GET, handler as POST };
