"""Client-facing model aliases and upstream routing for grokbot2api."""

from __future__ import annotations

import json
import re
import threading
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_ADMIN_CONFIG = Path(__file__).with_name("admin_config.json")


@dataclass
class ModelSpec:
    """One client-facing alias mapped to an upstream Cursor model + params."""

    alias: str
    upstream_id: str
    params: list[tuple[str, str]] = field(default_factory=list)
    display_name: str = ""
    context_window: int = 256000
    enabled: bool = True
    supports_vision: bool = True
    capabilities: list[str] = field(default_factory=lambda: ["chat"])
    notes: str = ""
    is_custom: bool = False
    # Packed GetUsableModels ids (e.g. grok-4.7-high) are variant string representations;
    # effort/fast live in the modelId, not in RequestedModel.parameters.
    is_variant_string: bool = True

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.alias,
            "object": "model",
            "owned_by": "local-sand-adapter",
            "upstream_id": self.upstream_id,
            "params": [{"id": key, "value": value} for key, value in self.params],
            "context_window": self.context_window,
            "supports_vision": self.supports_vision,
            "capabilities": list(self.capabilities),
            "display_name": self.display_name or self.alias,
            "enabled": self.enabled,
            "notes": self.notes,
            "is_custom": self.is_custom,
            "is_variant_string": self.is_variant_string,
        }


def _packed(
    alias: str,
    upstream_id: str,
    *,
    display_name: str = "",
    context_window: int = 256000,
    notes: str = "",
    supports_vision: bool = True,
) -> ModelSpec:
    """Map a client alias to a packed upstream modelId with no effort/fast params."""
    return ModelSpec(
        alias=alias,
        upstream_id=upstream_id,
        params=[],
        display_name=display_name or alias,
        context_window=context_window,
        capabilities=["chat"],
        notes=notes,
        supports_vision=supports_vision,
        is_variant_string=True,
    )


