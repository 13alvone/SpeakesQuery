"""
Model Validation
────────────────
Static validators for model-registry YAML records.

A model record describes a single LLM endpoint that the slice-2 router
will dispatch to: ``{id, provider, model_name, endpoint?, costs, ...}``.
The model registry is consumed by ``analyzers/llm_router.py`` (slice 2)
and the ``| llm`` / ``| llm_batch`` SPQL pipes (slice 4+).

The id field doubles as the filename stem (sanitized) so it must be
filename-safe AND unique. Cost fields are decimal USD per million
tokens to match the analyzers/claude_client._PRICING table convention.
"""

import re


# Model id - filename-safe identifier (lowercase letters, digits, hyphen,
# underscore, dot for version markers like "claude-sonnet-4-6").
_ID_REGEX = re.compile(r"^[a-z0-9._\-]+$")

# Allowed providers. Phase 2:
#   * `anthropic` - Cloud Claude API. Routed through the existing
#     analyzers/claude_client.py wrapper; SDK handles endpoint defaults.
#   * `ollama` - Local Ollama daemon, JSON chat protocol over HTTP at
#     `/api/chat`. Endpoint required (typically http://localhost:11434).
#   * `gemini` - Google Gemini. Endpoint optional (SDK default).
#   * `lmstudio` - Self-hosted LLM via LM Studio (https://lmstudio.ai).
#     LM Studio exposes a Chat Completions HTTP API at port 1234 by
#     default. Endpoint required (no SDK default - operators set the
#     URL of their LM Studio host, typically a LAN IP for a dedicated
#     machine). Designed for the cost-cascade pattern's "bigger local
#     model on a dedicated box" tier - frees the SpeakesQuery host from
#     the LLM's RAM/GPU footprint at the cost of a network round-trip.
#
# OpenAI deliberately omitted: SpeakesQuery does not interact with
# OpenAI's company or servers as a matter of principle (user direction
# 2026-05-08; slice 2.5). LM Studio remains supported because it is
# an independent open-source project that happens to use the
# Chat Completions JSON wire shape - that wire shape is industry-
# standard among self-hosted LLM servers (LM Studio, vLLM, llama.cpp
# server, GPT4All, etc.). Future similar self-hosted servers can be
# added as their own provider entries; the slice-2 router shares
# Chat Completions transport across all of them.
ALLOWED_PROVIDERS = frozenset(
    {"anthropic", "ollama", "gemini", "lmstudio"}
)

# Providers that have NO sensible default endpoint - operators must
# supply a URL pointing at the host running the inference server.
# Catches the common config error at save-time rather than first-use.
PROVIDERS_REQUIRING_ENDPOINT = frozenset({"ollama", "lmstudio"})

# Allowlisted sampler params an operator may pin on a model record via
# an optional ``sampling:`` block. Forwarded verbatim into the Chat
# Completions payload by analyzers/llm_router.py::_call_chat_completions.
# Primary use: pin a reasoning model's recommended sampling (notably
# ``presence_penalty``) so its <think> trace self-terminates instead of
# looping past the token budget and returning empty ``content`` - the
# documented failure mode of Qwen3.5-122B-A10B (added 2026-06-07). Keys
# outside this set are rejected at save-time so a typo surfaces
# immediately rather than being silently ignored by the server.
ALLOWED_SAMPLING_KEYS = frozenset({
    "temperature", "top_p", "top_k", "min_p",
    "presence_penalty", "frequency_penalty", "repeat_penalty", "seed",
})


