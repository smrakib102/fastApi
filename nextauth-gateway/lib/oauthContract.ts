import fs from "fs";
import path from "path";

type OAuthContract = {
  oauth_request_id: {
    regex: string;
    min_length: number;
    max_length: number;
    error_codes: {
      invalid: string;
      unknown: string;
    };
  };
  metrics: {
    invalid_state_rejected: string;
    unknown_oauth_request_id: string;
  };
};

let cached: OAuthContract | null = null;

function loadContract(): OAuthContract {
  if (cached) {
    return cached;
  }
  const contractPath = path.resolve(process.cwd(), "..", "contract", "oauth_contract.json");
  const raw = fs.readFileSync(contractPath, "utf-8");
  cached = JSON.parse(raw) as OAuthContract;
  return cached;
}

export function getOAuthRequestIdRegex(): RegExp {
  const { regex } = loadContract().oauth_request_id;
  return new RegExp(regex);
}

export function getOAuthErrorCode(code: "invalid" | "unknown"): string {
  return loadContract().oauth_request_id.error_codes[code];
}

export function getOAuthMetric(metric: "invalid_state_rejected" | "unknown_oauth_request_id"): string {
  return loadContract().metrics[metric];
}