# 2026-09-28: AiService/GetUsableModels no longer lists bare grok-4.7 / grok-4.6.
# Effort/fast are packed into modelId (grok-4.7-high, cursor-grok-4.6-high-fast, ...).
# Sending legacy bare ids + parameters yields ERROR_NOT_HIGH_ENOUGH_PERMISSIONS.
BUILTIN_MODELS: list[ModelSpec] = [
    _packed(
        "cursor-grok-4-7",
        "grok-4.7-high",
        display_name="Cursor Grok 4.7",
        notes="Upstream packed id grok-4.7-high (effort in modelId)",
        supports_vision=False,
    ),
    _packed(
        "cursor-grok-4-7-fast",
        "grok-4.7-high-fast",
        display_name="Cursor Grok 4.7 Fast",
        notes="Upstream packed id grok-4.7-high-fast",
        supports_vision=False,
    ),
    _packed(
        "cursor-grok-4-6",
        "cursor-grok-4.6-high",
        display_name="Cursor Grok 4.6",
        notes="Upstream packed id cursor-grok-4.6-high",
    ),
    _packed(
        "cursor-grok-4-6-fast",
        "cursor-grok-4.6-high-fast",
        display_name="Cursor Grok 4.6 Fast",
        notes="Upstream packed id cursor-grok-4.6-high-fast (legacy slug grok-4.6)",
    ),
    _packed(
        "cursor-grok-4-5",
        "cursor-grok-4.5-high",
        display_name="Cursor Grok 4.5",
        notes="Upstream packed id cursor-grok-4.5-high",
        supports_vision=False,
    ),
    _packed(
        "cursor-grok-4-5-fast",
        "cursor-grok-4.5-high-fast",
        display_name="Cursor Grok 4.5 Fast",
        notes="Upstream packed id cursor-grok-4.5-high-fast",
        supports_vision=False,
    ),
    _packed(
        "cursor-composer-2-5",
        "composer-2.5",
        display_name="Cursor Composer 2.5",
        notes="Composer 2.5 standard",
        supports_vision=False,
    ),
    _packed(
        "cursor-composer-2-5-fast",
        "composer-2.5-fast",
        display_name="Cursor Composer 2.5 Fast",
        notes="Composer 2.5 fast (product default)",
        supports_vision=False,
    ),

    # 2026-10-07: GetUsableModels lists packed Claude Opus 5.5 variant strings.
    # Agent chat may still auto-pick; Stream needs sbi_ + account permissions.
    _packed(
        "cursor-claude-opus-5-5",
        "claude-opus-5-5-high",
        display_name="Cursor Claude Opus 5.5",
        notes="Upstream packed id claude-opus-5-5-high (GetUsableModels)",
    ),
    _packed(
        "cursor-claude-opus-5-5-fast",
        "claude-opus-5-5-high-fast",
        display_name="Cursor Claude Opus 5.5 Fast",
        notes="Upstream packed id claude-opus-5-5-high-fast",
    ),
    _packed(
        "cursor-claude-opus-5-5-medium",
        "claude-opus-5-5-medium",
        display_name="Cursor Claude Opus 5.5 Medium",
        notes="Upstream packed id claude-opus-5-5-medium",
    ),
    _packed(
        "cursor-claude-opus-5-5-medium-fast",
        "claude-opus-5-5-medium-fast",
        display_name="Cursor Claude Opus 5.5 Medium Fast",
        notes="Upstream packed id claude-opus-5-5-medium-fast",
    ),
    _packed(
        "cursor-claude-opus-5-5-low",
        "claude-opus-5-5-low",
        display_name="Cursor Claude Opus 5.5 Low",
        notes="Upstream packed id claude-opus-5-5-low",
    ),
    _packed(
        "cursor-claude-opus-5-5-low-fast",
        "claude-opus-5-5-low-fast",
        display_name="Cursor Claude Opus 5.5 Low Fast",
        notes="Upstream packed id claude-opus-5-5-low-fast",
    ),
    _packed(
        "cursor-claude-opus-5-5-max",
        "claude-opus-5-5-max",
        display_name="Cursor Claude Opus 5.5 Max",
        notes="Upstream packed id claude-opus-5-5-max",
    ),
    _packed(
        "cursor-claude-opus-5-5-max-fast",
        "claude-opus-5-5-max-fast",
        display_name="Cursor Claude Opus 5.5 Max Fast",
        notes="Upstream packed id claude-opus-5-5-max-fast",
    ),
    _packed(
        "cursor-claude-opus-5-5-xhigh",
        "claude-opus-5-5-xhigh",
        display_name="Cursor Claude Opus 5.5 Extra High",
        notes="Upstream packed id claude-opus-5-5-xhigh",
    ),
    _packed(
        "cursor-claude-opus-5-5-xhigh-fast",
        "claude-opus-5-5-xhigh-fast",
        display_name="Cursor Claude Opus 5.5 Extra High Fast",
        notes="Upstream packed id claude-opus-5-5-xhigh-fast",
    ),
    ModelSpec(
        alias="cursor-generate-image",
        upstream_id="cursor-generate-image",
        params=[],
        display_name="Cursor Generate Image",
        context_window=0,
        supports_vision=False,
        capabilities=["image_generation"],
        notes=(
            "Maps to AiService/RunGenerateImage (session token). "
            "Client model_id is often ignored server-side (Google image model)."
        ),
        is_variant_string=False,
    ),
]

