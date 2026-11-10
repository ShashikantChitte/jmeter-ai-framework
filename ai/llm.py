#!/usr/bin/env python3
"""Thin, dependency-free LLM client (Anthropic Messages API) + redaction.

Env:  ANTHROPIC_API_KEY   key (absent => offline mode, everything still works with rules/statistics)
      AI_MODE             auto (default) | offline | llm   (llm = fail if no key)
      AI_MODEL            default claude-sonnet-5-5  (check the current model names in Anthropic docs)
      AI_API_URL          default https://api.anthropic.com/v1/messages  (override for gateways / tests)
      AI_MAX_INPUT_CHARS  default 60000  (cost guard)
Design rules: only aggregated evidence is sent (never raw JTL, bodies or credentials) and it is redacted first.
"""
import json, os, re, time, urllib.error, urllib.request

API_URL = os.environ.get("AI_API_URL", "https://api.anthropic.com/v1/messages")
MODEL = os.environ.get("AI_MODEL", "claude-sonnet-5-5")
MAX_CHARS = int(os.environ.get("AI_MAX_INPUT_CHARS", "60000"))

_REDACT = [
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*"), "<jwt>"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{10,}"), "Bearer <token>"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "<api-key>"),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "<hex>"),
    (re.compile(r"(?i)(password|passwd|secret|token|apikey|api_key)(\"?\s*[:=]\s*\"?)[^\s\",&]+"), r"\1\2<redacted>"),
]


def redact(text):
    for pat, rep in _REDACT:
        text = pat.sub(rep, text)
    return text


def mode():
    return os.environ.get("AI_MODE", "auto").lower()


def available():
    if mode() == "offline":
        return False
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    if mode() == "llm":
        raise SystemExit("AI_MODE=llm but ANTHROPIC_API_KEY is not set")
    return False


def call(system, user, max_tokens=3000, model=None, timeout=90, retries=2):
    """Returns the text of the answer. Raises RuntimeError on failure (callers fall back to offline logic)."""
    user = redact(user)
    if len(user) > MAX_CHARS:
        user = user[:MAX_CHARS] + "\n...[truncated by AI_MAX_INPUT_CHARS]"
    body = json.dumps({"model": model or MODEL, "max_tokens": max_tokens, "system": system,
                       "messages": [{"role": "user", "content": user}]}).encode()
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01",
               "x-api-key": os.environ.get("ANTHROPIC_API_KEY", "")}
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
            if e.code not in (429, 500, 502, 503, 529):
                break
        except Exception as e:  # network, timeout, bad json
            last = str(e)
        time.sleep(2 ** attempt)
    raise RuntimeError(f"LLM call failed: {last}")


def extract_json(text):
    """Parse a JSON object from a model answer (handles ```json fences / leading prose)."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            text = text[i:j + 1]
    return json.loads(text)
