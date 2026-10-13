"""Адаптация одного поста (§1.4–1.5): фильтры → манифест ссылок → редактор →
валидаторы и инварианты → перегенерация → судья. Публикации здесь нет."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from smm.config import Config
from smm.filters import check_ad, check_vpn
from smm.invariants import build_invariants
from smm.ir import IRResult, entities_to_ir, ir_plain_text
from smm.links import ManifestLink, build_manifest, host_of
from smm.llm.base import LLMError, LLMProvider, LLMResult
from smm.llm.prompts import build_editor_prompt, build_judge_prompt, load_glossary
from smm.llm.schemas import JUDGE, editor_schema, regen_schema
from smm.validators import ValidationResult, validate

log = logging.getLogger(__name__)


@dataclass
class PostInput:
    text: str
    entities: list[dict]
    media: list[dict] = field(default_factory=list)  # [{type: photo|video, size, video_dropped?}]
    quoted_sources: list[dict] = field(default_factory=list)  # [{ref, text}] — чужие и свои старые посты


@dataclass
class AdaptResult:
    status: str  # ready | skipped
    reason: str | None = None
    analysis: dict | None = None
    networks: dict[str, ValidationResult] = field(default_factory=dict)
    verdicts: dict[str, dict] = field(default_factory=dict)
    manifest: list[ManifestLink] = field(default_factory=list)
    llm_runs: list[LLMResult] = field(default_factory=list)
    outputs: dict[str, dict] = field(default_factory=dict)  # ответы редактора по сетям (IR), для доработок

    def publishable(self) -> dict[str, dict]:
        return {
            net: v.rendered for net, v in self.networks.items()
            if v.ok and not v.skipped and self.verdicts.get(net, {}).get("verdict") == "pass"
        }


@dataclass
class Adapter:
    cfg: Config
    provider: LLMProvider
    resolve: Callable[[str], str] = lambda u: u
    reachable: Callable[[str], bool] = lambda u: True
    copy_url: Callable[[int, str], str | None] = lambda msg_id, net: None
    # Фильтры рекламы и VPN до LLM. При ручной публикации по команде автора выключены:
    # раз автор сказал «публикуй», система не гадает, реклама это или нет.
    prefilters: bool = True

    def _model(self, role: str) -> str:
        return self.cfg.llm.get("models", {}).get(role, "claude-opus-5-5")

    def _call(self, result: AdaptResult, system: str, payload: dict, schema: dict, role: str) -> dict:
        res = self.provider.generate(system, payload, schema, role=role, model=self._model(role))
        result.llm_runs.append(res)
        return res.data

    def adapt(self, post: PostInput, networks: list[str] | None = None, *, feedback: dict | None = None) -> AdaptResult:
        """feedback = {"network": ..., "previous": прошлая версия, "notes": что сказал автор} — доработка одной сети."""
        networks = [feedback["network"]] if feedback else (networks or [n for n in self.cfg.enabled_networks()])
        ir: IRResult = entities_to_ir(post.text, post.entities)
        result = AdaptResult(status="ready")

        # 1. Код-фильтры до LLM.
        raw_urls = [l.raw_url for l in ir.links]
        if self.prefilters:
            ad = check_ad(post.text, raw_urls)
            if ad.skip:
                return AdaptResult(status="skipped", reason=f"ad: {ad.evidence}")
        manifest = build_manifest(ir.links, self.cfg, resolve=self.resolve, reachable=self.reachable,
                                  copy_url=self.copy_url, networks=tuple(n for n in networks if n != "story"))
        if self.prefilters:
            vpn = check_vpn(post.text, [m.link_class for m in manifest])
            if vpn.skip:
                return AdaptResult(status="skipped", reason=f"vpn: {vpn.evidence}")
        result.manifest = manifest

        # 2. Инварианты и вход для редактора.
        source_plain = ir_plain_text(ir.ir)
        inv = build_invariants(source_plain)
        # Факты из пересказываемых постов и названия сайтов из ссылок модель может упоминать.
        link_names = " ".join(host_of(m.final_url).split(".")[-2] for m in manifest if "." in host_of(m.final_url))
        quoted_text = "\n".join(q.get("text", "") for q in post.quoted_sources) + "\n" + link_names
        has_media = bool(post.media)
        video_dropped = any(m.get("video_dropped") for m in post.media)
        payload = {
            "post_ir": ir.ir,
            "links": [m.for_editor() for m in manifest],
            "mentions": [{"ref": m.ref, "username": m.username} for m in ir.mentions],
            "media": {"items": [{"type": m.get("type")} for m in post.media], "video_dropped": video_dropped},
            "quoted_sources": post.quoted_sources,
            "invariants": inv.as_list(),
            "networks": networks,
        }
        if feedback:
            payload["author_feedback"] = {"previous_version": feedback["previous"], "what_to_fix": feedback["notes"]}

        # 3. Редактор: анализ + все сети одним вызовом.
        prompts = self.cfg.prompts_dir
        editor_prompt = build_editor_prompt(prompts, networks)
        if feedback:
            editor_prompt = type(editor_prompt)(editor_prompt.text + _FEEDBACK_NOTE, editor_prompt.prompt_hash)
        data = self._call(result, editor_prompt.text, payload, editor_schema(networks), "editor")
        result.analysis = data["analysis"]
        # Сущности из анализа усиливают инварианты (только те, что реально есть в исходнике).
        inv = build_invariants(source_plain, [e["name"] for e in data["analysis"].get("entities", [])])
        glossary = load_glossary(prompts)
        mention_refs = {m.ref for m in ir.mentions}

        def check(net: str, out: dict) -> ValidationResult:
            return validate(net, out, manifest, inv, self.cfg, mention_refs=mention_refs, allowed_extra_text=quoted_text,
                            glossary=glossary, has_media=has_media)

        outputs = dict(data["networks"])
        result.outputs = outputs
        for net in networks:
            result.networks[net] = check(net, outputs[net])

        # 4. Перегенерация сетей, не прошедших валидаторы.
        max_attempts = self.cfg.llm.get("max_regen_attempts", 2)
        for net in networks:
            attempt = 0
            while not result.networks[net].ok and attempt < max_attempts:
                attempt += 1
                regen_payload = {**payload, "networks": [net], "previous": outputs[net],
                                 "errors": result.networks[net].errors}
                log.info("перегенерация %s, попытка %d: %s", net, attempt, result.networks[net].errors)
                regen_prompt = build_editor_prompt(prompts, [net])
                outputs[net] = self._call(result, regen_prompt.text + _REGEN_NOTE, regen_payload, regen_schema(net), "regen")["result"]
                result.networks[net] = check(net, outputs[net])

        # 5. Судья — по всем валидным версиям одним вызовом.
        to_judge = [n for n in networks if result.networks[n].ok and not result.networks[n].skipped]
        if to_judge:
            self._judge(result, post, ir, inv, manifest, to_judge, outputs, payload, check)
        return result

    def _judge(self, result, post, ir, inv, manifest, nets, outputs, payload, check) -> None:
        judge_prompt = build_judge_prompt(self.cfg.prompts_dir, nets)
        judge_payload = {
            "source_ir": ir.ir,
            "invariants": inv.as_list(),
            "links": [m.for_editor() for m in manifest],
            "quoted_sources": post.quoted_sources,
            "versions": {n: {"output": outputs[n], "rendered": result.networks[n].rendered} for n in nets},
        }
        verdicts = {v["network"]: v for v in self._call(result, judge_prompt.text, judge_payload, JUDGE, "judge")["verdicts"]}
        for net in nets:
            v = verdicts.get(net) or {"network": net, "verdict": "block", "issues": [{"severity": "major", "text": "судья не вынес вердикт"}], "fix_instructions": None}
            if v["verdict"] == "fix":
                regen_payload = {**payload, "networks": [net], "previous": outputs[net],
                                 "errors": [i["text"] for i in v["issues"]], "fix_instructions": v.get("fix_instructions")}
                regen_prompt = build_editor_prompt(self.cfg.prompts_dir, [net])
                outputs[net] = self._call(result, regen_prompt.text + _REGEN_NOTE, regen_payload, regen_schema(net), "regen")["result"]
                result.networks[net] = check(net, outputs[net])
                majors = any(i["severity"] == "major" for i in v["issues"])
                if result.networks[net].ok and majors:
                    again = self._call(result, judge_prompt.text,
                                       {**judge_payload, "versions": {net: {"output": outputs[net], "rendered": result.networks[net].rendered}}},
                                       JUDGE, "judge")["verdicts"]
                    v = next((x for x in again if x["network"] == net), v)
                    if v["verdict"] == "fix":
                        # После исправления осталась только «доводка» — публикуем: стоп только по block.
                        v = {**v, "verdict": "pass"}
                elif result.networks[net].ok:
                    v = {**v, "verdict": "pass"}
                else:
                    v = {**v, "verdict": "block"}
            result.verdicts[net] = v


_FEEDBACK_NOTE = """

# Доработка по замечаниям автора

Автор посмотрел прошлую версию (`author_feedback.previous_version`) и написал, что исправить (`author_feedback.what_to_fix`). Его замечания — главный приоритет: выполни их, даже если они расходятся с общими правилами площадки выше (кроме технических лимитов и правил про ссылки). Всё, о чём автор не просил, сохрани из прошлой версии."""


_REGEN_NOTE = """

# Перегенерация

Ты уже писал версию для этой площадки (`previous`), но она не прошла проверку. Исправь ошибки из `errors` (и `fix_instructions`, если есть), сохранив всё остальное. Верни объект `{"network": ..., "result": ...}`."""


def safe_adapt(adapter: Adapter, post: PostInput, networks: list[str] | None = None, *,
               feedback: dict | None = None) -> AdaptResult:
    """Обёртка для демона: ошибки LLM превращаются в held, а не в падение."""
    try:
        return adapter.adapt(post, networks, feedback=feedback)
    except LLMError as exc:
        log.warning("LLM недоступен (%s): %s", exc.error_class, exc)
        return AdaptResult(status="held", reason=f"llm:{exc.error_class}: {exc}")