# Bare / legacy ids -> packed upstream (no effort/fast params).
UPSTREAM_PASSTHROUGH: dict[str, ModelSpec] = {
    "grok-4.7": _packed("grok-4.7", "grok-4.7-high", display_name="Grok 4.7 (packed high)", supports_vision=False),
    "grok-4.7-high": _packed("grok-4.7-high", "grok-4.7-high", display_name="Grok 4.7 High", supports_vision=False),
    "grok-4.7-high-fast": _packed(
        "grok-4.7-high-fast", "grok-4.7-high-fast", display_name="Grok 4.7 High Fast", supports_vision=False
    ),
    "grok-4.6": _packed(
        "grok-4.6",
        "cursor-grok-4.6-high-fast",
        display_name="Grok 4.6 (legacy→cursor-grok-4.6-high-fast)",
    ),
    "grok-4.5": _packed(
        "grok-4.5",
        "cursor-grok-4.5-high",
        display_name="Grok 4.5 (legacy→cursor-grok-4.5-high)",
        supports_vision=False,
    ),
    "composer-2.5": _packed("composer-2.5", "composer-2.5", display_name="Composer 2.5", supports_vision=False),
    "composer-2.5-fast": _packed(
        "composer-2.5-fast", "composer-2.5-fast", display_name="Composer 2.5 Fast", supports_vision=False
    ),
    "cursor-grok-4.6-high": _packed(
        "cursor-grok-4.6-high", "cursor-grok-4.6-high", display_name="Cursor Grok 4.6 High"
    ),
    "cursor-grok-4.6-high-fast": _packed(
        "cursor-grok-4.6-high-fast",
        "cursor-grok-4.6-high-fast",
        display_name="Cursor Grok 4.6 High Fast",
    ),
    # Claude Opus 5.5 packed ids + short aliases (GetUsableModels 2026-10-07).
    "claude-opus-5-5": _packed(
        "claude-opus-5-5",
        "claude-opus-5-5-high",
        display_name="Claude Opus 5.5 (->high)",
    ),
    "opus-5.5": _packed(
        "opus-5.5",
        "claude-opus-5-5-high",
        display_name="Opus 5.5 (->claude-opus-5-5-high)",
    ),
    "opus-5-5": _packed(
        "opus-5-5",
        "claude-opus-5-5-high",
        display_name="Opus 5-5 (->claude-opus-5-5-high)",
    ),
    "claude-opus-5-5-high": _packed(
        "claude-opus-5-5-high", "claude-opus-5-5-high", display_name="Claude Opus 5.5 High"
    ),
    "claude-opus-5-5-high-fast": _packed(
        "claude-opus-5-5-high-fast",
        "claude-opus-5-5-high-fast",
        display_name="Claude Opus 5.5 High Fast",
    ),
    "claude-opus-5-5-medium": _packed(
        "claude-opus-5-5-medium", "claude-opus-5-5-medium", display_name="Claude Opus 5.5 Medium"
    ),
    "claude-opus-5-5-medium-fast": _packed(
        "claude-opus-5-5-medium-fast",
        "claude-opus-5-5-medium-fast",
        display_name="Claude Opus 5.5 Medium Fast",
    ),
    "claude-opus-5-5-low": _packed(
        "claude-opus-5-5-low", "claude-opus-5-5-low", display_name="Claude Opus 5.5 Low"
    ),
    "claude-opus-5-5-low-fast": _packed(
        "claude-opus-5-5-low-fast",
        "claude-opus-5-5-low-fast",
        display_name="Claude Opus 5.5 Low Fast",
    ),
    "claude-opus-5-5-max": _packed(
        "claude-opus-5-5-max", "claude-opus-5-5-max", display_name="Claude Opus 5.5 Max"
    ),
    "claude-opus-5-5-max-fast": _packed(
        "claude-opus-5-5-max-fast",
        "claude-opus-5-5-max-fast",
        display_name="Claude Opus 5.5 Max Fast",
    ),
    "claude-opus-5-5-xhigh": _packed(
        "claude-opus-5-5-xhigh", "claude-opus-5-5-xhigh", display_name="Claude Opus 5.5 Extra High"
    ),
    "claude-opus-5-5-xhigh-fast": _packed(
        "claude-opus-5-5-xhigh-fast",
        "claude-opus-5-5-xhigh-fast",
        display_name="Claude Opus 5.5 Extra High Fast",
    ),

}


DEFAULT_ALIAS = "cursor-grok-4-7"


