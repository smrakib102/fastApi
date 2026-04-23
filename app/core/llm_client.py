import json

import httpx


OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
GEMINI_GENERATE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


class LLMError(RuntimeError):
    pass


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 4))


def call_openai_chat(api_key: str, model: str, messages: list[dict], timeout: int = 45) -> tuple[str, int]:
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
    }
    response = httpx.post(OPENAI_CHAT_URL, headers=headers, json=payload, timeout=timeout)
    if response.status_code != 200:
        raise LLMError(f"OpenAI error: {response.status_code} {response.text}")
    data = response.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content")
    if not content:
        raise LLMError("OpenAI response missing content")
    usage = data.get("usage") or {}
    tokens = usage.get("total_tokens") or _estimate_tokens(content)
    return content, int(tokens)


def call_gemini(api_key: str, model: str, prompt: str, timeout: int = 45) -> tuple[str, int]:
    url = GEMINI_GENERATE_URL.format(model=model)
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ]
    }
    response = httpx.post(url, params={"key": api_key}, json=payload, timeout=timeout)
    if response.status_code != 200:
        raise LLMError(f"Gemini error: {response.status_code} {response.text}")
    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise LLMError("Gemini response missing candidates")
    content = candidates[0].get("content", {}).get("parts", [{}])[0].get("text")
    if not content:
        raise LLMError("Gemini response missing content")
    usage = data.get("usageMetadata") or {}
    tokens = usage.get("totalTokenCount") or _estimate_tokens(content)
    return content, int(tokens)


def serialize_messages(messages: list[dict]) -> str:
    return json.dumps(messages, ensure_ascii=True)
