"""The AI/PII configuration schema, in one place.

:class:`AiConfig` and its nested :class:`PiiConfig` are the single source of truth
for the copilot's settings and their defaults — including the privacy guard. The
parser :func:`load_ai_config` maps the ``[ai]`` / ``[ai.pii]`` TOML tables and the
``MOORING_AI_*`` env overrides onto them, so adding a knob is a one-place change
(a field here + its parse line) instead of a field on a god-dataclass plus a
separate loader line elsewhere.

:class:`AppConfig` keeps flat ``ai_*`` / ``ai_pii_*`` read-only properties that
forward here, so existing readers are unchanged; the value of the nested form is
that the whole :class:`PiiConfig` travels to the chat session as ONE object (see
``AIProvider.open_chat``), so a guard field can never be silently dropped on the
way.

Pure stdlib: this module imports nothing from the rest of ``mooring`` (it sits
below ``config``, which imports it), so there is no import cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

# Default NER name-detection model: a SAFETENSORS build loaded as its bf16 variant
# (no pickle), pinned to a commit for reproducibility.
_DEFAULT_NAME_MODEL = "gliner-community/gliner_small-v2.5"
_DEFAULT_NAME_REVISION = "f227d3cd637bd4e6757ae143935316d062393341"


@dataclass(frozen=True)
class PiiConfig:
    """The outbound-PII guard settings. ``enabled`` is the master switch; the
    ``name_*`` fields configure the optional local NER name pass."""

    enabled: bool = False
    block_prompt: bool = True
    scan_source: bool = True
    names: bool = False
    name_model: str = _DEFAULT_NAME_MODEL
    name_revision: str = _DEFAULT_NAME_REVISION
    name_variant: str = "bf16"
    name_labels: tuple[str, ...] = ("person", "name")
    name_threshold: float = 0.7


@dataclass(frozen=True)
class AiConfig:
    enabled: bool = True
    provider: str = "copilot"
    model: str = ""
    reasoning_effort: str = ""
    chat_idle_timeout: int = 900
    context: bool = False
    context_dir: str = "context"
    context_max_kb: int = 256
    live_schema: bool = True
    pii: PiiConfig = field(default_factory=PiiConfig)


def _as_bool(value: object, default: bool) -> bool:
    """Coerce a TOML bool or a string env override to bool; None keeps default."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _str_list(raw: object, default: tuple[str, ...]) -> tuple[str, ...]:
    """Coerce a TOML array (or a single string) to a tuple of strings."""
    if raw is None:
        return default
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, (list, tuple)) and all(isinstance(p, str) for p in raw):
        return tuple(raw)
    raise ValueError("[ai.pii] name_labels must be an array of strings")


def load_ai_config(ai: Mapping, env: Mapping[str, str]) -> AiConfig:
    """Build an :class:`AiConfig` from the merged ``[ai]`` table and env overrides.

    ``ai`` is the ``[ai]`` section (its ``pii`` key is the ``[ai.pii]`` table).
    Env vars (``MOORING_AI_*``) take precedence over the file, matching the rest
    of the config's read path.
    """
    p = ai.get("pii", {})
    if not isinstance(p, Mapping):
        p = {}
    pii = PiiConfig(
        enabled=_as_bool(env.get("MOORING_AI_PII"), _as_bool(p.get("enabled"), False)),
        block_prompt=_as_bool(
            env.get("MOORING_AI_PII_BLOCK_PROMPT"), _as_bool(p.get("block_prompt"), True)
        ),
        scan_source=_as_bool(
            env.get("MOORING_AI_PII_SCAN_SOURCE"), _as_bool(p.get("scan_notebook_source"), True)
        ),
        names=_as_bool(env.get("MOORING_AI_PII_NAMES"), _as_bool(p.get("detect_names"), False)),
        name_model=env.get("MOORING_AI_PII_NAME_MODEL", str(p.get("name_model", _DEFAULT_NAME_MODEL))),
        name_revision=env.get(
            "MOORING_AI_PII_NAME_REVISION", str(p.get("name_model_revision", _DEFAULT_NAME_REVISION))
        ),
        name_variant=env.get("MOORING_AI_PII_NAME_VARIANT", str(p.get("name_model_variant", "bf16"))),
        name_labels=_str_list(p.get("name_labels"), ("person", "name")),
        name_threshold=float(env.get("MOORING_AI_PII_NAME_THRESHOLD", p.get("name_threshold", 0.7))),
    )
    return AiConfig(
        enabled=_as_bool(env.get("MOORING_AI_ENABLED"), _as_bool(ai.get("enabled"), True)),
        provider=env.get("MOORING_AI_PROVIDER", str(ai.get("provider", "copilot"))),
        model=env.get("MOORING_AI_MODEL", str(ai.get("model", ""))),
        reasoning_effort=env.get("MOORING_AI_REASONING_EFFORT", str(ai.get("reasoning_effort", ""))),
        chat_idle_timeout=int(
            env.get("MOORING_AI_CHAT_IDLE_SEC", ai.get("chat_idle_timeout_sec", 900))
        ),
        context=_as_bool(env.get("MOORING_AI_CONTEXT"), _as_bool(ai.get("context"), False)),
        context_dir=env.get("MOORING_AI_CONTEXT_DIR", str(ai.get("context_dir", "context"))),
        context_max_kb=int(env.get("MOORING_AI_CONTEXT_MAX_KB", ai.get("context_max_kb", 256))),
        live_schema=_as_bool(env.get("MOORING_AI_LIVE_SCHEMA"), _as_bool(ai.get("live_schema"), True)),
        pii=pii,
    )
