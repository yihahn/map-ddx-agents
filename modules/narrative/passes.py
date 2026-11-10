import re
import time
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field

from modules.module3_deterministic.collapse import _structured
from modules.module3_deterministic.llm import get_llm

from .prompts import REASON_RULES, WRITE_RULES

# Input: the Narrative input text and dict from preprocess.py. Output: the reason pass — REASON_RUNS independent
# structured judgements (Reasoning JSON: key finding, top candidates with cited evidence, one-line others,
# contested declines, next steps) with an agreement flag on the top set — and the write pass, which turns one
# Reasoning into Korean bedside prose and is checked by code for format and length. Algorithm: the reason pass
# uses collapse's streamed guided-JSON call so the schema is enforced by the server and a long thinking phase
# (a 20K-token input ran past 1200 s on PT07) only has to keep chunks coming. The runs go in parallel and the
# first valid one is written up, the others are kept for review. Ids the model cites are validated against the
# input before the write pass, which sees names and quoted evidence instead of ids so it cannot leak tags into
# prose.

REASON_RUNS = 2
REASON_MAX_TOKENS = 40960
# Thinking included: on PT07 the writer spent 16384 tokens thinking and returned no text three times, so the
# write pass gets the project's default output cap like the reason pass.
WRITE_MAX_TOKENS = 40960
WRITE_ATTEMPTS = 3
# Writer problems worth a new attempt; "length" and "cjk" are reported only.
RETRY_ON = {"empty", "markup", "id_tags"}


# Field order is decoding order: text before ids, points before the decisive test, so each verdict is
# conditioned on its own reasoning (the collapse convention). Strings carry no length bound: under guided
# decoding a bound cuts the sentence off mid-word (seen on PT07) instead of shortening it; the rules ask for
# one sentence per point.
class Claim(BaseModel):
    text: str
    evidence_ids: list[str] = Field(description="ids from the material: V, D#, E#")


class TopCandidate(BaseModel):
    candidate_id: str = Field(description="C# or, for a contested decline, X#")
    points_for: list[Claim] = Field(min_length=1, max_length=4)
    points_against: list[Claim] = Field(max_length=3)
    decisive_test: str | None


class OtherCandidate(BaseModel):
    candidate_id: str
    considered_because: str
    missing: str
    decisive_test: str | None


class ContestedDecline(BaseModel):
    candidate_id: str = Field(description="X#")
    reason: str = Field(description="why the refuting result does not measure the criterion")


class NextStep(BaseModel):
    action: str
    what_result_means: str
    candidate_ids: list[str]
    before_pending_decision: bool


class Reasoning(BaseModel):
    key_finding: Claim
    top: list[TopCandidate] = Field(min_length=1, max_length=3)
    others: list[OtherCandidate]
    contested_declines: list[ContestedDecline]
    next_steps: list[NextStep] = Field(min_length=3, max_length=5)
    safety_notes: list[Claim] = Field(max_length=3)


def reason(material: str) -> dict:
    """REASON_RUNS parallel structured judgements over the same material; agreement = same top candidate set."""
    call = _structured(Reasoning, REASON_MAX_TOKENS)
    prompt = REASON_RULES + "\n\n# Material\n\n" + material

    def one(run: int) -> dict:
        t0 = time.time()
        try:
            out = {"reasoning": call(prompt).model_dump(), "seconds": round(time.time() - t0)}
            print(f"reason run {run + 1}: {out['seconds']}s, top = "
                  f"{[t['candidate_id'] for t in out['reasoning']['top']]}", flush=True)
        except Exception as exc:
            out = {"error": str(exc)[:300], "seconds": round(time.time() - t0)}
            print(f"reason run {run + 1}: failed after {out['seconds']}s: {out['error']}", flush=True)
        return out

    with ThreadPoolExecutor(REASON_RUNS) as pool:
        runs = list(pool.map(one, range(REASON_RUNS)))
    return summarize_runs(runs)


