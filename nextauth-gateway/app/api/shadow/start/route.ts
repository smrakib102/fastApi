import { NextResponse } from "next/server";

const SHADOW_STATE_COOKIE = "shadow_oauth_request_id";

function getPublicBaseUrl(request: Request): string {
  const envUrl = (process.env.NEXTAUTH_URL || "").trim();
  if (envUrl) {
    return envUrl.replace(/\/$/, "");
  }
  const proto = request.headers.get("x-forwarded-proto") || "https";
  const host = request.headers.get("x-forwarded-host") || request.headers.get("host") || "";
  if (!host) {
    return "";
  }
  return `${proto}://${host}`.replace(/\/$/, "");
}

export async function GET(request: Request) {
  const requestUrl = new URL(request.url);
  const { searchParams } = requestUrl;
  const requestId = (searchParams.get("request_id") || "").trim();
  const provider = (searchParams.get("provider") || "google").trim();
  const callbackUrl = (searchParams.get("callbackUrl") || "").trim();

  if (!requestId) {
    return new Response("missing_request_id", { status: 400 });
  }

  const baseUrl = getPublicBaseUrl(request) || requestUrl.origin;
  const target = new URL("/api/auth/signin", baseUrl);
  if (callbackUrl) {
    target.searchParams.set("callbackUrl", callbackUrl);
  }

  const response = NextResponse.redirect(target.toString());
  response.cookies.set({
    name: SHADOW_STATE_COOKIE,
    value: requestId,
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: "/",
    maxAge: 600
  });
  return response;
}
