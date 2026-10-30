from smm.config import load_config
from smm.filters import check_ad, check_vpn
from smm.invariants import build_invariants, check_invariants, extract_numbers
from smm.ir import content_hash, entities_to_ir, ir_plain_text
from smm.links import build_manifest, classify, decide, normalize_url
from smm.render import META_FOOTNOTE, render_max, render_plain

CFG = load_config()


def u16(text: str, sub: str) -> tuple[int, int]:
    """Смещение и длина подстроки в UTF-16, как в Telegram."""
    i = text.index(sub)
    return len(text[:i].encode("utf-16-le")) // 2, len(sub.encode("utf-16-le")) // 2


# --- IR -------------------------------------------------------------------

def test_ir_text_link_and_emoji_offsets():
    text = "🔥 Новый Claude тут и ещё ссылка https://github.com/x"
    o1, l1 = u16(text, "тут")
    o2, l2 = u16(text, "https://github.com/x")
    o3, l3 = u16(text, "Новый")
    r = entities_to_ir(text, [
        {"type": "bold", "offset": o3, "length": l3},
        {"type": "text_link", "offset": o1, "length": l1, "url": "https://claude.ai"},
        {"type": "url", "offset": o2, "length": l2},
    ])
    assert r.ir == '🔥 <b>Новый</b> Claude <a ref="L1">тут</a> и ещё ссылка <a ref="L2">https://github.com/x</a>'
    assert [(l.ref, l.raw_url) for l in r.links] == [("L1", "https://claude.ai"), ("L2", "https://github.com/x")]
    assert ir_plain_text(r.ir) == text


def test_ir_nested_and_escaping():
    text = "a < b & жирный курсив"
    o, l = u16(text, "жирный курсив")
    oi, li = u16(text, "курсив")
    r = entities_to_ir(text, [{"type": "bold", "offset": o, "length": l}, {"type": "italic", "offset": oi, "length": li}])
    assert r.ir == "a &lt; b &amp; <b>жирный <i>курсив</i></b>"


def test_content_hash_ignores_entity_order():
    e = [{"type": "bold", "offset": 0, "length": 1}, {"type": "italic", "offset": 2, "length": 1}]
    assert content_hash("a b", e) == content_hash("a b ", list(reversed(e)))


# --- Ссылки ---------------------------------------------------------------

def test_normalize():
    assert normalize_url("http://www.VK.com/wall1_2?utm_source=x&a=1") == "https://vk.ru/wall1_2?a=1"
    assert normalize_url("telegram.me/mikheenkovnews/12") == "https://t.me/mikheenkovnews/12"


def test_classify():
    assert classify("https://t.me/mikheenkovnews/12", CFG).link_class == "own_tg_post"
    assert classify("https://t.me/s/mikheenkovnews/12", CFG).own_msg_id == 12
    assert classify("https://t.me/other/5", CFG).link_class == "foreign_tg_post"
    assert classify("https://t.me/+abc", CFG).link_class == "tg_other"
    assert classify("https://m.youtube.com/watch?v=1", CFG).link_class == "blocked_ru"
    assert classify("https://instagram.com/p/1", CFG).link_class == "meta"
    assert classify("https://claude.ai", CFG).link_class == "geo_blocked_by_service"
    assert classify("https://habr.com/ru/1", CFG).link_class == "ru_site"
    assert classify("https://nordvpn.com", CFG).link_class == "circumvention"
    assert classify("https://example.org", CFG).link_class == "unknown"


def test_decide_matrix():
    assert decide("foreign_tg_post", "max") == "name"  # MAX: текст не пересказываем, ссылку убираем
    assert decide("foreign_tg_post", "threads") == "retell"
    assert decide("own_tg_post", "vk", has_copy=True) == "map"
    assert decide("meta", "max") == "name" and decide("meta", "threads") == "keep"
    assert decide("blocked_ru", "vk") == "name" and decide("blocked_ru", "threads") == "keep"
    assert decide("ok_foreign", "fb") == "keep" and decide("blocked_ru", "fb") == "keep"
    assert decide("own_tg_channel", "threads") == "drop"


def test_manifest_hides_urls_from_editor():
    r = entities_to_ir("смотри тут", [{"type": "text_link", "offset": 7, "length": 3, "url": "https://t.me/mikheenkovnews/7"}])
    m = build_manifest(r.links, CFG, copy_url=lambda mid, net: "https://vk.ru/wall1_9" if net == "vk" else None)
    assert m[0].decisions["vk"] == {"action": "map", "target_url": "https://vk.ru/wall1_9", "target_kind": "ваш пост в этой сети"}
    assert m[0].decisions["max"]["action"] == "name"
    assert "http" not in str(m[0].for_editor())


# --- Фильтры ----------------------------------------------------------------

def test_filters():
    assert check_ad("Реклама. ООО Ромашка", []).skip
    assert check_ad("текст", ["https://x.ru/?erid=abc"]).skip
    assert not check_ad("рекламный рынок растёт", []).skip
    assert check_vpn("поставьте VPN", []).skip
    assert not check_vpn("обычный пост", ["ok_foreign"]).skip


