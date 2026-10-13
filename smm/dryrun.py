"""Пробный прогон адаптации на реальной модели без публикации.

    uv run python -m smm.dryrun post.json            # JSON сообщения Telegram (text/caption + entities)
    uv run python -m smm.dryrun --text "Текст поста"  # просто текст, без ссылок

Нужен ANTHROPIC_API_KEY в .env. Ничего никуда не публикует.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from smm.adapt import Adapter, PostInput, safe_adapt
from smm.config import load_config
from smm.llm.anthropic_api import AnthropicApiProvider


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("message", nargs="?", help="JSON-файл сообщения Telegram")
    ap.add_argument("--text", help="текст поста вместо файла")
    ap.add_argument("--networks", default=None, help="через запятую, например max,threads")
    args = ap.parse_args()

    if args.text:
        post = PostInput(args.text, [])
    elif args.message:
        msg = json.loads(Path(args.message).read_text(encoding="utf-8"))
        post = PostInput(msg.get("text") or msg.get("caption") or "",
                         msg.get("entities") or msg.get("caption_entities") or [])
    else:
        ap.error("нужен файл сообщения или --text")

    cfg = load_config()
    provider = AnthropicApiProvider(cfg.secret("ANTHROPIC_API_KEY"), effort=cfg.llm.get("effort"),
                                    workspace_id=os.environ.get("ANTHROPIC_WORKSPACE_ID") or None)
    networks = args.networks.split(",") if args.networks else None
    result = safe_adapt(Adapter(cfg, provider), post, networks)

    print(f"Статус: {result.status}" + (f" ({result.reason})" if result.reason else ""))
    if result.analysis:
        print(f"Тип: {result.analysis['post_type']} · Тезис: {result.analysis['thesis']}")
    for ml in result.manifest:
        acts = ", ".join(f"{n}={d['action']}" for n, d in ml.decisions.items())
        print(f"  {ml.ref} [{ml.link_class}] {ml.final_url} → {acts}")
    for net, v in result.networks.items():
        verdict = result.verdicts.get(net, {}).get("verdict", "—")
        print(f"\n===== {net.upper()} · валидатор: {'ok' if v.ok else 'ошибки'} · судья: {verdict}")
        for e in v.errors:
            print(f"  ! {e}")
        for issue in result.verdicts.get(net, {}).get("issues", []):
            print(f"  ~ [{issue['severity']}] {issue['text']}")
        print(json.dumps(v.rendered, ensure_ascii=False, indent=2))
    cost = sum(r.cost_usd_est for r in result.llm_runs)
    print(f"\nLLM-вызовов: {len(result.llm_runs)}, оценка стоимости: ${cost:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