def summarize_runs(runs: list[dict]) -> dict:
    """The runs with their agreement flag: every run valid and the same top candidate set."""
    tops = [frozenset(t["candidate_id"] for t in r["reasoning"]["top"]) for r in runs if "reasoning" in r]
    return {"runs": runs, "agreement": len(tops) == REASON_RUNS and all(t == tops[0] for t in tops),
            "top_sets": [sorted(t) for t in tops]}


def cited_ids(reasoning: dict) -> list[str]:
    ids = list(reasoning["key_finding"]["evidence_ids"])
    for t in reasoning["top"]:
        ids.append(t["candidate_id"])
        ids += [i for c in t["points_for"] + t["points_against"] for i in c["evidence_ids"]]
    ids += [o["candidate_id"] for o in reasoning["others"]]
    ids += [c["candidate_id"] for c in reasoning["contested_declines"]]
    ids += [i for s in reasoning["next_steps"] for i in s["candidate_ids"]]
    ids += [i for c in reasoning["safety_notes"] for i in c["evidence_ids"]]
    return list(dict.fromkeys(ids))


def _lookup(inp: dict) -> dict[str, str]:
    """id -> human-readable form used in the writer's material and in the evidence appendix."""
    table = {"V": "clinical vignette"}
    for d in inp["documents"]:
        table[d["id"]] = f'{d["date"]} {d["name"]}'
    for c in inp["candidates"] + inp["declined"]:
        table[c["id"]] = c["name"]
        for e in c["confirmed"] + c["refuted"]:
            where = ", ".join(str(e[k]) for k in ("date", "doc") if e.get(k))
            table[e["id"]] = f'"{e["content"]}"' + (f" ({where})" if where else "")
    return table


_ID_LIST = re.compile(r"\s*\[(?:(?:V|D\d+|E\d+|C\d+|X\d+)(?:,\s*)?)+\]")
# Not \b: Korean particles attach directly ("C2를"), and \w covers Hangul, so there is no word boundary there.
# A Latin letter on either side means a different token (a variant name such as KIT D816V).
_BARE_ID = re.compile(r"(?<![A-Za-z0-9])([CXDE]\d+)(?![A-Za-z0-9])")


def _plain(text: str, name: dict, place: dict) -> str:
    """Free text from the reason pass with id tags removed and bare ids replaced: candidates by their names,
    documents and evidence lines by the date and file they come from. The model writes ids into its sentences
    although the schema has separate id fields ("D18의 ..." inside a sentence, measured on PT07 and PT10), and the
    writer must not see them."""
    text = _ID_LIST.sub("", text)
    return _BARE_ID.sub(lambda m: place.get(m.group(1)) or name.get(m.group(1), m.group(1)), text)


def _places(inp: dict) -> dict:
    """D# and E# -> '<date> <file>' of the document or of the record line's source, for use inside a sentence."""
    out = {d["id"]: f'{d["date"]} {d["name"]}' for d in inp["documents"]}
    for c in inp["candidates"] + inp["declined"]:
        for e in c.get("confirmed", []) + c.get("refuted", []):
            out[e["id"]] = f'{e["date"]} {e["doc"]}' if e.get("doc") else "기록"
    return out