# --- Инварианты --------------------------------------------------------------

def test_numbers_normalize():
    assert 5_000_000 in extract_numbers("бюджет 5 млн рублей")
    assert 30 in extract_numbers("рост на 30%")
    assert 5 in extract_numbers("пять сотрудников")
    assert extract_numbers("позиции 250-500 т.р. и даже более") == extract_numbers("позиции 250–500 тысяч рублей")
    assert 300_000 in extract_numbers("зарплата 300 тр") and 300_000 in extract_numbers("300 т. руб.")
    assert 500 in extract_numbers("500 трактористов")
    assert extract_numbers("буквально через год-два") == extract_numbers("буквально через 1–2 года")
    assert 1 not in extract_numbers("сделали два проекта за год")


def test_invariants_missing_new_and_dropped():
    inv = build_invariants("Claude 5.5 стоит $20, а команда выросла до 12 человек к 7.10")
    ok = check_invariants(inv, "Claude 5.5 за $20, команда — 12 человек к 7 октября", [])
    assert ok.errors(0.2) == []
    bad = check_invariants(inv, "Claude 5.5 за $25", [])
    assert "fact_new: «25» нет в исходнике" in bad.errors(1.0)
    assert any("«12»" in e for e in bad.errors(1.0))
    dropped = check_invariants(inv, "Claude 5.5 за $20 к 7 октября", ["12 человек — второстепенно"])
    assert dropped.errors(1.0) == [] and dropped.dropped_ratio > 0


# --- Рендер ------------------------------------------------------------------

def _manifest():
    r = entities_to_ir("про Claude и видео", [
        {"type": "text_link", "offset": 4, "length": 6, "url": "https://claude.ai"},
        {"type": "text_link", "offset": 13, "length": 5, "url": "https://youtube.com/watch?v=1"},
    ])
    return r, build_manifest(r.links, CFG)


def test_render_max_html_and_buttons():
    r, m = _manifest()
    out = render_max(r.ir, m, [{"label": "Открыть Claude", "ref": "L1"}])
    assert out["text"] == 'про <a href="https://claude.ai">Claude</a> и видео'  # YouTube в РФ → без ссылки
    assert out["buttons"] == [{"label": "Открыть Claude", "url": "https://claude.ai"}]


def test_render_plain_and_footnote():
    r, m = _manifest()
    assert render_plain(r.ir, m, "threads") == "про Claude (https://claude.ai) и видео (https://youtube.com/watch?v=1)"
    assert render_plain(r.ir, m, "fb") == "про Claude (https://claude.ai) и видео (https://youtube.com/watch?v=1)"
    assert render_plain(r.ir, m, "vk") == "про Claude (https://claude.ai) и видео"
    vk = render_plain('я в <a ref="L1">Instagram</a>', [], "vk", meta_footnote=True)
    assert vk.endswith(META_FOOTNOTE)


def test_validator_requires_kept_links_in_text():
    from smm.invariants import build_invariants
    from smm.validators import validate
    r = entities_to_ir("компания 40dB", [{"type": "text_link", "offset": 0, "length": 8, "url": "https://40db.es/"}])
    m = build_manifest(r.links, CFG)
    inv = build_invariants("компания 40dB")
    out = {"skip": False, "skip_reason": None, "parts": ["Компания 40dB слушает людей."], "text_attachment_ir": None,
           "link_attachment_ref": "L1", "topic_tags": ["Нейминг"], "dropped_facts": []}
    res = validate("threads", out, m, inv, CFG)
    assert any("пропала ссылка L1" in e for e in res.errors)  # карточка не заменяет ссылку в тексте
    out["parts"] = ['<a ref="L1">Компания 40dB</a> слушает людей.']
    assert validate("threads", out, m, inv, CFG).ok


def test_max_allows_bare_url_anchor_of_kept_link_only():
    from smm.invariants import build_invariants
    from smm.validators import validate
    src = "сайт: https://40db.es/ и insta https://instagram.com/x"
    r = entities_to_ir(src, [{"type": "url", "offset": 6, "length": 16}, {"type": "url", "offset": 29, "length": 25}])
    m = build_manifest(r.links, CFG)
    inv = build_invariants(r.ir)
    ok = {"skip": False, "skip_reason": None, "buttons": [], "dropped_facts": [],
          "text_ir": 'сайт: <a ref="L1">https://40db.es/</a> и insta <a ref="L2">Instagram</a>'}
    res = validate("max", ok, m, inv, CFG)
    assert not any("URL или домен" in e for e in res.errors), res.errors
    assert '<a href="https://40db.es">https://40db.es/</a>' in res.rendered["text"]
    bad = {**ok, "text_ir": 'сайт: <a ref="L1">https://40db.es/</a> и insta <a ref="L2">https://instagram.com/x</a>'}
    assert any("URL или домен" in e for e in validate("max", bad, m, inv, CFG).errors)  # Meta-адрес в MAX нельзя
