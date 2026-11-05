"""Конвейер адаптации на фейковом провайдере: без сети и без ключей."""

from smm.adapt import Adapter, PostInput
from smm.config import load_config
from smm.llm.base import LLMResult

CFG = load_config()
TEXT = "Claude 5.5 подешевел до $4 за миллион токенов. Подробности тут, а обзор у коллег."
ENTITIES = [
    {"type": "text_link", "offset": TEXT.index("тут"), "length": 3, "url": "https://anthropic.com/pricing"},
    {"type": "text_link", "offset": TEXT.index("у коллег"), "length": 8, "url": "https://t.me/othertech/42"},
]


class FakeProvider:
    name = "fake"

    def __init__(self, editor_networks: dict, verdicts: dict[str, str | list[str]], fix_instructions: str | None = None):
        self.editor_networks = editor_networks
        self.verdicts = verdicts  # список — вердикты по очереди на каждый вызов судьи
        self.fix_instructions = fix_instructions
        self.calls: list[tuple[str, dict]] = []

    def generate(self, system, payload, schema, *, role, model):
        self.calls.append((role, payload))
        if role == "editor":
            data = {"analysis": {"post_type": "news", "thesis": "подешевел", "key_facts": [], "entities": [
                {"name": "Claude", "kind": "product"}], "links": [], "ad_suspect": False, "ad_evidence": []},
                "networks": self.editor_networks}
        elif role == "judge":
            data = {"verdicts": [{"network": n, "verdict": v.pop(0) if isinstance(v, list) else v, "issues": [],
                                  "fix_instructions": self.fix_instructions}
                                 for n, v in self.verdicts.items()]}
        else:  # regen: исправленный MAX
            data = {"network": "max", "result": GOOD_MAX}
        return LLMResult(data=data, provider="fake", model=model)


GOOD_MAX = {"skip": False, "skip_reason": None,
            "text_ir": 'Claude 5.5 подешевел до $4 за миллион токенов. Подробности — <a ref="L1">на сайте Anthropic</a>. Коллеги уже сделали обзор: цена стала ниже.',
            "buttons": [{"label": "Цены Claude", "ref": "L1"}], "dropped_facts": []}
BAD_MAX = {**GOOD_MAX, "text_ir": "Claude 5.5 подешевел до $5, подробности в моём телеграм-канале.", "buttons": []}
THREADS = {"skip": False, "skip_reason": None, "parts": ['Claude 5.5 теперь $4 за миллион токенов. <a ref="L1">Цены</a>.'],
           "text_attachment_ir": None, "link_attachment_ref": "L1", "topic_tags": ["ИИ"], "dropped_facts": []}


def test_adapt_regenerates_invalid_network_and_publishes_after_judge():
    provider = FakeProvider({"max": BAD_MAX, "threads": THREADS}, {"max": "pass", "threads": "pass"})
    result = Adapter(CFG, provider).adapt(PostInput(TEXT, ENTITIES), ["max", "threads"])

    roles = [r for r, _ in provider.calls]
    assert roles == ["editor", "regen", "judge"]
    regen_errors = provider.calls[1][1]["errors"]
    assert any("Telegram" in e for e in regen_errors)
    # Выдуманная цифра — не повод перегенерировать: это подозрение, его видит судья.
    assert not any("«5»" in e for e in regen_errors)

    # Модель видит домен (чтобы назвать сайт словами), но не URL.
    assert "https://" not in str(provider.calls[0][1]["links"]) and "/pricing" not in str(provider.calls[0][1])
    assert "othertech" not in str(provider.calls[0][1])

    pub = result.publishable()
    assert pub["max"]["buttons"] == [{"label": "Цены Claude", "url": "https://anthropic.com/pricing"}]
    assert '<a href="https://anthropic.com/pricing">' in pub["max"]["text"]
    assert pub["threads"]["link_attachment_url"] == "https://anthropic.com/pricing"


def test_ad_post_never_reaches_llm():
    provider = FakeProvider({}, {})
    result = Adapter(CFG, provider).adapt(PostInput("Реклама. erid: 2Vtzq", []), ["max"])
    assert result.status == "skipped" and provider.calls == []


def test_judge_remarks_send_version_back_until_pass():
    provider = FakeProvider({"max": GOOD_MAX}, {"max": ["block", "fix", "pass"]}, fix_instructions="убери выдуманное «в месяц»")
    result = Adapter(CFG, provider).adapt(PostInput(TEXT, ENTITIES), ["max"])
    assert [r for r, _ in provider.calls] == ["editor", "judge", "regen", "judge", "regen", "judge"]
    assert provider.calls[2][1]["fix_instructions"] == "убери выдуманное «в месяц»"
    assert "max" in result.publishable() and not result.verdicts["max"].get("unresolved")


def test_after_all_rounds_version_is_published_with_remarks():
    provider = FakeProvider({"max": GOOD_MAX}, {"max": "block"})
    result = Adapter(CFG, provider).adapt(PostInput(TEXT, ENTITIES), ["max"])
    rounds = CFG.llm.get("max_rounds", 4)
    assert [r for r, _ in provider.calls].count("judge") == rounds
    assert "max" in result.publishable() and result.verdicts["max"]["unresolved"]


def test_editor_skip_is_reworked_not_accepted():
    skipped = {**GOOD_MAX, "skip": True, "skip_reason": "пост только для Telegram"}
    provider = FakeProvider({"max": skipped}, {"max": "pass"})
    result = Adapter(CFG, provider).adapt(PostInput(TEXT, ENTITIES), ["max"])
    assert [r for r, _ in provider.calls] == ["editor", "regen", "judge"]
    assert any("пропускать площадку нельзя" in e for e in provider.calls[1][1]["errors"])
    assert "max" in result.publishable()


def test_author_edit_may_remove_facts_even_in_max():
    # MAX не разрешает выбрасывать факты, но если автор сам попросил убрать абзац — это не ошибка.
    cut = {**GOOD_MAX, "text_ir": 'Claude 5.5 подешевел. Подробности — <a ref="L1">на сайте Anthropic</a>.'}
    fb = {"network": "max", "previous": GOOD_MAX, "notes": "убери цену"}
    provider = FakeProvider({"max": cut}, {"max": "pass"})
    result = Adapter(CFG, provider).adapt(PostInput(TEXT, ENTITIES), feedback=fb)
    assert [r for r, _ in provider.calls] == ["editor", "judge"]
    assert result.publishable()["max"]["text"].startswith("Claude 5.5 подешевел.")
    assert provider.calls[1][1]["author_feedback"]["what_to_fix"] == "убери цену"



def test_fact_and_link_suspicions_go_to_judge_not_regen():
    invented = {**GOOD_MAX, "text_ir": "Claude 5.5 подешевел до $5.", "buttons": []}
    provider = FakeProvider({"max": invented}, {"max": ["block", "pass"]})
    result = Adapter(CFG, provider).adapt(PostInput(TEXT, ENTITIES), ["max"])
    assert [r for r, _ in provider.calls] == ["editor", "judge", "regen", "judge"]  # перегенерацию попросил судья
    notes = provider.calls[1][1]["versions"]["max"]["auto_check_notes"]
    assert any("«5»" in n for n in notes) and any("пропала ссылка L1" in n for n in notes)
    assert "max" in result.publishable()