def writer_material(reasoning: dict, inp: dict) -> str:
    """The Reasoning with ids resolved to names and quoted record lines — what the write pass reads."""
    name = _lookup(inp)
    place = _places(inp)
    by_id = {c["id"]: c for c in inp["candidates"] + inp["declined"]}

    def plain(text: str | None) -> str:
        return _plain(text or "", name, place)

    def claims(items: list[dict]) -> list[str]:
        return [f'  - {plain(c["text"])}  근거: ' + "; ".join(name.get(i, i) for i in c["evidence_ids"]) for c in items]

    lines = ["핵심 소견: " + plain(reasoning["key_finding"]["text"]),
             "  근거: " + "; ".join(name.get(i, i) for i in reasoning["key_finding"]["evidence_ids"]), ""]
    for n, t in enumerate(reasoning["top"], 1):
        c = by_id.get(t["candidate_id"], {})
        lines.append(f'상위 후보 {n}: {name.get(t["candidate_id"], t["candidate_id"])} (status {c.get("status", "?")})')
        lines += ["  지지:"] + claims(t["points_for"])
        if t["points_against"]:
            lines += ["  반대:"] + claims(t["points_against"])
        lines.append("  결정 검사: " + (plain(t["decisive_test"]) or "없음"))
        lines.append("")
    if reasoning["others"]:
        lines.append("나머지 후보:")
        for o in reasoning["others"]:
            lines.append(f'  - {name.get(o["candidate_id"], o["candidate_id"])}: 고려 이유 — {plain(o["considered_because"])} / '
                         f'부족한 것 — {plain(o["missing"])}' + (f' / 결정 검사 — {plain(o["decisive_test"])}' if o["decisive_test"] else ""))
        lines.append("")
    if reasoning["contested_declines"]:
        lines.append("이의가 있는 기각:")
        lines += [f'  - {name.get(x["candidate_id"], x["candidate_id"])}: {plain(x["reason"])}' for x in reasoning["contested_declines"]]
        lines.append("")
    lines.append("다음 단계 (긴급한 것부터):")
    for s in reasoning["next_steps"]:
        lines.append(f'  - {plain(s["action"])} → {plain(s["what_result_means"])} (대상: '
                     + ", ".join(name.get(i, i) for i in s["candidate_ids"]) + ")"
                     + (" [보류 중인 치료 결정 전에]" if s["before_pending_decision"] else ""))
    if reasoning["safety_notes"]:
        lines += ["", "안전 메모:"] + claims(reasoning["safety_notes"])
    return "\n".join(lines)


def write(reasoning: dict, inp: dict) -> dict:
    """Prose from one Reasoning; retried when the reply is empty or carries format violations."""
    llm = get_llm(max_tokens=WRITE_MAX_TOKENS)
    prompt = WRITE_RULES + "\n\n# Reasoning\n\n" + writer_material(reasoning, inp)
    attempts = []
    for attempt in range(WRITE_ATTEMPTS):
        t0 = time.time()
        message = llm.invoke(prompt)
        text = (message.content or "").strip()
        problems = check_text(text, reasoning, inp)
        attempts.append({"seconds": round(time.time() - t0), "chars": len(text),
                         "finish_reason": message.response_metadata.get("finish_reason"), "problems": problems})
        print(f"write attempt {attempt + 1}: {attempts[-1]['seconds']}s, {len(text)} chars, "
              f"finish = {attempts[-1]['finish_reason']}, problems = {problems}", flush=True)
        if not set(problems) & RETRY_ON:
            break
    return {"text": text, "attempts": attempts}


def check_text(text: str, reasoning: dict, inp: dict) -> list[str]:
    """Format checks on the writer's text: non-empty, no markup, no ids (retried); length and stray CJK reported."""
    problems = []
    if not text:
        return ["empty"]
    if re.search(r"^\s*(#|[-*•]\s|\d+[.)]\s)", text, re.M) or "**" in text or "|" in text:
        problems.append("markup")
    if re.search(r"\[(?:V|D\d+|E\d+|C\d+|X\d+)\]", text) or _BARE_ID.search(text):
        problems.append("id_tags")
    # Six paragraphs of three to four spoken sentences land around 2,000-2,400 characters (PT07 probes).
    if not 1200 <= len(text) <= 2600:
        problems.append("length")
    # A Chinese character inside Korean prose (seen once on PT09: 감염灶) — reported, not retried.
    if re.search(r"[\u4e00-\u9fff]", text):
        problems.append("cjk")
    return problems


def appendix(reasoning: dict, inp: dict) -> str:
    """Evidence the reasoning cited, resolved to record lines — appended under the prose for traceability."""
    name = _lookup(inp)
    rows = [f"- {i}: {name[i]}" for i in cited_ids(reasoning) if i in name and i[0] in "EDV"]
    return "근거 (추론 패스가 인용한 기록)\n" + "\n".join(rows)
