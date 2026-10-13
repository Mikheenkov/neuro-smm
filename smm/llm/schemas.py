"""JSON-схемы ответов (§5.3). Общее строгое подмножество: все поля в required,
additionalProperties=false, необязательные — через тип null."""

from __future__ import annotations

_S = {"type": "string"}
_NS = {"type": ["string", "null"]}
_STRS = {"type": "array", "items": _S}


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


DROPPED = {"type": "array", "items": _obj({"fact": _S, "reason": _S})}

NETWORK_SCHEMAS = {
    "max": _obj({
        "skip": {"type": "boolean"}, "skip_reason": _NS, "text_ir": _S,
        "buttons": {"type": "array", "items": _obj({"label": _S, "ref": _S})},
        "dropped_facts": DROPPED,
    }),
    "vk": _obj({
        "skip": {"type": "boolean"}, "skip_reason": _NS, "text_ir": _S, "main_link_ref": _NS,
        "hashtags": _STRS, "dropped_facts": DROPPED,
    }),
    "fb": _obj({
        "skip": {"type": "boolean"}, "skip_reason": _NS, "text_ir": _S, "main_link_ref": _NS,
        "hashtags": _STRS, "dropped_facts": DROPPED,
    }),
    "threads": _obj({
        "skip": {"type": "boolean"}, "skip_reason": _NS, "parts": _STRS, "text_attachment_ir": _NS,
        "link_attachment_ref": _NS, "topic_tags": _STRS, "dropped_facts": DROPPED,
    }),
    "story": _obj({
        "skip": {"type": "boolean"}, "skip_reason": _NS, "card_title": _S, "card_subtitle": _NS, "caption": _S,
    }),
}

ANALYSIS = _obj({
    "post_type": {"type": "string", "enum": ["news", "tool_review", "howto", "opinion", "announcement", "digest", "personal"]},
    "thesis": _S,
    "key_facts": _STRS,
    "entities": {"type": "array", "items": _obj({
        "name": _S, "kind": {"type": "string", "enum": ["product", "company", "person", "other"]}})},
    "links": {"type": "array", "items": _obj({
        "ref": _S,
        "role": {"type": "string", "enum": ["source", "tool", "own_promo", "reference", "example", "cta"]},
        "importance": {"type": "integer", "enum": [1, 2, 3]},
        "essential": {"type": "boolean"},
    })},
    "ad_suspect": {"type": "boolean"},
    "ad_evidence": _STRS,
})


def editor_schema(networks: list[str]) -> dict:
    return _obj({"analysis": ANALYSIS, "networks": _obj({n: NETWORK_SCHEMAS[n] for n in networks})})


def regen_schema(network: str) -> dict:
    return _obj({"network": {"type": "string", "enum": [network]}, "result": NETWORK_SCHEMAS[network]})


JUDGE = _obj({
    "verdicts": {"type": "array", "items": _obj({
        "network": _S,
        "verdict": {"type": "string", "enum": ["pass", "fix", "block"]},
        "issues": {"type": "array", "items": _obj({"severity": {"type": "string", "enum": ["minor", "major"]}, "text": _S})},
        "fix_instructions": _NS,
    })},
})
