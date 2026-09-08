"""Explicit REST adapters. No SDK retries, environment routing, or CLI fallback."""
from __future__ import annotations
import json
import math
import socket
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, ROUND_CEILING

from .core import SCHEMA, SYSTEM


@dataclass(frozen=True)
class Policy:
    model: str
    key: str
    origin: str
    input_price: str
    output_price: str
    cached_price: str
    cache_write_multiplier: str
    billed_output_bound: int | None
    bound_source: str
    price_source: str
    reviewed: str = "2026-09-07"


POLICIES = {
    "luna": Policy("gpt-5.6-luna", "OPENAI_API_KEY", "https://api.openai.com",
                   "0.20", "1.20", "0.02", "1.25", 128000,
                   "https://developers.openai.com/api/reference/cli/resources/responses/methods/create",
                   "https://developers.openai.com/api/docs/models/gpt-5.6-luna"),
    "flash": Policy("gemini-3.8-flash", "GEMINI_API_KEY", "https://generativelanguage.googleapis.com",
                    "0.75", "3.75", "0.075", "1", None,
                    "https://ai.google.dev/api/generate-content#GenerationConfig",
                    "https://ai.google.dev/gemini-api/docs/pricing"),
    "sonnet": Policy("claude-sonnet-5", "ANTHROPIC_API_KEY", "https://api.anthropic.com",
                     "2", "10", "0.20", "1.25", 128000,
                     "https://platform.claude.com/docs/en/models/sonnet-5/whats-new-sonnet-5",
                     "https://platform.claude.com/docs/en/about-claude/pricing"),
}


def policy_issues(policies=None, today=None):
    policies = POLICIES if policies is None else policies
    today = today or date.today()
    issues = []
    for name, policy in policies.items():
        if policy.billed_output_bound is None:
            issues.append(f"{name}: combined billable output/thinking ceiling is unverified")
        age = (today - date.fromisoformat(policy.reviewed)).days
        if age < 0 or age > 7:
            issues.append(f"{name}: pricing and billing documentation needs re-verification")
    return issues


def nanodollars(tokens, price):
    return int((Decimal(tokens) * Decimal(price) * 1000).to_integral_value(rounding=ROUND_CEILING))


def reservation(policy, counted_input):
    if policy.billed_output_bound is None:
        raise ValueError("unverified_billing_bound")
    if type(counted_input) is not int or not 0 <= counted_input <= 200_000:
        raise ValueError("input_token_count_outside_benchmark_limit")
    # The count request includes system prompt/schema. Reserve extra input headroom;
    # never apply caching savings or long-context pricing assumptions to large inputs.
    input_bound = math.ceil(counted_input * 1.1) + 1024
    price = Decimal(policy.input_price) * Decimal(policy.cache_write_multiplier)
    return nanodollars(input_bound, price) + nanodollars(policy.billed_output_bound, policy.output_price)


