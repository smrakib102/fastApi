import { NextResponse } from "next/server";

const SHADOW_STATE_COOKIE = "shadow_oauth_request_id";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const requestId = (searchParams.get("request_id") || "").trim();
  const provider = (searchParams.get("provider") || "google").trim();
  const callbackUrl = (searchParams.get("callbackUrl") || "").trim();

  if (!requestId) {
    return new Response("missing_request_id", { status: 400 });
  }

  const target = new URL(`/api/auth/signin/${provider}`, request.url);
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