class ModelCatalogue:
    """In-memory catalogue with optional JSON persistence for admin toggles."""

    def __init__(
        self,
        *,
        default_alias: str = DEFAULT_ALIAS,
        config_path: Path | None = None,
        fallback_upstream: str = "grok-4.7-high",
    ) -> None:
        self.lock = threading.Lock()
        self.config_path = Path(config_path) if config_path else DEFAULT_ADMIN_CONFIG
        self.fallback_upstream = fallback_upstream
        self.models: dict[str, ModelSpec] = {m.alias: deepcopy(m) for m in BUILTIN_MODELS}
        for alias, spec in UPSTREAM_PASSTHROUGH.items():
            self.models.setdefault(alias, deepcopy(spec))
        self.default_alias = default_alias if default_alias in self.models else DEFAULT_ALIAS
        self.client_keys: list[dict[str, Any]] = []
        self._load_persisted()

    def _load_persisted(self) -> None:
        path = self.config_path
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        custom_models = data.get("custom_models")
        if isinstance(custom_models, list):
            for item in custom_models:
                try:
                    spec = self._custom_spec_from_payload(item)
                except (TypeError, ValueError):
                    continue
                if spec.alias not in self.models:
                    self.models[spec.alias] = spec
        default = data.get("default_alias")
        if isinstance(default, str) and default in self.models:
            self.default_alias = default
        disabled = data.get("disabled")
        if isinstance(disabled, list):
            for alias in disabled:
                if isinstance(alias, str) and alias in self.models:
                    self.models[alias].enabled = False
        enabled = data.get("enabled")
        if isinstance(enabled, list):
            for alias in enabled:
                if isinstance(alias, str) and alias in self.models:
                    self.models[alias].enabled = True
        keys = data.get("client_keys")
        if isinstance(keys, list):
            cleaned: list[dict[str, Any]] = []
            for item in keys:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or "").strip()
                if not key:
                    continue
                cleaned.append(
                    {
                        "id": str(item.get("id") or f"key_{len(cleaned)}"),
                        "key": key,
                        "name": str(item.get("name") or ""),
                        "created_at": int(item.get("created_at") or 0),
                    }
                )
            self.client_keys = cleaned

    def save(self) -> None:
        path = self.config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "default_alias": self.default_alias,
            "disabled": [alias for alias, spec in self.models.items() if not spec.enabled],
            "enabled": [alias for alias, spec in self.models.items() if spec.enabled],
            "client_keys": list(self.client_keys),
            "custom_models": [
                asdict(spec) for spec in self.models.values() if spec.is_custom
            ],
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def list_public(self, *, only_enabled: bool = True) -> list[dict[str, Any]]:
        with self.lock:
            items = []
            for alias in sorted(self.models):
                spec = self.models[alias]
                if only_enabled and not spec.enabled:
                    continue
                # Prefer curated aliases in OpenAI /v1/models; still include
                # passthrough upstream ids so raw clients keep working.
                items.append(spec.to_public())
            return items

    def resolve(self, client_model: str | None) -> ModelSpec:
        """Map a client model id to an upstream ModelSpec.

        Unknown or empty client ids fall back to the configured default alias,
        then to a synthetic entry using ``fallback_upstream`` (startup --model).
        """
        with self.lock:
            requested = (client_model or "").strip()
            if requested and requested in self.models:
                spec = deepcopy(self.models[requested])
                if not spec.enabled:
                    raise ValueError(f"model {requested!r} is disabled in the admin catalogue")
                return spec
            if self.default_alias in self.models and self.models[self.default_alias].enabled:
                return deepcopy(self.models[self.default_alias])
            # Last resort: route literally to the startup upstream id.
            return ModelSpec(
                alias=requested or self.fallback_upstream,
                upstream_id=self.fallback_upstream,
                params=[],
                display_name=self.fallback_upstream,
            )

    def set_enabled(self, alias: str, enabled: bool) -> ModelSpec:
        with self.lock:
            if alias not in self.models:
                raise KeyError(alias)
            self.models[alias].enabled = bool(enabled)
            spec = deepcopy(self.models[alias])
            self.save()
            return spec

    def set_default(self, alias: str) -> str:
        with self.lock:
            if alias not in self.models:
                raise KeyError(alias)
            if not self.models[alias].enabled:
                raise ValueError(f"cannot set disabled model {alias!r} as default")
            self.default_alias = alias
            self.save()
            return self.default_alias

    @staticmethod
    def _custom_spec_from_payload(payload: Any) -> ModelSpec:
        if not isinstance(payload, dict):
            raise TypeError("custom model must be an object")
        alias = str(payload.get("alias") or payload.get("id") or "").strip()
        upstream_id = str(payload.get("upstream_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", alias):
            raise ValueError("alias must be 1-100 letters, numbers, '.', '_' or '-'")
        if not upstream_id or len(upstream_id) > 200:
            raise ValueError("upstream_id must be 1-200 characters")
        raw_params = payload.get("params") or []
        if isinstance(raw_params, dict):
            raw_params = list(raw_params.items())
        if not isinstance(raw_params, list) or len(raw_params) > 32:
            raise ValueError("params must be an array/object with at most 32 entries")
        params: list[tuple[str, str]] = []
        for item in raw_params:
            if isinstance(item, dict):
                key, value = item.get("id"), item.get("value")
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                key, value = item
            else:
                raise ValueError("each param must contain id and value")
            key, value = str(key or "").strip(), str(value or "").strip()
            if not key or len(key) > 100 or len(value) > 500:
                raise ValueError("invalid model parameter")
            params.append((key, value))
        raw_capabilities = payload.get("capabilities") or ["chat"]
        if isinstance(raw_capabilities, str):
            raw_capabilities = [part.strip() for part in raw_capabilities.split(",")]
        if not isinstance(raw_capabilities, list):
            raise ValueError("capabilities must be an array")
        allowed = {"chat", "vision", "image_generation"}
        capabilities = list(
            dict.fromkeys(str(item).strip() for item in raw_capabilities if str(item).strip())
        )
        if not capabilities or any(item not in allowed for item in capabilities):
            raise ValueError("capabilities may contain chat, vision, image_generation")
        supports_vision = bool(payload.get("supports_vision", "vision" in capabilities))
        if supports_vision and "vision" not in capabilities:
            capabilities.append("vision")
        return ModelSpec(
            alias=alias,
            upstream_id=upstream_id,
            params=params,
            display_name=str(payload.get("display_name") or alias).strip()[:200],
            context_window=max(0, int(payload.get("context_window") or 0)),
            enabled=bool(payload.get("enabled", True)),
            supports_vision=supports_vision,
            capabilities=capabilities,
            notes=str(payload.get("notes") or "Custom admin alias").strip()[:500],
            is_custom=True,
        )

    def add_custom_model(self, payload: dict[str, Any]) -> ModelSpec:
        spec = self._custom_spec_from_payload(payload)
        with self.lock:
            if spec.alias in self.models:
                raise ValueError(f"model alias {spec.alias!r} already exists")
            self.models[spec.alias] = spec
            self.save()
            return deepcopy(spec)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "default_alias": self.default_alias,
                "fallback_upstream": self.fallback_upstream,
                "models": [spec.to_public() for spec in self.models.values()],
                "client_key_count": len(self.client_keys),
                "client_keys": [
                    {
                        "id": item["id"],
                        "name": item.get("name") or "",
                        "created_at": item.get("created_at") or 0,
                        "key_preview": (
                            (item["key"][:4] + "…" + item["key"][-4:])
                            if len(item["key"]) > 8
                            else "****"
                        ),
                    }
                    for item in self.client_keys
                ],
            }

    def list_client_key_values(self) -> list[str]:
        with self.lock:
            return [item["key"] for item in self.client_keys]

    def add_client_key(self, key: str, name: str = "") -> dict[str, Any]:
        import secrets
        import time
        import uuid

        key = (key or "").strip()
        if not key:
            # Auto-generate when the admin UI leaves the field empty.
            key = "gb_" + secrets.token_urlsafe(24)
        if len(key) < 8:
            raise ValueError("密钥长度至少 8 个字符（可留空以自动生成）")
        with self.lock:
            for existing in self.client_keys:
                if existing["key"] == key:
                    raise ValueError("key already exists")
            entry = {
                "id": f"key_{int(time.time())}_{uuid.uuid4().hex[:8]}",
                "key": key,
                "name": (name or "").strip(),
                "created_at": int(time.time()),
            }
            self.client_keys.append(entry)
            self.save()
            return {
                "id": entry["id"],
                "name": entry["name"],
                "created_at": entry["created_at"],
                "key": entry["key"],
            }

    def revoke_client_key(self, key_id: str) -> bool:
        with self.lock:
            before = len(self.client_keys)
            self.client_keys = [item for item in self.client_keys if item["id"] != key_id]
            if len(self.client_keys) == before:
                raise KeyError(key_id)
            self.save()
            return True
