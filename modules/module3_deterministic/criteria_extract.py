import json
import re
import time
from pathlib import Path
from typing import NamedTuple

from ..backends import ACTIVE, BACKENDS
from .criteria_literature import CATEGORIES
from .llm import get_llm

# Input: a diagnosis name, the name of the source document, and the source's text. Output:
# extract_best(name, source_doc, text) -> Extraction(items, rules, newer_versions, meta): the
# criteria items (id, text, category, checkable_by, refute_requires — the tests that must all be
# completed before a record can show the item absent — and scope: the whole condition or the one
# subtype the item applies to, its category being its role there), the combination and points rules over them
# (combo groups with a minimum count each; score points, cut-offs and gate items), each labelled
# with its criteria set ("Del Brutto 2017"), and the newer versions of those sets the source
# mentions; meta["off_topic"] holds the model's reason when it found the source not about the
# diagnosis itself, and then there are no items. Algorithm: fill the v3 prompt (prompts/criteria_extract_v3.txt: the v2 workbook's
# '추출 프롬프트' sheet plus the 2026-10-27 rules), send it with ddxcriteria_extract v1's sampling
# (temperature 0.6, top_p 0.95, 65,536 answer tokens) twice, and keep the finer answer — more items,
# then more rules — as that script's notes recommend; an answer that does not parse is asked once more.

PROMPT_PATH = Path(__file__).parent / "prompts" / "criteria_extract_v3.txt"
PROMPT_VERSION = ("v3: 진단기준_추출_프롬프트_v2_규칙보존.xlsx (2026-10-23) + 2026-10-27 "
                  "(e.g. 보존, 개발 중 검사 제외, refute_requires, 규칙 set, newer_versions, 아형 scope) + 2026-10-28 (아형 = 질환 자체의 형태)"
                  " + 2026-10-29 (about: 이 진단 자체의 기준이 아닌 문서는 항목 없음)"
                  " + 2026-10-30 (about: 다른 질환·약물·노출로 한정된 환자군, 동물 대상도 제외)"
                  " + 2026-10-30 (점수 규칙: 구간마다 항목, default, 개수 항목은 count_of)")
TEMPERATURE, TOP_P, MAX_TOKENS = 0.6, 0.95, 65536
RUNS = 2
ATTEMPTS = 2  # calls per run when an answer does not parse
# 40,960 answer tokens cut off the answer on sources with many rules (the v2 workbook's note), so
# the full 65,536 are kept and the source text is what gives way: the text one call may take is the
# model's context less the prompt and that answer room, at ~3 characters per token.
_PROMPT_TOKENS, _CHARS_PER_TOKEN = 5500, 3
MAX_TEXT_CHARS = (BACKENDS[ACTIVE].context - _PROMPT_TOKENS - MAX_TOKENS) * _CHARS_PER_TOKEN
_PROMPT = PROMPT_PATH.read_text(encoding="utf-8")


class Item(NamedTuple):
    id: str
    text: str
    category: str
    checkable_by: str
    refute_requires: list[str]
    scope: str = "whole"  # "whole", or the subtype the item applies to (its category is within it)


class Extraction(NamedTuple):
    items: list[Item]
    rules: list[dict]
    newer_versions: list[dict]
    meta: dict


def prompt(diagnosis_name: str, source_doc: str, text: str) -> str:
    return (_PROMPT.replace("{진단명}", diagnosis_name).replace("{출처 문서}", source_doc)
            .replace("{원문 텍스트}", text))


def _parse(answer: str) -> dict:
    data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", answer.strip(), flags=re.M).strip())
    if not isinstance(data.get("items"), list) or not isinstance(data.get("rules"), list):
        raise ValueError("answer has no items/rules lists")
    return data


def extract(diagnosis_name: str, source_doc: str, text: str) -> Extraction:
    """One run: items and rules from one source; no items (and the error in meta) if none parses."""
    llm = get_llm(temperature=TEMPERATURE).bind(top_p=TOP_P, max_tokens=MAX_TOKENS)
    filled = prompt(diagnosis_name, source_doc, text)
    meta = {"prompt_version": PROMPT_VERSION, "prompt_chars": len(filled), "text_chars": len(text),
            "temperature": TEMPERATURE, "top_p": TOP_P, "max_tokens": MAX_TOKENS, "errors": []}
    for attempt in range(1, ATTEMPTS + 1):
        started = time.monotonic()
        try:
            data = _parse(llm.invoke(filled).content)
        except Exception as error:  # a failed call or an answer that does not parse: ask again
            meta["errors"].append(f"attempt {attempt}: {type(error).__name__}: {str(error)[:300]}")
            continue
        meta.update(seconds=round(time.monotonic() - started, 1), attempts=attempt)
        if data.get("about") is False:
            meta["off_topic"] = str(data.get("about_reason") or "").strip() or "not about this diagnosis"
            return Extraction([], [], [], meta)
        items, dropped = [], []
        for raw in data["items"]:
            if not isinstance(raw, dict):
                continue
            requires = raw.get("refute_requires") or []
            item = Item(str(raw.get("id", "")), str(raw.get("item", "")).strip(),
                        str(raw.get("category", "")).strip(), str(raw.get("checkable_by", "")).strip(),
                        [str(t).strip() for t in requires if str(t).strip()] if isinstance(requires, list) else [],
                        str(raw.get("scope") or "whole").strip() or "whole")
            if item.text and item.category in CATEGORIES:
                items.append(item)
            else:
                dropped.append(raw)
        if dropped:
            meta["dropped_items"] = dropped
        newer = [v for v in data.get("newer_versions") or [] if isinstance(v, dict) and v.get("set")]
        return Extraction(items, [r for r in data["rules"] if isinstance(r, dict)], newer, meta)
    return Extraction([], [], [], meta)


def extract_best(diagnosis_name: str, source_doc: str, text: str) -> Extraction:
    """RUNS runs of extract, one after another (two at once would double a branch's share of the
    host's concurrency); the one with the most items wins, then the one with the most rules — except
    that a run finding the source not about the diagnosis wins outright: a source is left unused
    rather than risk criteria for something else (the user's call, 2026-10-29)."""
    runs = [extract(diagnosis_name, source_doc, text) for _ in range(RUNS)]
    best = max(range(RUNS), key=lambda i: ("off_topic" in runs[i].meta, len(runs[i].items), len(runs[i].rules)))
    chosen = runs[best]
    chosen.meta["runs"] = [{"items": len(r.items), "rules": len(r.rules), "seconds": r.meta.get("seconds"),
                            "off_topic": r.meta.get("off_topic"), "errors": r.meta["errors"]} for r in runs]
    chosen.meta["chosen_run"] = best + 1
    return chosen