class APIError(Exception):
    """Only safe categorical details; never include a URL, header, or response body."""
    def __init__(self, kind, code=None):
        self.kind, self.code = kind, code
        super().__init__(kind + (f"_{code}" if code else ""))

    @property
    def retryable(self):
        return self.code in (429, 500, 502, 503, 504)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_json(url, headers, body=None, timeout=600):
    request = urllib.request.Request(url, headers={**headers, "Content-Type": "application/json"},
                                     data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            return json.loads(response.read(8_000_000))
    except urllib.error.HTTPError as exc:
        raise APIError("http", exc.code) from None
    except (socket.timeout, TimeoutError):
        raise APIError("timeout_unknown_usage") from None
    except urllib.error.URLError:
        raise APIError("transport_unknown_usage") from None
    except (ValueError, OSError):
        raise APIError("response_unreadable_unknown_usage") from None


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("missing_or_invalid_usage")
    return value


class Adapter:
    def __init__(self, name, keys, transport=http_json, policy=None):
        self.name = name
        self.policy = policy or POLICIES[name]
        self.transport = transport
        key = keys[self.policy.key]
        self.headers = ({"Authorization": "Bearer " + key} if name == "luna" else
                        {"x-goog-api-key": key} if name == "flash" else
                        {"x-api-key": key, "anthropic-version": "2023-06-01"})

    def request(self, path, body=None):
        return self.transport(self.policy.origin + path, self.headers, body)

    def metadata(self):
        path = ("/v1beta/models/" if self.name == "flash" else "/v1/models/") + self.policy.model
        data = self.request(path)
        actual = data.get("id", data.get("name", ""))
        if actual.removeprefix("models/") != self.policy.model:
            raise APIError("unexpected_model_identity")
        return {"model": self.policy.model, "available": True}

    def body(self, user):
        model = self.policy.model
        if self.name == "luna":
            return {"model": model, "instructions": SYSTEM, "input": user,
                    "reasoning": {"effort": "medium"},
                    "store": False, "truncation": "disabled", "tools": [],
                    "text": {"format": {"type": "json_schema", "name": "resume_match",
                                        "schema": SCHEMA, "strict": True}}}
        if self.name == "sonnet":
            return {"model": model, "system": SYSTEM,
                    "messages": [{"role": "user", "content": user}],
                    # Anthropic requires max_tokens; use the model's full capacity.
                    "max_tokens": 128000, "thinking": {"type": "adaptive"},
                    "output_config": {"effort": "medium",
                                      "format": {"type": "json_schema", "schema": SCHEMA}}}
        return {"systemInstruction": {"parts": [{"text": SYSTEM}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"thinkingConfig": {"thinkingLevel": "MEDIUM"},
                                     "responseMimeType": "application/json",
                                     "responseJsonSchema": SCHEMA}}

    def count_input(self, user):
        body = self.body(user)
        if self.name == "luna":
            body.pop("store")
            data = self.request("/v1/responses/input_tokens", body)
            return _count(data.get("input_tokens"))
        if self.name == "sonnet":
            body.pop("max_tokens")
            data = self.request("/v1/messages/count_tokens", body)
            return _count(data.get("input_tokens"))
        # Include the full generate request, including system instruction and schema.
        data = self.request(f"/v1beta/models/{self.policy.model}:countTokens",
                            {"generateContentRequest": {"model": "models/" + self.policy.model, **body}})
        return _count(data.get("totalTokens"))

    def generate(self, user, *, accept_unverified_billing=False):
        # Defense in depth: direct adapter calls cannot bypass the unverified bound.
        if self.policy.billed_output_bound is None and not accept_unverified_billing:
            raise ValueError("unverified_billing_bound")
        path = ("/v1/responses" if self.name == "luna" else "/v1/messages" if self.name == "sonnet"
                else f"/v1beta/models/{self.policy.model}:generateContent")
        return self.request(path, self.body(user))

    def decode(self, data):
        if self.name == "luna":
            if data.get("status") != "completed":
                raise ValueError("incomplete_or_refused_response")
            content = [c for item in data.get("output", []) if item.get("type") == "message"
                       for c in item.get("content", [])]
            if any(c.get("type") == "refusal" for c in content):
                raise ValueError("refused_response")
            text = "".join(c.get("text", "") for c in content if c.get("type") == "output_text")
        elif self.name == "sonnet":
            if data.get("stop_reason") != "end_turn":
                raise ValueError("incomplete_or_refused_response")
            text = "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text")
        else:
            candidates = data.get("candidates", [])
            if len(candidates) != 1 or candidates[0].get("finishReason") != "STOP":
                raise ValueError("incomplete_or_refused_response")
            text = "".join(c.get("text", "") for c in candidates[0].get("content", {}).get("parts", [])
                           if not c.get("thought"))
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            raise ValueError("invalid_response_json") from None

    def usage(self, data):
        """Return normalized counts; reasoning is not added twice to billed output."""
        u = data.get("usageMetadata" if self.name == "flash" else "usage")
        if not isinstance(u, dict):
            raise ValueError("missing_or_invalid_usage")
        if self.name == "flash":
            inp, out = _count(u.get("promptTokenCount")), _count(u.get("candidatesTokenCount"))
            thinking, cached = _count(u.get("thoughtsTokenCount", 0)), _count(u.get("cachedContentTokenCount", 0))
            out += thinking
            write = 0
        else:
            inp, out = _count(u.get("input_tokens")), _count(u.get("output_tokens"))
            thinking = _count(u.get("output_tokens_details", {}).get(
                "reasoning_tokens" if self.name == "luna" else "thinking_tokens", 0))
            if self.name == "luna":
                cached = _count(u.get("input_tokens_details", {}).get("cached_tokens", 0))
                write = _count(u.get("input_tokens_details", {}).get("cache_write_tokens", 0))
            else:
                cached, write = _count(u.get("cache_read_input_tokens", 0)), _count(u.get("cache_creation_input_tokens", 0))
                inp += cached + write  # Anthropic input_tokens excludes cache reads/writes.
        if cached + write > inp or thinking > out:
            raise ValueError("inconsistent_usage")
        p = self.policy
        cost = (nanodollars(inp - cached - write, p.input_price)
                + nanodollars(cached, p.cached_price)
                + nanodollars(write, Decimal(p.input_price) * Decimal(p.cache_write_multiplier))
                + nanodollars(out, p.output_price))
        return {"input": inp, "output": out, "thinking": thinking, "cached": cached,
                "cache_write": write, "cost": cost}


def preflight(keys):
    result = {"policies": {k: asdict(v) for k, v in POLICIES.items()},
              "issues": policy_issues(), "access": {}}
    for name in POLICIES:
        try:
            adapter = Adapter(name, keys)
            result["access"][name] = adapter.metadata()
            result["access"][name]["synthetic_input_tokens"] = adapter.count_input(
                "Synthetic adapter check: an empty posting with no resume evidence.")
        except APIError as e:
            result["access"].setdefault(name, {"available": False})["error"] = str(e)
            result["issues"].append(f"{name}: access or token-count contract check failed ({e})")
        except ValueError:
            result["issues"].append(f"{name}: token count unavailable")
    result["ready"] = not result["issues"]
    return result