class ModelValidation:
    """Static validators for model-registry YAML records."""

    ID_REGEX = _ID_REGEX
    ALLOWED_PROVIDERS = ALLOWED_PROVIDERS
    PROVIDERS_REQUIRING_ENDPOINT = PROVIDERS_REQUIRING_ENDPOINT
    ALLOWED_SAMPLING_KEYS = ALLOWED_SAMPLING_KEYS
    MAX_DESCRIPTION_LEN = 2000

    @staticmethod
    def validate_id(model_id):
        """Validate the model id. Raises ValueError on failure."""
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("Model id must be a non-empty string.")
        s = model_id.strip()
        if not _ID_REGEX.match(s):
            raise ValueError(
                f"Invalid model id: {s!r}. "
                "Only lowercase letters, digits, underscore, hyphen, "
                "and dot are permitted (no spaces, no uppercase)."
            )
        if len(s) > 128:
            raise ValueError("Model id must be 128 characters or fewer.")
        return s

    @staticmethod
    def validate_provider(provider):
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider is required (string).")
        s = provider.strip().lower()
        if s not in ALLOWED_PROVIDERS:
            raise ValueError(
                f"Unknown provider: {provider!r}. "
                f"Allowed: {sorted(ALLOWED_PROVIDERS)}."
            )
        return s

    @staticmethod
    def validate_model_name(model_name):
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("model_name is required (string).")
        s = model_name.strip()
        if len(s) > 256:
            raise ValueError("model_name must be 256 characters or fewer.")
        return s

    @staticmethod
    def validate_endpoint(endpoint):
        """Endpoint is optional. Empty string allowed (provider default)."""
        if endpoint is None:
            return ""
        if not isinstance(endpoint, str):
            raise ValueError("endpoint must be a string when provided.")
        s = endpoint.strip()
        # If non-empty, must look like a URL (very forgiving - provider-
        # specific validation lives in the router).
        if s and not (s.startswith("http://") or s.startswith("https://")):
            raise ValueError(
                f"endpoint must be a http(s) URL when set, got {s!r}"
            )
        return s

    @staticmethod
    def validate_description(description):
        if description is None:
            return ""
        if not isinstance(description, str):
            raise ValueError("description must be a string when provided.")
        s = description.strip()
        if len(s) > ModelValidation.MAX_DESCRIPTION_LEN:
            raise ValueError(
                f"description must be {ModelValidation.MAX_DESCRIPTION_LEN} "
                "characters or fewer."
            )
        return s

    @staticmethod
    def validate_cost(value, *, name):
        """Cost fields are USD per million tokens. Non-negative; zero is
        valid (Ollama and other free local backends).
        """
        if value is None:
            return 0.0
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a number, got bool.")
        if not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number, got {type(value).__name__}.")
        if value < 0:
            raise ValueError(f"{name} must be non-negative, got {value}.")
        return float(value)

    @staticmethod
    def validate_positive_int(value, *, name, default, ceiling=None):
        if value is None:
            return default
        if isinstance(value, bool):
            raise ValueError(f"{name} must be an int, got bool.")
        if not isinstance(value, int):
            raise ValueError(f"{name} must be an integer, got {type(value).__name__}.")
        if value <= 0:
            raise ValueError(f"{name} must be > 0, got {value}.")
        if ceiling is not None and value > ceiling:
            raise ValueError(f"{name} must be <= {ceiling}, got {value}.")
        return value

    @classmethod
    def validate_sampling(cls, value) -> dict:
        """Validate the optional per-record ``sampling`` block.

        Returns a canonical dict of sampler overrides that the Chat
        Completions transport forwards verbatim into the request payload
        (e.g. ``{"presence_penalty": 1.5, "temperature": 1.0}``).
        ``None`` / empty → ``{}`` - no overrides, which is the behaviour
        for every model that doesn't set the field (server defaults
        apply, exactly as before this field existed).

        Each value must be a real number (``int``/``float``, never
        ``bool``). Keys must be in :data:`ALLOWED_SAMPLING_KEYS`; an
        unknown key is treated as a typo and raises so it surfaces at
        save-time rather than being silently dropped by the server.
        """
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(
                f"sampling must be a dict, got {type(value).__name__}."
            )
        out: dict = {}
        for key, val in value.items():
            if key not in cls.ALLOWED_SAMPLING_KEYS:
                raise ValueError(
                    f"Unknown sampling key {key!r}. Allowed: "
                    f"{sorted(cls.ALLOWED_SAMPLING_KEYS)}."
                )
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(
                    f"sampling[{key!r}] must be a number, got "
                    f"{type(val).__name__}."
                )
            out[key] = val
        return out

    @classmethod
    def validate_record(cls, data: dict) -> dict:
        """Validate + normalise a model record. Returns the canonical dict.

        Required: ``id``, ``provider``, ``model_name``.
        Optional: everything else; sensible defaults applied.

        Cross-field rule: providers in
        :data:`PROVIDERS_REQUIRING_ENDPOINT` (``ollama`` and ``lmstudio``)
        MUST supply a non-empty endpoint URL - there's no SDK default for
        a self-hosted server, so an empty endpoint can never resolve.
        Caught at save-time so the operator sees the error immediately
        rather than at first-use.
        """
        if not isinstance(data, dict):
            raise ValueError("Model record must be a dict.")
        record = {
            "id": cls.validate_id(data.get("id", "")),
            "provider": cls.validate_provider(data.get("provider", "")),
            "model_name": cls.validate_model_name(data.get("model_name", "")),
            "endpoint": cls.validate_endpoint(data.get("endpoint", "")),
            "description": cls.validate_description(data.get("description", "")),
            "cost_per_input_million_usd": cls.validate_cost(
                data.get("cost_per_input_million_usd", 0.0),
                name="cost_per_input_million_usd",
            ),
            "cost_per_output_million_usd": cls.validate_cost(
                data.get("cost_per_output_million_usd", 0.0),
                name="cost_per_output_million_usd",
            ),
            "max_output_tokens": cls.validate_positive_int(
                data.get("max_output_tokens"),
                name="max_output_tokens", default=4096, ceiling=131072,
            ),
            "default_timeout_seconds": cls.validate_positive_int(
                data.get("default_timeout_seconds"),
                name="default_timeout_seconds", default=120, ceiling=3600,
            ),
            "sampling": cls.validate_sampling(data.get("sampling")),
        }
        if record["provider"] in cls.PROVIDERS_REQUIRING_ENDPOINT and not record["endpoint"]:
            raise ValueError(
                f"provider={record['provider']!r} requires a non-empty "
                "endpoint URL - there is no SDK default for a self-hosted "
                "server. Set `endpoint` to the URL of the host running "
                f"the {record['provider']} server "
                "(e.g. http://localhost:1234/v1 for LM Studio, "
                "http://localhost:11434 for Ollama)."
            )
        return record
