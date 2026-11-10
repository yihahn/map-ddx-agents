#!/usr/bin/env python3
"""
ddxcriteria_sources.py — 진단명마다 '진단기준이 적힌 참고문헌'(원 논문이든 재수록본이든)의 원문을 모은다.
    LLM·평가 없음. 표준 라이브러리만 사용(PDF→텍스트만 pdftotext 또는 pypdf). 규칙 점수는 받을 순서를 정할 뿐 문헌의 질을 평가하지 않는다.

[1] 후보 검색 (PubMed + Europe PMC)
    MeSH 표제어  진단명을 esearch에 넣고 PubMed 자동 용어 매핑(TranslationSet)을 읽어, 진단명 구절 '전체'가 한 표제어로 대응될 때만 채택
                 (Drug-Induced Liver Injury → chemical and drug induced liver injury ✓ / eosinophilic hepatitis → eosinophils AND hepatitis ✗)
    PubMed       T0  ("<MeSH 표제어>"[mh] OR "X"[tiab]) AND (guideline[pt] OR "practice guideline"[pt] OR guideline*[ti] OR consensus[ti] …)
                 T1  "X"[ti] AND (criteria[ti] OR definition*[ti] OR ((consensus[ti] OR guideline*[ti] OR classification[ti]) AND diagnos*[ti]))
                 T2  "X"[ti] AND diagnosis[ti] NOT "case reports"[pt]
                 T2R "X"[ti] AND (review[pt] OR "systematic review"[pt]) NOT "case reports"[pt]
                 T3  diagnostic criteria for "X"
                 · 검색식마다 상위 --per-query편. PubMed가 진단명 구절을 모두 '색인에 없음'으로 버린 검색 결과는 쓰지 않는다
    Europe PMC   TITLE:"X" AND (criteria/definition/classification/guideline/consensus/recommendation/diagnos*) AND OPEN_ACCESS:y
    · 동의어(--synonyms, 또는 --dx-file의 '|')는 "X"[ti] OR "동의어"[ti]로 넓힌다
[2] 정리
    · 같은 논문은 PMID로 합치고(found_by에 검색식·순위 기록) PubMed 메타데이터(초록·출판유형·MeSH·DOI·PMCID·언어)를 붙인다
    · 제외(exclusion), 순서대로 첫 해당:
        ① 출판유형: Case Reports, Comment, Editorial, Letter, News, Newspaper Article, Published Erratum, Retracted Publication,
           Retraction of Publication, Biography, Interview, Video-Audio Media, Portrait, Historical Article
           (Europe PMC 표기 'case-report'도 같게 정규화. Editorial·Historical Article뿐이고 제목이 지침·정의·기준이면 통과 — 지침이 사설로 색인된 경우)
        ② 제목상 증례: "a case of/report/description/presentation", "case report(s)"
        ③ 수의학: 제목에 veterinary(humans 없음) / MeSH가 Animals만(Humans 없음) / 수의학지 / 동물 모델 제목 / MeSH 없이 동물만 다룬 제목
        ④ 관련성 없음(on_topic 실패): 제목·초록에 진단명(동의어) 구절이 없고, 논문 MeSH에 진단 표제어가 없고, 진단명 단어를 모두 담은 MeSH 표제어도 없음
    · 우선순위 점수(priority — 받을 순서일 뿐 평가 아님; 이유는 priority_reasons에 남는다):
        +4  출판유형 Practice Guideline / Guideline / Consensus Development Conference(, NIH) / Consensus Statement,
            또는 제목에 기관명(society·association·college·academy… 또는 선두 대문자 약어 "AASLD", "ACR/EULAR") + 지침·기준 어구 (출판유형 미색인 보완)
        +3  제목에 criteria / criterion / case definition / definition / classification
        +2  제목에 진단명 (없으면 MeSH 진단 표제어가 주요주제 +2, 부차주제 +1)
        +2  T0 또는 T1에서 찾음 (아니면 T2·T2R +1)
        +1  제목에 guideline / guidance / consensus / recommendation / position paper·statement
        +1  제목에 diagnos*       +1  출판유형 Review / Systematic Review / Meta-Analysis
        −6  지침에 관한 연구 제목 (adherence·implementation·awareness·survey·validation of·cost-effectiveness·impact of …) — 지침 가점을 상쇄
        −2  지침 제목에 치료어(treatment·therapy·dosing·prophylaxis·biologics …)만 있고 진단어 없음, 또는 부제가 치료 전용
    · doc_kind(동점 순서이자 [3]의 종류별 상한 단위): guideline(공식 지침·기관명 제목) > criteria(제목에 기준 어휘) > guideline_kw(제목에 지침 어휘)
      > review > other. treatment(제목·초록 모두 진단어 없고 치료어 있음)는 맨 뒤에 시도. 같은 점수·종류면 검색 순위가 빠른 것부터
[3] 원문 수집 — 점수 순으로 진단당 --k편(기본 4)을 채울 때까지, 최대 --max-tries편 시도
    · tier=primary: --min-priority(기본 3)점 이상. 0편이면 제목에 진단명이 있거나 리뷰인 1~2점 후보를 tier=weak로 (--no-weak-fallback로 끔)
    · 한 doc_kind가 차지하는 상한 max(2, ⌈3k/5⌉)편(k=4 → 3). 시도 순서: 일반 후보 → 상한으로 미룬 후보 → 치료 전용·비영어
    · 출처 순서: NCBI PMC JATS XML → Europe PMC JATS XML → Unpaywall(OA PDF→텍스트, OA HTML). 비OA는 받지 않는다
    · 텍스트 형식: 머리말(서지·출처·라이선스, --- 로 끝) + 본문. JATS는 절 제목을 #/##, 표·상자를 "[TABLE] 캡션"/"[BOX] 캡션" 줄 + "셀 | 셀" 행으로,
      그림을 "[FIGURE] 캡션"으로 적고 부록·floats도 뒤에 붙인다(PARSER_VERSION). PDF·HTML 텍스트에는 표 마크업이 없다(2단 PDF는 줄이 섞인다)
    · 부록: PMCID가 있으면 Europe PMC zip 또는 JATS <supplementary-material> 파일을 받아 텍스트로 바꾼다(PDF·DOCX·XLSX·HTML·TXT; --no-suppl로 끔).
      못 받은 것은 suppl_to_fetch(파일명·캡션·링크)로 돌려준다. 진단 알고리즘·기준 그림은 캡션으로 골라 PMC Cloud CC 문헌만 받는다(--no-figures로 끔)
    · 캐시: 이미 받은 파일은 출력 폴더 → --cache-dir(기본 dxcriteria_cache) 순으로 재사용(--refresh로 무시). PARSER_VERSION을 올리면 PMC 원문만 다시 받는다
[3b] 기준 논문 추적 — 지침·리뷰는 기준을 '인용'만 하고 표는 원 논문에 있는 경우가 많다 (--no-trace로 끔)
    · 받은 JATS 본문에서 기준을 가리키는 문장(diagnostic/classification criteria, case definition, defined as, scoring system …)이 인용한
      참고문헌을 ref-list에서 PMID·DOI로 풀고, 인용 횟수와 초록·출판유형 신호(지침 +2, 기준 어휘·수치·'we propose' +1) 순으로
      상위 --trace-max편(기본 3)을 doc_kind=criteria_paper, tier=traced로 k와 별도로 받는다. 깊이 1
    · 제목·초록에 진단명(동의어)이 없고 제목에 진단명 핵심 단어도 절반 미만이면 추적하지 않는다(다른 질환의 기준·합의문 제외)
    · 비OA 기준 논문은 web_search_hints 맨 앞에 "기준 논문(인용 n회)"로 둔다. documents[i].criteria_refs에 인용 목록(상위 5)을 남긴다
[3c] 기준 완전성 점수 — 받은 원문 본문에 기준이 실제로 적혀 있는지 정규식으로 센다. 참고 순위일 뿐 거르는 용도가 아니다(확보 원문 전부가 추출 입력)
    · table_score = 3·min(기준표 캡션,2) + min(점수 칸 행,16)/2 + min(기준 행,8)  (최대 22; 표 마크업이 있는 xml 원문에서만 0이 아님)
      content_score = min(수치 기준,10)/2 + min(조합 구절,3) + min(점수제 구절,3) + 정의 문장 1  (최대 12; 형식 무관)
    · documents[i].text_format(xml/html/pdf), table_score, content_score, completeness(합), criteria_signals. 진단별 상위 2편이 recommended
[4] 출력
    <out>/manifest.json   진단별 status·MeSH 표제어·검색식·후보 전체(candidates)·확보 문서(documents: path, tier, doc_kind, 점수)·web_search_hints
    <out>/manifest.csv    진단별 한 줄 요약
    <out>/NN_진단명/<PMID>_<출처>.txt (+ .pdf)   원문 · <PMID>_suppl<N>_*.txt 부록 · <PMID>_fig<N>_* 그림 · <PMID>_suppl.json 부록·그림 목록
    status: fulltext_found(원문 1편 이상) / candidates_only(후보는 있으나 OA 원문 없음) / no_candidates
    needs_web_search = true 사유(web_search_reason, 첫 해당): ① 원문 0편 ② 모두 약한 출처 ③ 확보분보다 점수가 높거나 같은 후보의 원문 미확보
        ④ k편 미만이고 지침·기준(doc_kind guideline/criteria) 문헌 없음.  web_search_hints: 못 받은 후보의 제목·DOI·PMID, 나은 후보가 맨 앞

사용법
  python3 ddxcriteria_sources_v0.8.8.py --dx "Sepsis,Acute pancreatitis" --k 4 --per-query 20 --cache-dir dxcriteria_cache --out dxcriteria_out
  python3 ddxcriteria_sources_v0.8.8.py --dx-file names.txt            # 한 줄에 하나: 진단명 | 동의어1 | 동의어2
  python3 ddxcriteria_sources_v0.8.8.py --report PT07_pipeline_report.html   # 리포트의 Module 3 최종·기각 진단명 전체
  옵션 기본값: --k 4 --per-query 10(20 권장: PubMed 상위 N은 호출마다 달라진다) --max-tries 10 --min-priority 3 --trace-max 3
        --out dxcriteria_out --cache-dir dxcriteria_cache · 끄기: --no-fetch --no-suppl --no-figures --no-trace --no-weak-fallback --refresh
  환경변수: NCBI_API_KEY(선택, 있으면 빠름), UNPAYWALL_EMAIL

파이썬에서
  from ddxcriteria_sources import retrieve      # 파일명에 점이 있으므로 ddxcriteria_sources.py로 복사해 import
  r = retrieve("Psittacosis", synonyms=["Chlamydia psittaci infection"], cache_dir="dxcriteria_cache")
  r["documents"][i]["path"]  # 원문 txt;  r["needs_web_search"], r["web_search_hints"]
"""
import argparse, csv, glob, io, json, os, re, shutil, subprocess, sys, tempfile, threading, time, unicodedata, zipfile
import urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from html import unescape

# map-ddx-agents 사본: 원본 v0.8.8(/workspace/ddxcriteria_sources_v0.8.8.py)에서 바꾼 곳 (__version__은 원문 파일 캐시가 버전으로 무효화되므로 그대로)
#   · 연락처 기본값 (Unpaywall 필수, NCBI 권장 파라미터)
#   · 2026-10-29 검색 실패: NCBI 간격을 스레드 공용 잠금으로, 검색 실패는 쉬었다 재시도 (ncbi·epmc) — 178개 진단 중 61개가
#     프록시 403·Europe PMC 404로 검색식 일부를 잃었고, 같은 검색을 다시 보내면 정상이었다
#   · 2026-10-29 우선순위: 진단명이 더 긴 다른 진단명의 일부로만 제목에 있으면 가점 없이 감점, 진단이 아닌 기준(반응·병기·예후)은 감점
EMAIL = os.environ.get("UNPAYWALL_EMAIL", "jongsoo4177@gmail.com")
NCBI_KEY = os.environ.get("NCBI_API_KEY", "")
__version__ = "0.8.8"
TOOL = "dxcriteria_sources"
UA = f"{TOOL}/{__version__} (mailto:{EMAIL})"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/"
MIN_HTML_CHARS = 15000         # HTML을 원문으로 인정할 최소 길이 (v0.8.2: 8000→15000)
MIN_HTML_PARAS = 8             # v0.8.2: 서로 다른 본문 문단(≥400자) 최소 수 — 저장소 소개 페이지(HAL·Pure 등)는 초록+저자+인용 내보내기뿐
_CITE_LINE = re.compile(r"^\s*(abstract|author|title|journal|keywords|N2|AB|TI|AU|KW|T1|JO)\s*[=-]\s", re.I)


def html_is_fulltext(txt):
    """HTML 텍스트가 원문인가: 길이 + 서로 다른 본문 문단 수 (인용 내보내기 줄·저자 목록·중복 초록 제외)."""
    if len(txt or "") < MIN_HTML_CHARS:
        return False
    seen, n = set(), 0
    for line in txt.split("\n"):
        l = line.strip()
        if len(l) < 400 or _CITE_LINE.match(l) or l.count(",") / len(l) > 0.04:   # 쉼표가 많으면 저자 목록
            continue
        key = _norm(l)[:80]
        if key not in seen:
            seen.add(key); n += 1
    return n >= MIN_HTML_PARAS
MIN_PDF_CHARS = 1000


# ═════════════════════════ HTTP ═════════════════════════
def http_get(url, params=None, accept="*/*", timeout=60, retries=3):
    """→ (bytes, content-type, 최종 URL) / 실패 시 (None, 오류문자열, url). 4xx는 재시도하지 않음(429 제외)."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    err = ""
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.headers.get("Content-Type", ""), r.geturl()
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            if 400 <= e.code < 500 and e.code != 429:
                break
        except Exception as e:
            err = str(e)
        time.sleep(2 * (i + 1))
    return None, err, url


_ncbi_last = [0.0]
_ncbi_lock = threading.Lock()   # map-ddx: 진단 분기들이 스레드로 동시에 부르므로 간격을 공용 잠금으로 지킨다
# map-ddx: http_get의 짧은 재시도(2·4초)로도 실패한 검색을 다시 보내기 전 쉬는 시간(초). 실패한 검색식은 결과 0건으로
# 조용히 넘어가 기준 논문을 잃으므로, 한도 초과·프록시 거절이 풀릴 만큼 기다린다.
NET_RETRY_WAITS = (10, 30, 60)


def _retryable(err, also=()):
    """다시 보내 볼 실패인가: 연결·프록시 오류와 429 (그 밖의 4xx는 요청 자체가 틀린 것)."""
    m = re.match(r"HTTP (\d+)", err or "")
    return not m or not 400 <= int(m.group(1)) < 500 or int(m.group(1)) in (429, *also)


def ncbi(endpoint, params):
    """E-utilities 호출 (NCBI 한도: 키 없이 3회/초, 키 있으면 10회/초)."""
    p = {**params, "tool": TOOL, "email": EMAIL}
    if NCBI_KEY:
        p["api_key"] = NCBI_KEY
    for pause in (0, *NET_RETRY_WAITS):
        time.sleep(pause)
        with _ncbi_lock:                      # 보낼 시각을 잠금 안에서 차지한다 (map-ddx: throttle.py와 같은 방식)
            wait = _ncbi_last[0] + (0.11 if NCBI_KEY else 0.35) - time.time()
            _ncbi_last[0] = time.time() + max(wait, 0)
        if wait > 0:
            time.sleep(wait)
        raw, err, _ = http_get(EUTILS + endpoint, p, "application/xml")
        if raw is not None or not _retryable(err):
            break
    if raw is None:
        return None, err
    try:
        return ET.fromstring(raw), ""
    except ET.ParseError as e:
        return None, f"XML 오류: {e}"


def epmc(path, params):
    for pause in (0, *NET_RETRY_WAITS):
        time.sleep(pause)
        raw, err, _ = http_get(EPMC + path, params, "application/json")
        if raw is not None or not _retryable(err, also=(404,)):   # 2026-10-28 런의 404 18건은 다시 보내면 정상이었다
            break
    if raw is None:
        return None, err
    try:
        return json.loads(raw), ""
    except json.JSONDecodeError:
        return None, "JSON 오류"


def _text(el):
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"'s\b", "", s.lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# ═════════════════════════ [1] 후보 검색 ═════════════════════════
def _ti(names):
    return "(" + " OR ".join(f'"{n}"[ti]' for n in names) + ")" if len(names) > 1 else f'"{names[0]}"[ti]'


def _any(names):
    return "(" + " OR ".join(f'"{n}"' for n in names) + ")" if len(names) > 1 else f'"{names[0]}"'


# 지침 필터: 출판유형 + 제목 용어 (출판유형이 아직 안 붙은 최신 지침·중국/일본 학회 합의문까지)
FULL_TAKE = 50          # v0.8: PubMed 결과가 이 수 이하인 검색식은 상위 N 대신 전부 수집 (T1 같은 정밀 검색식의 비결정성 제거)
GUIDE_FILTER = ('(guideline[pt] OR "practice guideline"[pt] OR guideline*[ti] OR guidance[ti] OR consensus[ti] '
                'OR recommendation*[ti] OR "position paper"[ti] OR "position statement"[ti])')   # v0.7.11: guidance·recommendation·position 추가 (AASLD "practice guidance" 누락)


def _t0(N, C):
    """T0: 진단명이 '통째로' 매핑되는 MeSH 개념[mh] 또는 진단명 구절[tiab] AND 지침.
    따옴표 없는 진단명을 그대로 넣으면 PubMed가 단어별로 쪼개 매핑해(eosinophilic hepatitis →
    eosinophils AND hepatitis) 무관한 지침이 걸리므로, 통째 매핑된 MeSH만 쓴다."""
    parts = [f'"{c}"[mh]' for c in C] + [f'"{n}"[tiab]' for n in N]
    return f"({' OR '.join(parts)}) AND {GUIDE_FILTER}"


PUBMED_QUERIES = [   # (tag, build(names, mesh_concepts))
    ("T0", _t0),
    ("T1", lambda N, C: f"{_ti(N)} AND (criteria[ti] OR definition*[ti] OR ((consensus[ti] OR guideline*[ti] "
                        f"OR classification[ti]) AND diagnos*[ti]))"),
    ("T2", lambda N, C: f'{_ti(N)} AND diagnosis[ti] NOT "case reports"[pt]'),
    # T2R: 제목에 진단명이 있는 리뷰 — 지침이 없는 희귀질환(protothecosis, MTX-LPD 등)의 주 출처
    ("T2R", lambda N, C: f'{_ti(N)} AND (review[pt] OR "systematic review"[pt]) NOT "case reports"[pt]'),
    ("T3", lambda N, C: f"diagnostic criteria for {_any(N)}"),
]
STOPWORDS = {"for", "of", "the", "and", "in", "a", "an", "with", "to", "on"}


def _cmp(s):
    """비교용 정규화: 소문자·발음기호 제거·소유격('s) 제거·단어 끝 복수형 s 제거."""
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in _norm(s).split() if w != "s")


_PUNCT = re.compile(r"[,;:.()\[\]{}/?!\"]+")


_TOPIC_STOP = {"disease", "diseases", "syndrome", "syndromes", "disorder", "disorders", "with", "associated", "related"}


def title_on_topic(title, names):
    """제목에 진단명의 핵심 단어(4자 이상, disease/syndrome 등 제외)가 절반 이상 있는가 — 앞 5자 일치로 복수형·어미 차이를 허용한다.
    "adult Still's disease"(Yamaguchi)·"drug, herbal, and dietary supplement-induced liver injury"(AASLD)처럼 진단명 구절이 변형된 제목용."""
    words = {w[:5] for w in re.findall(r"[a-z]{4,}", (title or "").lower())}
    best = 0.0
    for n in names:
        toks = [w[:5] for w in re.findall(r"[a-z]{4,}", n.lower()) if w not in _TOPIC_STOP]
        if toks:
            best = max(best, sum(tk in words for tk in toks) / len(toks))
    return best >= 0.5


def _is_abbr(n):
    """대문자 약어 동의어인가 (글자 5개 이하, 소문자 없음): HLH, DILI, SLE, AKI. 'MTX-LPD'(6자)는 일반 비교."""
    letters = re.sub(r"[^A-Za-z0-9]", "", n or "")
    return 2 <= len(letters) <= 5 and letters.upper() == letters and any(ch.isalpha() for ch in letters)


def has_name(text, names):
    """text에 진단명(동의어) 구절이 단어 경계로 들어 있는가 (복수형·하이픈·소유격·발음기호 차이 허용).
    쉼표·마침표·콜론·괄호 등을 넘어서는 이어 붙이지 않는다
    ("Epstein-Barr virus, hepatitis B" ≠ "Epstein-Barr virus hepatitis")."""
    text = text or ""
    abbr = [n for n in names if _is_abbr(n)]              # v0.8.2: "HLH"가 "r-hLH"(재조합 인간 LH)와 맞지 않도록
    if any(re.search(rf"(?<![A-Za-z0-9]){re.escape(a)}(?![A-Za-z0-9])", text) for a in abbr):
        return True
    names = [n for n in names if not _is_abbr(n)]
    joined = re.sub(r"\s*\([^()]{1,15}\)", " ", text)   # "Epstein-Barr virus (EBV) hepatitis" → 이어 붙인 판
    segs = [f" {_cmp(x)} " for t in (text, joined) for x in _PUNCT.split(t)]   # 원문 판은 "(MTX-LPD)" 약어 보존
    keys = [f" {_cmp(n)} " for n in names if _cmp(n)]
    return any(k in seg for seg in segs for k in keys)


def _top_level(q):
    """검색식 번역문에서 괄호 밖(깊이 0) 부분만 남긴다. 전체를 감싼 괄호는 벗긴다."""
    q = q.strip()
    while q.startswith("("):
        d, end = 0, -1
        for i, ch in enumerate(q):
            d += (ch == "(") - (ch == ")")
            if d == 0:
                end = i
                break
        if end != len(q) - 1:
            break
        q = q[1:-1].strip()
    out, d, quote = [], 0, False
    for ch in q:
        if ch == '"':
            quote = not quote
        if not quote:
            if ch == "(":
                d += 1
                continue
            if ch == ")":
                d -= 1
                continue
        if d == 0:
            out.append(ch)
    return "".join(out)


def mesh_concepts(names):
    """각 진단명이 PubMed 자동 용어 매핑(ATM)에서 '구절 전체'로 대응되는 MeSH 표제어.
    단어별로 쪼개져 매핑되면(최상위에 AND) 개념 없음으로 본다. → ([표제어], {진단명: [표제어]})"""
    out, per = [], {}
    for n in names:
        root, _ = ncbi("esearch.fcgi", {"db": "pubmed", "term": n, "retmax": "0"})
        if root is None:
            continue
        found = []
        for tr in root.findall(".//TranslationSet/Translation"):
            if _cmp(tr.findtext("From") or "") == _cmp(n):
                found += re.findall(r'"([^"]+)"\[MeSH Terms\]', tr.findtext("To") or "")
        if not found:      # TranslationSet이 없을 때: 번역문 최상위에 AND가 없을 때만 인정
            top = _top_level(root.findtext("QueryTranslation") or "")
            if not re.search(r"\b(AND|NOT)\b", top):
                found = re.findall(r'"([^"]+)"\[MeSH Terms\]', top)
        found = list(dict.fromkeys(f.lower() for f in found))
        if found:
            per[n] = found
            out += found
    return list(dict.fromkeys(out)), per


def _all_names_dropped(warn_list, names):
    """PubMed가 모든 진단명 구절을 '색인에 없음'으로 무시했는가 → 남은 조건만으로 검색돼 무관한 결과가 나옴."""
    dropped = {_cmp(re.sub(r"\[[^\]]*\]", "", w).strip().strip('"')) for w in warn_list}
    return all(_cmp(n) in dropped for n in names)


def epmc_query(N):
    t = "(" + " OR ".join(f'TITLE:"{n}"' for n in N) + ")"
    return (f'{t} AND (TITLE:criteria OR TITLE:"case definition" OR TITLE:definition* OR TITLE:classification '
            f'OR TITLE:guideline* OR TITLE:consensus OR TITLE:recommendation* OR TITLE:diagnos*) '
            f'AND OPEN_ACCESS:y NOT PUB_TYPE:"Case Reports"')


def pubmed_search(term, n):
    root, err = ncbi("esearch.fcgi", {"db": "pubmed", "term": term, "retmax": str(n), "sort": "relevance"})
    if root is None:
        return 0, [], [f"ERROR: {err}"]
    warns = [e.text for e in root.iter()
             if e.tag in ("QuotedPhraseNotFound", "PhraseNotFound", "PhraseIgnored")
             and e.text and e.text.strip().lower() not in STOPWORDS]
    return int(root.findtext("Count") or 0), [e.text for e in root.findall("./IdList/Id")], warns


def pubmed_meta(pmids):
    out = {}
    for s in range(0, len(pmids), 100):
        root, _ = ncbi("efetch.fcgi", {"db": "pubmed", "id": ",".join(pmids[s:s + 100]), "retmode": "xml"})
        if root is None:
            continue
        for art in root.findall(".//PubmedArticle"):
            pmid = art.findtext(".//MedlineCitation/PMID")
            ab = " ".join(((a.get("Label") + ": ") if a.get("Label") else "") + _text(a)
                          for a in art.findall(".//Abstract/AbstractText"))
            ids = {i.get("IdType"): (i.text or "").strip()
                   for i in art.findall(".//PubmedData/ArticleIdList/ArticleId")}
            out[pmid] = {
                "title": _text(art.find(".//ArticleTitle")), "abstract": ab,
                "pubtypes": [_text(p) for p in art.findall(".//PublicationTypeList/PublicationType")],
                "mesh": [_text(d) for d in art.findall(".//MeshHeadingList/MeshHeading/DescriptorName")],
                "mesh_major": [_text(mh.find("DescriptorName")) for mh in art.findall(".//MeshHeadingList/MeshHeading")
                               if mh.find("DescriptorName").get("MajorTopicYN") == "Y"
                               or any(q.get("MajorTopicYN") == "Y" for q in mh.findall("QualifierName"))],   # v0.8
                "year": art.findtext(".//PubDate/Year") or (art.findtext(".//PubDate/MedlineDate") or "")[:4],
                "journal": art.findtext(".//Journal/ISOAbbreviation") or "",
                "doi": ids.get("doi", ""), "pmcid": ids.get("pmc", ""),
                "language": ",".join(l.text or "" for l in art.findall(".//Article/Language")),
            }
    return out


def epmc_search(N, n):
    q = epmc_query(N)
    data, err = epmc("search", {"query": q, "resultType": "core", "format": "json", "pageSize": str(n)})
    if data is None:
        return q, 0, [], f"ERROR: {err}"
    res = []
    for h in data.get("resultList", {}).get("result", []):
        res.append({
            "pmid": h.get("pmid", ""), "pmcid": h.get("pmcid", ""), "doi": h.get("doi", ""),
            "epmc_id": f"{h.get('source', '')}:{h.get('id', '')}",
            "title": re.sub(r"<[^>]+>", "", h.get("title", "")).strip(),
            "abstract": re.sub(r"<[^>]+>", " ", h.get("abstractText", "") or "").strip(),
            "pubtypes": (h.get("pubTypeList") or {}).get("pubType", []),
            "mesh": [m.get("descriptorName", "") for m in (h.get("meshHeadingList") or {}).get("meshHeading", [])],
            "mesh_major": [m.get("descriptorName", "") for m in (h.get("meshHeadingList") or {}).get("meshHeading", [])
                           if m.get("majorTopic_YN") == "Y" or any(q.get("majorTopic_YN") == "Y" for q in (m.get("meshQualifierList") or {}).get("meshQualifier", []))],
            "year": str(h.get("pubYear", "")),
            "journal": ((h.get("journalInfo") or {}).get("journal") or {}).get("isoabbreviation", ""),
            "in_epmc": h.get("inEPMC") == "Y", "license": h.get("license", ""),
        })
    return q, int(data.get("hitCount", 0) or 0), res, ""


# ═════════════════════════ [2] 제외 규칙 · 우선순위 ═════════════════════════
EXCLUDE_PT = {"Case Reports", "Comment", "Editorial", "Letter", "News", "Newspaper Article", "Published Erratum",
              "Retracted Publication", "Retraction of Publication", "Biography", "Interview", "Video-Audio Media",
              "Portrait", "Historical Article"}
GUIDE_PT = {"Practice Guideline", "Guideline", "Consensus Development Conference",
            "Consensus Development Conference, NIH", "Consensus Statement"}
REVIEW_PT = {"Review", "Systematic Review", "Meta-Analysis", "review-article", "systematic-review"}
KW_CRIT = re.compile(r"\b(criteria|criterion|case definitions?|definitions?|classification)\b", re.I)
KW_GUIDE = re.compile(r"\b(guidelines?|guidance|consensus|recommendations?|position (paper|statement)|statement)\b", re.I)
# 제목에 발행 기관 신호 (v0.8): 일반 명사 또는 제목 선두의 대문자 약어(진단명 약어 제외). 학회 약어 목록을 두지 않는다.
KW_SOC_WORDS = re.compile(r"\b(society|association|college|academy|federation|organi[sz]ation|consortium|working group|task force"
                          r"|foundation|institute|alliance|committee|network|panel)\b", re.I)
_LEAD_ACRONYM = re.compile(r"^\s*(?:\d{4}\s+)?(?:[A-Za-z]+\s+){0,2}([A-Z][A-Z0-9/&]{2,19})(?=\s)")   # "AASLD practice…", "2022 ACR/EULAR …", "Joint ESPGHAN/NASPGHAN …"


def society_title(t, names=()):
    if KW_SOC_WORDS.search(t or ""):
        return True
    m = _LEAD_ACRONYM.match(t or "")
    if not m:
        return False
    tok = m.group(1)
    return any(ch.isalpha() for ch in tok) and _cmp(tok) not in {_cmp(n) for n in names}


KW_DX = re.compile(r"\bdiagnos\w*", re.I)
# 기관명 가점에 쓰는 '엄격한' 기준 어구 (v0.8): 'WHO criteria for a neglected disease' 같은 일반 용법은 제외
KW_CRIT_STRICT = re.compile(r"\b(diagnostic criteria|classification criteria|case definitions?|definition of|consensus definitions?|diagnostic classification)\b", re.I)
KW_CASE = re.compile(r"\b(an? (rare |unusual |atypical )?case (report|of|description|presentation)"
                     r"|case reports?|case descriptions?)\b", re.I)

ANIMAL_TITLE = re.compile(
    r"\b(avian|birds?|psittacine|psittaciformes|parrots?|poultry|chickens?|geese|swans|ducks?|cats?|feline|"
    r"dogs?|canine|swine|pigs?|porcine|cattle|bovine|horses?|equine|sheep|ovine|goats?|rodents?|mice|mouse|"
    r"rats?|ferrets?|rabbits?|primates?|macaques?|veterinary|zoo|wildlife|livestock)\b", re.I)
HUMAN_WORDS = re.compile(r"\b(humans?|patients?|man|woman|men|women|children|child|adults?|infants?|"
                         r"zoono\w*|clinical|people|persons?)\b", re.I)
ANIMAL_MODEL = re.compile(r"\b(experimental|animal)\s+(\w+\s+)?(porcine|murine|swine|pigs?|piglets?|rats?|mice|mouse|rodents?|canine|dogs?|ovine|sheep|bovine|equine|feline|rabbits?|primates?|models?)\b"
                          r"|\b(porcine|murine|swine|rat|mouse|rodent|canine|ovine|bovine|equine|feline|rabbit|primate|zebrafish)\s+models?\b"
                          r"|\bin\s+(mice|rats|pigs|piglets|dogs|rabbits|sheep|primates)\b", re.I)
ANIMAL_JOURNAL = re.compile(r"\b(vet\w*|avian|zoo|wildl\w*|anim\w*|equine|feline|canine|poult\w*|exot\w*)\b", re.I)


def is_veterinary(c):
    mesh = set(c.get("mesh") or [])
    t0 = c.get("title", "")
    if re.search(r"\bveterinar\w*", t0, re.I) and not re.search(r"\bhumans?\b", t0, re.I):
        return "제목: 수의학"            # MeSH에 Humans가 함께 있어도 (인수공통 리뷰 등) 제외
    if "Humans" in mesh:
        return ""
    if "Animals" in mesh:
        return "MeSH: Animals (Humans 없음)"
    if ANIMAL_JOURNAL.search(c.get("journal", "")):
        return f"수의학지: {c['journal']}"
    t = t0
    m = ANIMAL_MODEL.search(t)
    if m and not re.search(r"\b(humans?|patients?|children|adults?|clinical)\b", t, re.I):
        return f"동물 실험 제목 ('{m.group(0)}')"   # v0.8.4: MeSH 미부여 신규 논문도 (초록의 'clinical' 등과 무관하게)
    if not mesh and ANIMAL_TITLE.search(t) and not HUMAN_WORDS.search(t + " " + (c.get("abstract") or "")[:400]):
        return f"동물만 다룬 제목 ('{ANIMAL_TITLE.search(t).group(0)}')"
    return ""


def _pt(c):
    """출판유형 정규화: PubMed('Case Reports')와 Europe PMC('case-report') 표기를 맞춘다."""
    return {re.sub(r"s$", "", p.lower().replace("-", " ")) for p in (c.get("pubtypes") or [])}


KW_GUIDE_TITLE = re.compile(r"\b(guidelines?|guidance|consensus|recommendations?|position (paper|statement)|statement|definition of|case definition|classification criteria|diagnostic criteria)\b", re.I)


def exclusion(c):
    ex = {re.sub(r"s$", "", p.lower()) for p in EXCLUDE_PT}
    bad = sorted(_pt(c) & ex)
    if bad and not (set(bad) <= {"editorial", "historical article"} and KW_GUIDE_TITLE.search(c.get("title", ""))):
        return "출판유형: " + ", ".join(bad)      # v0.7.11: 제목이 지침·정의·기준이면 editorial/historical 색인은 무시 (CSH DILI 지침, AHA/ASA 뇌졸중 정의)
    if KW_CASE.search(c.get("title", "")):
        return "제목상 증례보고"
    return is_veterinary(c)


# "지침 자체"가 아니라 "지침에 관한 연구"(이행·인식·설문·정책 분석·비용효과)를 알리는 제목 표현
KW_META = re.compile(
    r"(guidelines?\s+(use|adherence|implementation|uptake|awareness|knowledge|alignment)"
    r"|(use|adherence|uptake|implementation|awareness|knowledge|alignment)\s+(of|to|with)\s+.{0,60}guidelines?"
    r"|analysis of .{0,40}guidelines?|\b(performance|validation|validity|reliability|sensitivity|accuracy|applicability|utility) of\b|informing the development|outcome measures?|\bsurvey\b|questionnaire|\bbarriers\b|gaps and opportunities"
    r"|mixed[- ]methods|cost[- ]effectiveness|\bimpact of\b|among health ?care (providers|workers|professionals))",
    re.I)
KW_TREAT = re.compile(r"\b(treat\w*|therap\w*|dosing|dose|induction|management|prophylaxis|biologics?|medications?|regimens?|efficacy|effectiveness|anti-\w+)\b", re.I)
# 본 제목 전체 판정용: 'management'는 진단까지 포함하는 지침 제목에 흔해 제외 (부제 판정에만 사용)
KW_TREAT_TITLE = re.compile(r"\b(treat\w*|therap\w*|dosing|dose|induction|prophylaxis|biologics?|medications?|regimens?|efficacy|effectiveness|anti-\w+)\b", re.I)   # v0.8: 약물 메타분석·합의 제목
KW_DXCRIT = re.compile(r"\b(diagnos\w*|criteria|criterion|definitions?|classification|investigation|"
                       r"work-?up|evaluation|assessment|recognition|identification)\b", re.I)


def _words(s):
    return set(_norm(s).split())


def on_topic(c, names, concepts=()):
    """원문을 받기 전 관련성 관문 — 모든 검색식의 후보에 똑같이 적용 (검색식만 믿고 통과시키지 않음):
    ① 진단명(동의어) 구절이 제목·초록에 있음  ② 논문 MeSH에 진단명의 통째 매핑 MeSH 개념이 있음
    ③ 진단명 단어가 모두 한 MeSH 표제어 안에 있음 (예: cryptococcosis ⊆ Cryptococcosis)"""
    if has_name(c.get("title", "") + " " + (c.get("abstract") or ""), names):
        return True
    mesh = [m.lower() for m in (c.get("mesh") or [])]
    if set(concepts) & set(mesh):
        return True
    mw = [_words(m) for m in mesh]
    return any(_words(n) and any(_words(n) <= m for m in mw) for n in names)


# map-ddx 2026-10-29: 진단이 아니라 치료 반응·병기·예후·중증도를 정하는 기준 ("response evaluation criteria in lymphoma",
# Lugano 병기, CLL scoring system) — '기준/분류' 가점을 받지만 진단 근거가 아니다
KW_NONDX_CRIT = re.compile(r"\b(response|staging|prognos\w*|severity|grading|scoring system|risk (score|stratification)|"
                           r"remission|monitoring)\b", re.I)
# 진단명 바로 뒤에 붙으면 다른 질환이 되는 말 ("lymphoma-associated hemophagocytic syndrome")
_NAME_TAIL = {"associated", "related", "induced", "like"}


def name_only_in_longer(t, names):
    """map-ddx 2026-10-29: 제목의 진단명이 모두 더 긴 다른 진단명의 일부인가 — '/'로 다른 진단과 묶임
    ("acute lymphoblastic leukemia/lymphoma"의 lymphoma) 또는 뒤에 associated/related/induced/like.
    수식어 앞붙음(idiopathic acute eosinophilic pneumonia)은 같은 질환이므로 일부로 보지 않는다."""
    toks = re.findall(r"[a-z0-9]+|/", re.sub(r"'s\b", "", unicodedata.normalize("NFKD", t or "").lower()))
    stem = lambda w: re.sub(r"s$", "", w)
    seen = False
    for n in names:
        if _is_abbr(n):
            continue
        key = [stem(w) for w in re.findall(r"[a-z0-9]+", _norm(n))]
        for i in range(len(toks) - len(key) + 1):
            if [stem(w) for w in toks[i:i + len(key)]] != key:
                continue
            seen = True
            before = toks[i - 1] if i else ""
            after = toks[i + len(key)] if i + len(key) < len(toks) else ""
            if before != "/" and after not in _NAME_TAIL:
                return False
    return seen


def priority(c, names, concepts=()):
    """원문을 받아볼 순서를 정하는 규칙 점수 (평가 아님). 이유 문자열을 함께 남긴다."""
    t, pt, found = c.get("title", ""), _pt(c), c.get("found_by", [])
    guide = {re.sub(r"s$", "", p.lower()) for p in GUIDE_PT}
    review = {re.sub(r"s$", "", p.lower().replace("-", " ")) for p in REVIEW_PT}
    s, why = 0, []

    def add(v, r):
        nonlocal s
        s += v
        why.append(f"{r} {v:+d}")
    if pt & guide:
        add(4, "출판유형:지침/합의")              # 공식 지침·합의문은 출판유형으로 별도 가점
    elif society_title(t, names) and (KW_GUIDE.search(t) or KW_CRIT_STRICT.search(t)) and not KW_META.search(t):
        add(4, "제목:기관명+지침/기준어휘")      # v0.8: 출판유형 미색인 보완 ("AASLD practice guidance…", "ACR/EULAR classification criteria…")
    if KW_CRIT.search(t):
        add(3, "제목:기준/정의/분류")
    if KW_GUIDE.search(t):
        add(1, "제목:지침/합의")                  # 제목 키워드만으로는 약하게 (지침 관련 연구도 걸림)
    if has_name(t, names) and name_only_in_longer(t, names):
        add(-3, "제목:진단명이 다른 진단명의 일부")     # map-ddx 2026-10-29: ALL/lymphoblastic lymphoma 분류가 'Lymphoma' 1순위였음
    elif has_name(t, names):
        add(2, "제목:진단명")
    elif concepts and set(concepts) & {m.lower() for m in (c.get("mesh_major") or [])}:
        add(2, "MeSH:진단 개념(주요주제)")       # v0.8: 색인자가 '이 논문의 주제'로 표시 → 제목 일치와 동급
    elif concepts and set(concepts) & {m.lower() for m in (c.get("mesh") or [])}:
        add(1, "MeSH:진단 개념(부차주제)")       # v0.8: 한 절에서 다루는 수준 → 약하게
    tiers = {f.split(":")[1].split("#")[0] for f in found if f.startswith("pubmed:")}
    if tiers & {"T0", "T1"}:
        add(2, "T0(MeSH/구절+지침)" if "T0" in tiers else "T1")
    elif tiers & {"T2", "T2R"}:
        add(1, "T2" if "T2" in tiers else "T2R")
    if KW_DX.search(t):
        add(1, "제목:diagnos*")
    if pt & review:
        add(1, "리뷰")
    if KW_NONDX_CRIT.search(t) and not KW_CRIT_STRICT.search(t):
        add(-3, f"제목:진단 외 기준('{KW_NONDX_CRIT.search(t).group(0)[:20]}')")   # map-ddx 2026-10-29: '기준/분류' 가점 상쇄
    if KW_META.search(t):     # 지침 '에 관한' 연구는 지침 가점(+4~+7)을 상쇄하고도 남도록
        add(-6, f"지침 관련 연구('{KW_META.search(t).group(0)[:30]}')")
    sub = t.split(":", 1)[1] if ":" in t else ""
    if (pt & guide or KW_GUIDE.search(t)) and KW_TREAT_TITLE.search(t) and not KW_DXCRIT.search(t):
        add(-2, "제목이 치료 전용 지침")    # 지침 가점을 상쇄하려는 것 — 일반 논문(약물 중단 후 관해 등)엔 적용 안 함
    elif sub and KW_TREAT.search(sub) and not KW_DXCRIT.search(sub):
        add(-2, "부제가 치료 전용")
    best = min((int(f.split("#")[1]) for f in found if "#" in f), default=99)
    kind = 0 if (pt & guide or (society_title(t, names) and (KW_GUIDE.search(t) or KW_CRIT_STRICT.search(t)) and not KW_META.search(t))) else 1 if KW_CRIT.search(t) else 2 if KW_GUIDE.search(t) else 3 if pt & review else 4
    if treatment_only(c):
        kind = 5                              # v0.8.4: 치료 전용 → 별도 종류 (지침 상한을 차지하지 않도록, 시도는 맨 뒤)
    return s, why, (kind, best)          # 동점이면 공식 지침 > 기준 > 지침 키워드 > 리뷰 > 기타, 그다음 검색 순위


_REVIEW = {re.sub(r"s$", "", p.lower().replace("-", " ")) for p in REVIEW_PT}
DOC_KIND = ("guideline", "criteria", "guideline_kw", "review", "other", "treatment", "criteria_paper")   # priority()의 kind 값 이름 (v0.8.4: treatment, v0.8.6: criteria_paper = 인용 추적으로 받은 기준 논문)
# 치료 전용 판정 (v0.8.5): 제목에도 초록에도 진단어가 없고, 치료어가 어디든 있음 (v0.8.4는 둘 중 하나만 봐서 'Diagnosis and management…' 제목도 걸렸음)
TX_WORDS = re.compile(r"\b(treat\w*|therap\w*|dos(e|es|ing|age)|prophyla\w*|medications?|regimens?|efficacy|effectiveness|anti-\w+)\b", re.I)
DX_TITLE = re.compile(r"\b(diagnos\w*|criteri\w*|definitions?|classification|evaluation|assessment)\b", re.I)
DX_ABSTRACT = re.compile(r"\b(diagnos\w*|criteri\w*|imaging|serolog\w*|confirm\w*)\b", re.I)


def treatment_only(c):
    """치료 전용 판정 (v0.8.5, Hahn): 제목과 초록 **모두**에 진단어가 없고, 치료어가 제목이나 초록에 있을 때.
    초록이 없으면 제목만으로 판정. 반환값은 치료어가 나온 곳(로그용)."""
    t, a = c.get("title", ""), c.get("abstract") or ""
    if DX_TITLE.search(t) or (a and DX_ABSTRACT.search(a)):
        return ""
    if TX_WORDS.search(t):
        return "제목"
    if a and TX_WORDS.search(a):
        return "초록"
    return ""


def is_english(c):
    lang = (c.get("language") or "").lower()
    return not lang or "eng" in lang


# ═════════════════════════ [3] 원문 수집 ═════════════════════════
PARSER_VERSION = 6      # JATS → 텍스트 변환 규칙 버전 (올리면 캐시의 PMC 원문을 다시 받아 변환)


def _int(v, d=1):
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return d


_CELL_BLOCKS = {"p", "list-item", "break", "def-item", "term", "def", "title", "list", "def-list", "sec"}


def _cell_text(el):
    """표 셀 텍스트 — 셀 안의 문단·목록 항목·줄바꿈 경계에 공백을 넣는다 (v6: 'and' + 'Organ damage'가 붙지 않도록)."""
    parts = []
    def walk(e, top=False):
        blk = _local(e.tag) in _CELL_BLOCKS
        if blk:
            parts.append(" ")
        parts.append(e.text or "")
        for c in e:
            walk(c)
        if blk:
            parts.append(" ")
        if not top:
            parts.append(e.tail or "")
    walk(el, True)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _table_lines(tw):
    """표 → 'a | b | c' 행. rowspan·colspan을 펼쳐 열 정렬을 유지한다."""
    out, carry = [], {}                       # carry: 열 번호 → (텍스트, 남은 행 수)
    for tr in tw.iter():
        if _local(tr.tag) != "tr":
            continue
        pending = [td for td in tr if _local(td.tag) in ("td", "th")]
        row, col = [], 0
        while pending or col in carry:
            if col in carry:
                t, n = carry[col]
                row.append(t)
                if n > 1:
                    carry[col] = (t, n - 1)
                else:
                    del carry[col]
                col += 1
                continue
            td = pending.pop(0)
            t, rs, cs = _cell_text(td), _int(td.get("rowspan")), _int(td.get("colspan"))
            for _ in range(cs):
                row.append(t)
                if rs > 1:
                    carry[col] = (t, rs - 1)
                col += 1
        if any(row):
            out.append(" | ".join(row))
    for foot in tw.iter():                    # 표 각주: 문턱값·약어 정의가 자주 들어 있음
        if _local(foot.tag) == "table-wrap-foot":
            for x in foot:
                if _text(x):
                    out.append("  ※ " + _text(x))
    return out


def _list_lines(lst, ind=0):
    out = []
    for li in lst:
        if _local(li.tag) != "list-item":
            continue
        txt = " ".join(_text(x) for x in li if _local(x.tag) != "list").strip()
        if txt:
            out.append("  " * ind + "- " + txt)
        for sub in li:
            if _local(sub.tag) == "list":
                out += _list_lines(sub, ind + 1)
    return out


BLOCK_IN_P = ("table-wrap", "boxed-text", "fig", "list", "def-list")


def _p_text(p):
    """<p>의 자체 텍스트 — 중첩 블록(표·상자·그림·목록)은 빼고 그 뒤 tail만 잇는다."""
    parts = [p.text or ""]
    for c in p:
        if _local(c.tag) not in BLOCK_IN_P:
            parts.append("".join(c.itertext()))
        parts.append(c.tail or "")
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _cap(el):
    return " ".join(x for x in (_text(el.find("label")), _text(el.find("caption"))) if x)


def _jats_lines(el, depth=0):
    out = []
    for ch in el:
        tag = _local(ch.tag)
        if tag in ("sec", "app"):
            t = _text(ch.find("title"))
            if t:
                out.append("\n" + "#" * min(depth + 2, 6) + " " + t)
            out += _jats_lines(ch, depth + 1)
        elif tag == "p":
            nested = [c for c in ch if _local(c.tag) in BLOCK_IN_P]
            if nested:                        # BMC/Springer: <p> 안에 표·상자·그림·목록이 중첩됨 (v5)
                t = _p_text(ch)
                if t:
                    out.append(t)
                out += _jats_lines(ch, depth)
            else:
                out.append(_text(ch))
        elif tag == "list":
            out += _list_lines(ch)
        elif tag == "def-list":
            for di in ch.iter():
                if _local(di.tag) == "def-item":
                    out.append(f"- {_text(di.find('term'))}: {_text(di.find('def'))}")
        elif tag == "table-wrap":
            out.append(f"\n[TABLE] {_cap(ch)}".rstrip())
            out += _table_lines(ch)
        elif tag == "boxed-text":             # 진단기준 상자: 안의 문단·목록·표까지 모두
            out.append(f"\n[BOX] {_cap(ch)}".rstrip())
            out += _jats_lines(ch, depth)
            out.append("[/BOX]")
        elif tag == "fig":
            out.append(f"\n[FIGURE] {_cap(ch)}".rstrip())
        elif tag not in ("title", "label", "caption", "table-wrap-foot", "ref-list", "fn-group", "ack"):
            out += _jats_lines(ch, depth)
    return out


XLINK = "{http://www.w3.org/1999/xlink}href"


def _local(tag):
    return tag.split("}")[-1] if isinstance(tag, str) else ""


def jats_supplements(root):
    """JATS의 <supplementary-material>/<media> 목록 → [{href, label, caption}] (본문·back 어디에 있든)."""
    out, seen = [], set()
    if root is None:
        return out
    for sm in root.iter():
        if _local(sm.tag) != "supplementary-material":
            continue
        cap = " ".join(x for x in (_text(sm.find("label")), _text(sm.find("caption"))) if x)
        hrefs = [sm.get(XLINK) or sm.get("href")] + [m.get(XLINK) or m.get("href") for m in sm.iter()
                                                      if _local(m.tag) in ("media", "inline-supplementary-material")]
        for h in hrefs:
            if h and h not in seen:
                seen.add(h)
                out.append({"href": h, "caption": cap[:300]})
    return out


# back matter 중 진단기준과 무관한 행정 섹션 (BMC·Springer 등의 Declarations 묶음)
BACK_NOISE = re.compile(r"\b(declarations?|ethic\w*|competing|conflicts? of interest|funding|acknowledg\w*|"
                        r"authors?'? ?contributions?|contributors?|availability|data sharing|sharing statement|consent|disclosures?|"
                        r"supplementary (material|data|information)|electronic supplementary)\b", re.I)


def jats_figures(root):
    """그림 목록 → [{href, caption}] (진단 알고리즘·기준 그림을 고르는 데 사용)."""
    out = []
    for fg in (root.iter() if root is not None else []):
        if _local(fg.tag) != "fig":
            continue
        for g in fg.iter():
            if _local(g.tag) == "graphic" and (g.get(XLINK) or g.get("href")):
                out.append({"href": g.get(XLINK) or g.get("href"), "caption": _cap(fg)[:400]})
    return out


# ═════════════════════════ [3b] 기준 논문 추적 (v0.8.6) ═════════════════════════
# 검증(2026-10-24, NCC·DILI·AOSD): DILI 리뷰·CSH 지침이 인용한 RUCAM 2016(PMC)을 추가 확보; AASLD 2023 practice guidance도 추적 대상.
# NCC는 Del Brutto 2017·1996(비OA)을, AOSD는 Yamaguchi 1992·Fautrel 2002(비OA)를 웹검색 힌트 맨 앞에 올림.
# 본문 문장이 '기준'을 가리킬 때 그 문장이 인용한 참고문헌을 모은다. 치료·역학 문장의 인용은 걸리지 않도록 명사구 단위로 둔다.
KW_CITE_CRIT = re.compile(
    r"\b(diagnostic criteria|classification criteria|case definitions?|consensus (criteria|definitions?)|criteria (for|of) "
    r"|(proposed|revised|updated|validated|international|preliminary) criteria|defined as|definition of"
    r"|according to the [^.]{0,40}criteria|scoring system|diagnostic (score|algorithm|scale)|causality assessment"
    r"|score ranges|total score|cut-?off|threshold of|likelihood categor"
    r"|\b[A-Z][A-Za-z]{2,}(?:-[A-Za-z]+)? criteria\b)", re.I)


def _p_stream(el, buf, marks):
    """<p> 안을 문서 순서대로 훑어 텍스트는 buf에, 참고문헌 xref는 (buf 위치, rid)로 marks에 쌓는다. 중첩 블록(표·상자·그림)은 건너뜀."""
    buf.append(el.text or "")
    for ch in el:
        tag = _local(ch.tag)
        if tag == "xref" and ch.get("ref-type") == "bibr":
            for rid in (ch.get("rid") or "").split():
                marks.append((sum(len(x) for x in buf), rid))
        elif tag not in BLOCK_IN_P:
            _p_stream(ch, buf, marks)
        buf.append(ch.tail or "")


def _ref_meta(ref):
    """<ref> → {pmid, doi, pmcid, title, year, journal}"""
    ids = {}
    for pid in ref.iter():
        if _local(pid.tag) == "pub-id" and pid.get("pub-id-type"):
            ids[pid.get("pub-id-type").lower()] = (pid.text or "").strip()
    title = next((_text(e) for e in ref.iter() if _local(e.tag) in ("article-title", "chapter-title")), "")
    if not title:                                   # 제목 태그가 없는 mixed-citation은 전체 문자열에서 짧게
        title = _text(ref)[:200]
    year = next(((e.text or "").strip()[:4] for e in ref.iter() if _local(e.tag) == "year"), "")
    src = next((_text(e) for e in ref.iter() if _local(e.tag) == "source"), "")
    return {"pmid": ids.get("pmid", ""), "doi": ids.get("doi", ""), "pmcid": ids.get("pmcid", ""),
            "title": title, "year": year, "journal": src}


def jats_criteria_refs(root):
    """본문의 '기준 문장'이 인용한 참고문헌 → [{rid, pmid, doi, pmcid, title, year, journal, n_cites, sentences}] (인용 횟수 순).
    문장 경계는 마침표·물음표·느낌표. 인용 표지가 문장 끝 바로 뒤(위첨자)에 오는 경우도 그 문장에 넣는다."""
    if root is None:
        return []
    refs = {r.get("id"): r for r in root.iter() if _local(r.tag) == "ref" and r.get("id")}
    if not refs:
        return []
    hits = {}
    for p in root.iter():
        if _local(p.tag) != "p":
            continue
        buf, marks = [], []
        _p_stream(p, buf, marks)
        text = "".join(buf)
        if not marks or not KW_CITE_CRIT.search(text):
            continue
        bounds = [b.end() for b in re.finditer(r"[.!?](?=\s+[A-Z\[(]|\s*$)", text)]   # 소수점(1.5)·'Fig. 1'에서는 끊지 않음
        start = 0
        for end in bounds or [len(text)]:
            sent, s0, s1 = text[start:end], start, end
            start = end
            if not KW_CITE_CRIT.search(sent):
                continue
            rids = [rid for pos, rid in marks if s0 <= pos <= s1 + 3]
            for rid in dict.fromkeys(rids):
                if rid not in refs:
                    continue
                h = hits.setdefault(rid, {"rid": rid, "n_cites": 0, "sentences": [], **_ref_meta(refs[rid])})
                h["n_cites"] += 1
                if len(h["sentences"]) < 2:
                    h["sentences"].append(re.sub(r"\s+", " ", sent).strip()[:220])
    return sorted(hits.values(), key=lambda h: (-h["n_cites"], h["rid"]))


def _jats(root):
    """JATS → 텍스트: 초록 + 본문 + 부록(back의 app/sec). 표는 행렬 펼침·각주 포함, 상자·목록·정의목록 보존."""
    body = root.find(".//body") if root is not None else None
    if body is None:
        return None, ""
    lic = _text(root.find(".//license")) or _text(root.find(".//permissions"))
    parts = []
    meta = next((e for e in root.iter() if _local(e.tag) == "article-meta"), None)
    if meta is not None:
        for ab in meta:
            if _local(ab.tag) == "abstract" and ab.get("abstract-type") not in ("graphical", "teaser", "toc"):
                lines = _jats_lines(ab) or [_text(ab)]
                parts.append("# Abstract\n" + "\n".join(lines).strip())
                break
    parts.append("# Main text\n" + "\n".join(_jats_lines(body)).strip())
    back = next((e for e in root.iter() if _local(e.tag) == "back"), None)
    if back is not None:
        app = [e for e in back.iter() if _local(e.tag) in ("app-group", "glossary")]
        app += [e for e in back if _local(e.tag) == "sec" and not BACK_NOISE.search(_text(e.find("title")))]
        lines = []
        for e in app:
            t = _text(e.find("title")) if _local(e.tag) == "sec" else ""
            lines += ([f"## {t}"] if t else []) + _jats_lines(e)
        if any(l.strip() and not l.lstrip().startswith("#") for l in lines):   # 제목만 있는 back은 버림
            parts.append("# Appendix (article back matter)\n" + "\n".join(lines).strip())
    floats = next((e for e in root.iter() if _local(e.tag) == "floats-group"), None)
    if floats is not None:                    # 본문 밖에 모아 둔 표·상자·그림 (저자원고·Lancet·Elsevier 등) (v5)
        lines = _jats_lines(floats)
        if any(l.strip() for l in lines):
            parts.append("# Tables, boxes and figures (floats)\n" + "\n".join(lines).strip())
    return "\n\n".join(parts).strip(), lic[:300]


def ft_ncbi_pmc(c):
    if not c.get("pmcid"):
        return None, "PMCID 없음"
    root, err = ncbi("efetch.fcgi", {"db": "pmc", "id": c["pmcid"].replace("PMC", ""), "retmode": "xml"})
    txt, lic = _jats(root)
    if not txt:
        return None, err or "PMC에 있으나 XML 다운로드 불허"
    return {"text": txt, "license": lic, "source": "ncbi_pmc", "pmcid": c["pmcid"],
            "suppl_list": jats_supplements(root), "fig_list": jats_figures(root), "criteria_refs": jats_criteria_refs(root),
            "url": f"https://pmc.ncbi.nlm.nih.gov/articles/{c['pmcid']}/"}, ""


def ft_europepmc(c):
    pmcid = c.get("pmcid")
    if not pmcid and c.get("pmid"):                     # PubMed에 PMCID가 없어도 Europe PMC에는 있을 수 있음
        data, err = epmc("search", {"query": f"EXT_ID:{c['pmid']} AND SRC:MED", "resultType": "core",
                                    "format": "json"})
        hit = ((data or {}).get("resultList", {}).get("result") or [{}])[0]
        if hit.get("inEPMC") != "Y":
            return None, "Europe PMC 원문 없음"
        pmcid = hit.get("pmcid", "")
    if not pmcid:
        return None, "Europe PMC 원문 없음"
    raw, err, _ = http_get(f"{EPMC}{pmcid}/fullTextXML", accept="application/xml")
    if raw is None:
        return None, f"Europe PMC XML 없음 ({err})"
    try:
        root = ET.fromstring(raw)
        txt, lic = _jats(root)
    except ET.ParseError:
        return None, "Europe PMC XML 해석 실패"
    if not txt:
        return None, "Europe PMC 본문 없음"
    return {"text": txt, "license": lic or c.get("license", ""), "source": "europepmc", "pmcid": pmcid,
            "suppl_list": jats_supplements(root), "fig_list": jats_figures(root), "criteria_refs": jats_criteria_refs(root),
            "url": f"https://europepmc.org/article/PMC/{pmcid}"}, ""


def pdf_to_text(pdf):
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf)
        p = f.name
    try:
        if shutil.which("pdftotext"):
            r = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", p, "-"], capture_output=True, timeout=120)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.decode("utf-8", "replace")
        try:
            from pypdf import PdfReader
            return "\n".join((pg.extract_text() or "") for pg in PdfReader(p).pages)
        except ImportError:
            return None
    finally:
        os.unlink(p)


def html_to_text(raw):
    s = raw.decode("utf-8", "replace")
    s = re.sub(r"(?is)<(script|style|nav|header|footer|noscript|svg)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<(br|/p|/div|/tr|/h[1-6]|/li|/table)[^>]*>", "\n", s)
    s = re.sub(r"(?i)</t[dh]>", " | ", s)
    s = unescape(re.sub(r"<[^>]+>", " ", s))
    return "\n".join(l.strip() for l in s.splitlines() if l.strip())


def ft_unpaywall(c):
    if not c.get("doi"):
        return None, "DOI 없음"
    raw, err, _ = http_get(f"https://api.unpaywall.org/v2/{urllib.parse.quote(c['doi'])}",
                           {"email": EMAIL}, "application/json")
    if raw is None:
        return None, f"Unpaywall 조회 실패 ({err})"
    try:
        u = json.loads(raw)
    except json.JSONDecodeError:
        return None, "Unpaywall 응답 해석 실패"
    if not u.get("is_oa"):
        return None, "Unpaywall: OA 사본 없음"
    locs = u.get("oa_locations") or []
    tries = [(l, l["url_for_pdf"], "pdf") for l in locs if l.get("url_for_pdf")]
    tries += [(l, l["url"], "html") for l in locs if l.get("url") and not l.get("url_for_pdf")]
    tries.sort(key=lambda x: (x[2] != "pdf", x[0].get("host_type") != "publisher"))
    notes = []
    for loc, url, kind in tries[:4]:
        raw, ctype, final = http_get(url, accept="application/pdf,text/html;q=0.9", retries=2)
        time.sleep(0.5)
        if raw is None:
            notes.append(f"{kind} 다운로드 실패({ctype})")
            continue
        meta = {"license": loc.get("license") or "", "url": final, "version": loc.get("version") or "",
                "source": f"unpaywall_{loc.get('host_type', '?')}"}
        if raw[:5] == b"%PDF-":
            txt = pdf_to_text(raw)
            if txt and len(txt.strip()) >= MIN_PDF_CHARS:
                return {**meta, "text": txt, "pdf": raw, "source": meta["source"] + "_pdf"}, ""
            if txt is None:
                return {**meta, "text": "", "pdf": raw, "source": meta["source"] + "_pdf"}, \
                       "PDF만 저장 (pdftotext·pypdf 없음)"
            notes.append("PDF 텍스트 추출 실패")
        elif "html" in ctype.lower():
            txt = html_to_text(raw)
            if html_is_fulltext(txt):
                return {**meta, "text": txt, "source": meta["source"] + "_html"}, ""
            notes.append("HTML이 원문 아님(짧거나 소개 페이지)")
        else:
            notes.append(f"알 수 없는 형식 {ctype}")
    return None, "Unpaywall: " + ("; ".join(notes) or "사용 가능한 링크 없음")


FETCHERS = (ft_ncbi_pmc, ft_europepmc, ft_unpaywall)


# ═════════════════════════ [3-b] 부록(supplementary) 수집 ═════════════════════════
# 지침의 정의표·진단 알고리즘이 온라인 부록에만 있는 경우가 많다 (예: 38346436의 정의는 부록 s23).
SUPPL_SKIP_EXT = {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".bmp", ".svg", ".webp", ".jfif", ".heic",
                  ".eps", ".psd", ".ico", ".mp4", ".mov", ".avi", ".wmv", ".mpg", ".mpeg", ".mp3", ".wav", ".m4v",
                  ".webm", ".ogg"}
MAX_SUPPL_BYTES = 40_000_000     # 부록 파일 1개 최대 크기
MAX_SUPPL_FILES = 15
MIN_SUPPL_CHARS = 50       # 작은 기준표도 살리도록 낮게


def _xml_local_text(xml_bytes, para_tag, cell_tag=None, row_tag=None):
    """OOXML 간이 텍스트 추출: 문단 끝 줄바꿈, 표는 셀 ' | ' · 행 줄바꿈 (셀 안 문단은 공백으로)."""
    s = xml_bytes.decode("utf-8", "replace")
    if cell_tag:
        s = re.sub(rf"(?s)<(\w+:)?{cell_tag}[ >].*?</(\w+:)?{cell_tag}>",
                   lambda m: re.sub(rf"</(\w+:)?{para_tag}>", " ", m.group(0)), s)
        s = re.sub(rf"</(\w+:)?{cell_tag}>", " | ", s)
    if row_tag:
        s = re.sub(rf"</(\w+:)?{row_tag}>", "\n", s)
    s = re.sub(rf"</(\w+:)?{para_tag}>", "\n", s)
    s = re.sub(r"<(\w+:)?tab/>", "\t", s)
    return unescape(re.sub(r"<[^>]+>", "", s))


def docx_text(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return _xml_local_text(z.read("word/document.xml"), "p", "tc", "tr")


def xlsx_text(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        ss = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
                ss.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
        out = []
        for nm in sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)):
            out.append(f"## {os.path.basename(nm)[:-4]}")
            for row in ET.fromstring(z.read(nm)).iter():
                if _local(row.tag) != "row":
                    continue
                cells = []
                for cel in row:
                    if _local(cel.tag) != "c":
                        continue
                    v = next((x.text for x in cel if _local(x.tag) == "v"), None)
                    if cel.get("t") == "s" and v is not None and v.isdigit() and int(v) < len(ss):
                        v = ss[int(v)]
                    elif cel.get("t") == "inlineStr":
                        v = "".join(t.text or "" for t in cel.iter() if _local(t.tag) == "t")
                    cells.append(v or "")
                if any(cells):
                    out.append(" | ".join(cells))
        return "\n".join(out)


def suppl_to_text(name, data):
    """→ (텍스트 또는 None, 메모)."""
    ext = os.path.splitext(name)[1].lower()
    try:
        if data[:5] == b"%PDF-" or ext == ".pdf":
            t = pdf_to_text(data)
            return (t, "") if t else (None, "PDF 텍스트 변환 불가 (pdftotext·pypdf 없음)")
        if ext == ".docx":
            return docx_text(data), ""
        if ext == ".xlsx":
            return xlsx_text(data), ""
        if ext in (".htm", ".html"):
            return html_to_text(data), ""
        if ext in (".txt", ".csv", ".tsv", ".xml", ".md"):
            return data.decode("utf-8", "replace"), ""
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        return None, f"변환 실패 ({type(e).__name__})"
    return None, f"텍스트 변환 미지원 형식 ({ext or '확장자 없음'}) — 원본만 저장"


def _suppl_downloads(pmcid, listed):
    """부록 파일 내려받기: ① Europe PMC supplementaryFiles(zip) ② PMC /bin/<파일> 개별. → ([(이름, bytes, 출처)], 메모)"""
    files, notes = [], []
    raw, ctype, _ = http_get(f"{EPMC}{pmcid}/supplementaryFiles", accept="application/zip,*/*", retries=2)
    if raw and raw[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                for info in z.infolist():
                    nm = os.path.basename(info.filename)
                    if info.is_dir() or not nm or nm.startswith("."):
                        continue
                    if info.file_size > MAX_SUPPL_BYTES:
                        notes.append(f"{nm}: 크기 초과")
                        continue
                    files.append((nm, z.read(info), "europepmc_zip"))
        except zipfile.BadZipFile:
            notes.append("Europe PMC 부록 zip 손상")
    else:
        notes.append("Europe PMC 부록 없음" + (f" ({ctype})" if raw is None else ""))
    if not files:
        for it in listed[:MAX_SUPPL_FILES]:
            href = it["href"]
            if os.path.splitext(href)[1].lower() in SUPPL_SKIP_EXT:
                continue
            q, num = urllib.parse.quote(href), pmcid.replace("PMC", "")
            urls = [(f"https://europepmc.org/articles/{pmcid}/bin/{q}", "europepmc_bin"),
                    (f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/bin/{q}", "ncbi_pmc_bin"),
                    (f"https://pmc.ncbi.nlm.nih.gov/articles/instance/{num}/bin/{q}", "ncbi_pmc_bin"),
                    (f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/bin/{q}", "ncbi_pmc_bin")]
            why = []
            for url, src in urls:           # 사이트마다 부록 경로 형식이 달라 차례로 시도
                raw, ctype, _ = http_get(url, retries=1)
                time.sleep(0.4)
                if raw is None:
                    why.append(f"{src} {ctype}")
                    continue
                if raw.lstrip()[:1] == b"<" and not href.lower().endswith((".htm", ".html", ".xml")):
                    why.append(f"{src} HTML 반환(차단·로그인)")
                    continue
                files.append((os.path.basename(href), raw, src))
                break
            else:
                notes.append(f"{href}: 실패 ({'; '.join(why)})")
    return files, notes


PMC_CLOUD = "https://pmc-oa-opendata.s3.amazonaws.com/"
KW_FIG = re.compile(r"\b(algorithm|flow ?chart|flow ?diagram|criteria|criterion|diagnos\w*|classification|"
                    r"definitions?|work-?up|decision|approach)\b", re.I)
# 그림 캡션이 치료 전용이면 제외 (예: "Proposed algorithm for the therapeutic management of …")
KW_FIG_DX = re.compile(r"\b(diagnos\w*|criteria|criterion|classification|definitions?|work-?up|etiolog\w*|"
                       r"aetiolog\w*|evaluation|investigation|differential|screening|detection)\b", re.I)
KW_FIG_TX = re.compile(r"\b(therap\w*|treat\w*|management|dosing|induction|maintenance|prophylaxis)\b", re.I)


def is_dx_figure(caption):
    """진단 알고리즘·기준 그림인가: 키워드가 있고, 치료 전용(치료어만 있고 진단어 없음)이 아닐 것."""
    return bool(KW_FIG.search(caption)) and not (KW_FIG_TX.search(caption) and not KW_FIG_DX.search(caption))
MAX_FIGS = 6
_cloud_memo = {}


def cloud_meta(pmcid):
    """PMC Cloud Service(AWS S3 공개 버킷, NCBI 공식 배포 경로)의 문헌 메타데이터 JSON — 최신 버전.
    OA 데이터셋·저자원고 데이터셋에 없으면 None. 필드: license_code, is_manuscript, xml_url, pdf_url, media_urls"""
    if pmcid in _cloud_memo:
        return _cloud_memo[pmcid]
    meta = None
    raw, _, _ = http_get(PMC_CLOUD, {"list-type": "2", "prefix": f"metadata/{pmcid}."}, "application/xml", retries=2)
    keys = re.findall(r"<Key>(metadata/[^<]+\.json)</Key>", raw.decode("utf-8", "replace")) if raw else []
    if keys:
        key = max(keys, key=lambda k: _int(re.search(r"\.(\d+)\.json$", k).group(1), 0)
                  if re.search(r"\.(\d+)\.json$", k) else 0)
        raw, _, _ = http_get(PMC_CLOUD + key, accept="application/json", retries=2)
        try:
            meta = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            meta = None
    _cloud_memo[pmcid] = meta
    return meta


def _is_cc(code):
    return bool(code) and str(code).upper().startswith("CC")


def _https(u):
    """PMC Cloud 메타데이터의 URL은 s3://pmc-oa-opendata/<key>?md5=… 형식일 수 있다 → 익명 HTTPS 주소로."""
    m = re.match(r"s3://([^/]+)/(.+)$", u or "")
    return f"https://{m.group(1)}.s3.amazonaws.com/{m.group(2)}" if m else u


def _url_name(u):
    return urllib.parse.unquote(u.split("?")[0].rsplit("/", 1)[-1])


def _save_suppl_file(c, folder, i, nm, data, src, caption, pmcid):
    key, ext = _key(c), os.path.splitext(nm)[1].lower()
    stem = os.path.join(folder, f"{key}_suppl{i}_" + re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(nm)[0])[:60])
    with open(stem + (ext or ".bin"), "wb") as f:
        f.write(data)
    txt, note = suppl_to_text(nm, data)
    path = ""
    if txt and len(txt.strip()) >= MIN_SUPPL_CHARS:
        path = stem + ".txt"
        hdr = {"title": c.get("title", ""), "pmid": c.get("pmid", ""), "pmcid": pmcid,
               "supplement_of": key, "file": nm, "caption": caption, "source": src}
        with open(path, "w", encoding="utf-8") as f:
            f.write("---\n" + "\n".join(f"{k}: {v}" for k, v in hdr.items()) + "\n---\n\n" + txt + "\n")
    elif txt is not None:
        note = note or "텍스트가 너무 짧음"
    return {"file": nm, "source": src, "caption": caption, "raw_path": stem + (ext or ".bin"), "path": path,
            "chars": len(txt or ""), "note": note}


def fetch_supplements(c, res, folder, figures=True):
    """원문을 확보한 문헌의 부록(+ 진단 알고리즘·기준 그림)을 받는다.
    경로: ① PMC Cloud(S3, 공식)  ② Europe PMC supplementaryFiles zip  ③ 사이트 /bin/ 개별 파일
    PMC Cloud 메타데이터로 '비CC 저자원고(license_code=TDM)'이면 부록이 정책상 배포되지 않으므로 ②③을 건너뛴다.
    결과: <key>_suppl<N>_*.(원본|txt), <key>_fig<N>_*.(이미지), <key>_suppl.json"""
    key = _key(c)
    pmcid = (res or {}).get("pmcid") or c.get("pmcid") or ""
    if pmcid and not pmcid.startswith("PMC"):
        pmcid = "PMC" + pmcid
    listed, figs = (res or {}).get("suppl_list"), (res or {}).get("fig_list")
    if (listed is None or figs is None) and pmcid:   # 본문이 캐시·출판사 PDF에서 왔으면 JATS만 다시 읽음
        root, _ = ncbi("efetch.fcgi", {"db": "pmc", "id": pmcid.replace("PMC", ""), "retmode": "xml"})
        if root is None or root.find(".//body") is None:
            raw, _, _ = http_get(f"{EPMC}{pmcid}/fullTextXML", accept="application/xml", retries=1)
            try:
                root = ET.fromstring(raw) if raw else None
            except ET.ParseError:
                root = None
        listed = jats_supplements(root) if listed is None else listed
        figs = jats_figures(root) if figs is None else figs
    listed, figs = listed or [], figs or []
    man = {"version": __version__, "pmcid": pmcid, "doi": c.get("doi", ""), "n_listed": len(listed),
           "files": [], "figures": [], "notes": [], "license_code": "", "is_manuscript": None,
           "in_pmc_cloud": False, "policy_excluded": False,
           "listed": [{"href": i["href"], "caption": i["caption"]} for i in listed]}
    if not pmcid:
        man["notes"].append("PMCID 없음 — 부록 수집 경로 없음")
    else:
        os.makedirs(folder, exist_ok=True)
        caps = {os.path.basename(i["href"]): i["caption"] for i in listed}
        meta = cloud_meta(pmcid)
        files = []
        if meta:
            man.update(in_pmc_cloud=True, license_code=meta.get("license_code") or "",
                       is_manuscript=meta.get("is_manuscript"))
            media = meta.get("media_urls") or []
            for u in media:
                nm = _url_name(u)
                if os.path.splitext(nm)[1].lower() in SUPPL_SKIP_EXT:
                    continue
                raw, ctype, _ = http_get(_https(u), retries=2)
                if raw is None:
                    man["notes"].append(f"{nm}: PMC Cloud 다운로드 실패({ctype})")
                    continue
                files.append((nm, raw, "pmc_cloud"))
            if figures and figs:                # 진단 알고리즘·기준 그림 (캡션으로 선별)
                by_stem = {os.path.splitext(_url_name(u))[0]: u for u in media}
                for fg in [f for f in figs if is_dx_figure(f["caption"])][:MAX_FIGS]:
                    u = by_stem.get(os.path.splitext(os.path.basename(fg["href"]))[0])
                    if not u:
                        continue
                    raw, _, _ = http_get(_https(u), retries=2)
                    if raw:
                        nm = _url_name(u)
                        fp = os.path.join(folder, f"{key}_fig{len(man['figures']) + 1}_"
                                          + re.sub(r"[^A-Za-z0-9._-]+", "_", nm)[:80])
                        with open(fp, "wb") as f:
                            f.write(raw)
                        man["figures"].append({"file": nm, "caption": fg["caption"], "path": fp})
            if not files and meta.get("is_manuscript") and not _is_cc(meta.get("license_code")):
                man["policy_excluded"] = True
                if listed:
                    man["notes"].append(f"PMC 정책: 비CC 저자원고(license_code={meta.get('license_code')})의 부록은 "
                                        "배포되지 않음 — 출판사 사이트에서 확보")
        cloud_says_none = bool(meta) and not files and not listed   # Cloud 목록이 완전 — JATS도 부록 없다고 함
        if cloud_says_none:
            man["notes"].append("PMC Cloud·JATS 모두 부록 없음")
        if not files and not man["policy_excluded"] and not cloud_says_none:
            files, notes = _suppl_downloads(pmcid, listed)
            man["notes"] += notes
        for i, (nm, data, src) in enumerate(files[:MAX_SUPPL_FILES], 1):
            if os.path.splitext(nm)[1].lower() in SUPPL_SKIP_EXT:
                man["notes"].append(f"{nm}: 이미지·동영상 건너뜀")
                continue
            man["files"].append(_save_suppl_file(c, folder, i, nm, data, src, caps.get(nm, ""), pmcid))
    with open(os.path.join(folder, f"{key}_suppl.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    return man


def suppl_missing(man):
    """본문에 표시된 부록 중 받지 못한 것 → 웹·브라우저·수동 확보용 링크.
    (NIH 저자원고 NIHMS 부록은 Europe PMC·PMC 모두 자동 다운로드를 403으로 막는 경우가 많다)"""
    if not man or not man.get("pmcid"):
        return []
    if any(f.get("source") in ("europepmc_zip", "pmc_cloud") for f in man.get("files", [])):
        return []          # zip·Cloud는 부록 전체 — 파일명이 목록과 달라도 받은 것으로 본다
    got = {f.get("file") for f in man.get("files", [])}
    pmcid, out = man["pmcid"], []
    for it in man.get("listed", []):
        nm = os.path.basename(it["href"])
        if nm in got or os.path.splitext(nm)[1].lower() in SUPPL_SKIP_EXT:
            continue
        q = urllib.parse.quote(it["href"])
        if man.get("policy_excluded"):        # PMC에서는 정책상 못 받음 → 출판사(DOI) 우선
            links = ([f"https://doi.org/{man['doi']}"] if man.get("doi") else []) + \
                    [f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"]
            reason = f"PMC 정책상 미배포 (비CC 저자원고, license_code={man.get('license_code')})"
        else:
            links = [f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/bin/{q}",
                     f"https://europepmc.org/articles/{pmcid}/bin/{q}",
                     f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/#supplementary-material-sec"]
            reason = "자동 다운로드 실패 (차단·오류)"
        out.append({"file": nm, "caption": it.get("caption", ""), "reason": reason, "links": links})
    return out


def reusable_suppl(man):
    """이전 부록 기록을 다시 쓸지: 텍스트를 하나라도 얻었거나, 같은 버전에서 '부록 없음'으로 확인된 경우만.
    (실패 기록이거나 이전 버전 기록이면 다시 시도)"""
    if not man or man.get("version") != __version__:      # 버전이 바뀌면 (새 경로·그림 수집 반영) 다시 시도
        return None
    if any(f.get("path") for f in man.get("files", [])):
        return man
    if man.get("version") == __version__ and (man.get("policy_excluded")
                                              or (not man.get("n_listed") and not man.get("files"))):
        return man
    return None


def load_suppl_manifest(c, folder):
    p = os.path.join(folder, f"{_key(c)}_suppl.json")
    if os.path.exists(p):
        try:
            man = json.load(open(p, encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        man["files"] = [f for f in man.get("files", [])       # 이전 버전이 남긴 이미지 항목 정리
                        if os.path.splitext(f.get("file", ""))[1].lower() not in SUPPL_SKIP_EXT]
        for f in man.get("files", []) + man.get("figures", []):   # 캐시에서 복사된 경우 경로를 현재 폴더 기준으로
            for k in ("path", "raw_path"):
                if f.get(k):
                    q = os.path.join(folder, os.path.basename(f[k]))
                    f[k] = q if os.path.exists(q) else ""
        return man
    return None


def fetch_fulltext(c):
    notes = []
    for fn in FETCHERS:
        res, note = fn(c)
        if res:
            return res, note
        notes.append(f"{fn.__name__[3:]}: {note}")
    return None, " / ".join(notes)


def _key(c):
    return c.get("pmid") or re.sub(r"[^A-Za-z0-9]+", "_", c.get("epmc_id") or c.get("doi") or "x")


JATS_SOURCES = ("ncbi_pmc", "europepmc")


def save_doc(c, res, folder):
    os.makedirs(folder, exist_ok=True)
    stem = os.path.join(folder, f"{_key(c)}_{res['source']}")
    pdf_path = ""
    if res.get("pdf"):
        pdf_path = stem + ".pdf"
        with open(pdf_path, "wb") as f:
            f.write(res["pdf"])
    txt_path, n = "", 0
    if res.get("text"):
        txt_path = stem + ".txt"
        hdr = {"title": c.get("title", ""), "pmid": c.get("pmid", ""), "pmcid": c.get("pmcid", ""),
               "doi": c.get("doi", ""), "journal": c.get("journal", ""), "year": c.get("year", ""),
               "source": res["source"], "url": res.get("url", ""), "version": res.get("version", ""),
               "license": res.get("license", ""), "language": c.get("language", ""),
               "parser": PARSER_VERSION if res["source"] in JATS_SOURCES else ""}
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write("---\n" + "\n".join(f"{k}: {v}" for k, v in hdr.items()) + "\n---\n\n" + res["text"] + "\n")
        n = len(res["text"])
    return txt_path, pdf_path, n


def _key_files(folder, key):
    """폴더 안 이 문헌의 모든 파일 (본문·PDF·부록)."""
    return sorted(glob.glob(os.path.join(folder, f"{glob.escape(key)}_*")))


def _main_txt(folder, key):
    """본문 txt만 (부록 <key>_suppl* 제외)."""
    return [p for p in sorted(glob.glob(os.path.join(folder, f"{glob.escape(key)}_*.txt")))
            if not os.path.basename(p)[len(key) + 1:].startswith("suppl")]


def to_cache(folder, key, cache_dir):
    """이 문헌의 파일(본문·PDF·부록)을 실행 간 공유 캐시로 복사 (출력 폴더를 바꾸거나 출처 서버가
    일시적으로 실패해도 재사용)."""
    if not cache_dir:
        return
    os.makedirs(cache_dir, exist_ok=True)
    for f in _key_files(folder, key):
        shutil.copy2(f, os.path.join(cache_dir, os.path.basename(f)))


def find_cached(c, folder, cache_dir=None):
    """출력 폴더 → 공유 캐시 순으로 찾는다. 캐시에서 찾으면 이 문헌의 파일 전부를 출력 폴더로 복사한다."""
    key = _key(c)
    hits = _main_txt(folder, key)
    if not hits and cache_dir and _main_txt(cache_dir, key):
        os.makedirs(folder, exist_ok=True)
        for f in _key_files(cache_dir, key):
            shutil.copy2(f, os.path.join(folder, os.path.basename(f)))
        hits = _main_txt(folder, key)
    for p in hits:
        src = os.path.basename(p)[len(_key(c)) + 1:-4]
        body = open(p, encoding="utf-8").read()
        meta = dict(re.findall(r"^(\w+): (.*)$", body.split("---", 2)[1], re.M)) if body.startswith("---") else {}
        pdf = p[:-4] + ".pdf"
        stale = src in JATS_SOURCES and str(meta.get("parser", "")) != str(PARSER_VERSION)
        if src.startswith("unpaywall") and src.endswith("_html") and not html_is_fulltext(body.split("---", 2)[-1]):
            continue                          # v0.8.2: 예전 기준으로 저장된 소개 페이지는 캐시로 쓰지 않음 (다시 받기 시도)
        return {"source": src, "url": meta.get("url", ""), "license": meta.get("license", ""),
                "version": meta.get("version", ""), "path": p, "pdf_path": pdf if os.path.exists(pdf) else "",
                "chars": len(body), "cached": True, "stale": stale}
    return None


def slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "_", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
                  ).strip("_")[:60] or "dx"


# ═════════════════════════ [3c] 기준 완전성 점수 (v0.8.7) ═════════════════════════
CS_CAPTION = re.compile(r"^\[(TABLE|BOX)\][^\n]*\b(criteri(a|on)|diagnos|classification|case definitions?|definitions? (of|for)|scor(e|ing)|scale|algorithm|checklist)", re.I | re.M)
CS_SCORE_ROW = re.compile(r"\|\s*[+−-]\d{1,2}\s*(\||$)|\b\d{1,2} points?\b")   # 표 행의 점수 칸(+2, −1) 또는 "n points"
CS_CRIT_ROW = re.compile(r"\b(major|minor|absolute|at least|of the following|or more of)\b|[≥≤]", re.I)
CS_NUM = re.compile(r"(?:[≥≤<>]|>=|<=|\bover\b|\babove\b|\bbelow\b|\bat least\b)\s*\d+(?:[.,]\d+)?\s*(?:%|×|x\s*ULN|ULN|mg|g|mmol|μmol|umol|mL|ml|IU|U/|cells|days?|weeks?|hours?|h\b|mm|cm|°C|/μL|/uL|/mm3|times)", re.I)
CS_COMBO = re.compile(r"\b(at least (one|two|three|four|five|\d+) of|(one|two|three|four|five|\d+) of the following|(one|two|three|four|five|\d+) or more of|major (and|or|/) minor|two major|one major|absolute criteri)\b", re.I)
CS_SCORE = re.compile(r"\b(\d+ points?|points? (are|is) (assigned|given)|total score|cut-?off|scored? (of|≥|>=) ?\d|likelihood categor|highly probable|probable \(|possible \()\b", re.I)
CS_DEF = re.compile(r"\b(is defined as|are defined as|definition of|diagnostic criteria|classification criteria|case definition|diagnosis (requires|is established|is made when))\b", re.I)


def criteria_signals(text):
    """본문 텍스트 → {tables, thresholds, combos, scores, definition, completeness}. LLM 없이 정규식만."""
    if not text:
        return {"tables": 0, "score_rows": 0, "crit_rows": 0, "thresholds": 0, "combos": 0, "scores": 0, "definition": 0, "completeness": 0.0}
    if text.startswith("---"):                 # 저장 파일의 서지 머리말은 뺀다
        j = text.find("\n---", 3)
        text = text[j + 4:] if j > 0 else text
    rows = [ln for ln in text.split("\n") if " | " in ln]                   # 표 행(셀을 ' | '로 이은 줄)
    s = {"tables": len(CS_CAPTION.findall(text)),
         "score_rows": sum(bool(CS_SCORE_ROW.search(ln)) for ln in rows),
         "crit_rows": sum(bool(CS_CRIT_ROW.search(ln) or CS_NUM.search(ln)) for ln in rows),
         "thresholds": len(CS_NUM.findall(text)), "combos": len(CS_COMBO.findall(text)),
         "scores": len(CS_SCORE.findall(text)), "definition": 1 if CS_DEF.search(text) else 0}
    # 표 점수(JATS 표 마크업이 있어야 셀 수 있음, 최대 22)와 내용 점수(어느 형식에서나 세는 본문 신호, 최대 12)를 나눠 둔다
    s["table_score"] = round(3 * min(s["tables"], 2) + min(s["score_rows"], 16) / 2 + min(s["crit_rows"], 8), 1)
    s["content_score"] = round(min(s["thresholds"], 10) / 2 + min(s["combos"], 3) + min(s["scores"], 3) + s["definition"], 1)
    s["completeness"] = round(s["table_score"] + s["content_score"], 1)
    return s


def text_format(source):
    """저장 원문의 형식: xml(PMC·Europe PMC JATS, 표 마크업 있음) / html / pdf(2단 조판이면 줄이 섞임)."""
    src = (source or "").lower()
    return "xml" if ("pmc" in src and "unpaywall" not in src) else ("pdf" if src.endswith("pdf") else "html")


def doc_criteria_refs(c, res):
    """원문의 기준 문장 인용 목록. 지금 받은 JATS면 res에 있고, 캐시·PDF에서 온 원문은 PMCID로 JATS를 한 번 다시 읽는다."""
    if res and res.get("criteria_refs") is not None:
        return res["criteria_refs"]
    pmcid = (res or {}).get("pmcid") or c.get("pmcid") or ""
    if not pmcid:
        return []
    pmcid = pmcid if pmcid.startswith("PMC") else "PMC" + pmcid
    root, _ = ncbi("efetch.fcgi", {"db": "pmc", "id": pmcid.replace("PMC", ""), "retmode": "xml"})
    if root is None or root.find(".//body") is None:
        raw, _, _ = http_get(f"{EPMC}{pmcid}/fullTextXML", accept="application/xml", retries=1)
        try:
            root = ET.fromstring(raw) if raw else None
        except ET.ParseError:
            root = None
    return jats_criteria_refs(root)


def resolve_pmid(ref):
    """참고문헌의 PMID를 얻는다: pub-id → DOI 검색 → 제목 검색(정확히 1건일 때만)."""
    if ref.get("pmid"):
        return ref["pmid"]
    if ref.get("doi"):
        _, ids, _ = pubmed_search(f'"{ref["doi"]}"[doi]', 2)
        if len(ids) == 1:
            return ids[0]
    title = re.sub(r"[^A-Za-z0-9 ]+", " ", ref.get("title", "")).strip()
    if len(title) > 25:
        _, ids, _ = pubmed_search(f'"{title}"[ti]', 2)
        if len(ids) == 1:
            return ids[0]
    return ""


# ═════════════════════════ 진단명 하나 처리 ═════════════════════════
def retrieve(name, synonyms=(), k=4, per_query=10, max_tries=10, outdir="dxcriteria_out", folder_name=None,
             fetch=True, refresh=False, log=None, min_priority=3, weak_fallback=True, weak_min=1,
             cache_dir=None, supplements=True, figures=True, trace=True, trace_max=3):
    """진단명 하나에 대해 후보 검색 → 정리 → 원문 수집 → (v0.8.6) 기준 논문 추적. 결과 dict (manifest의 한 항목).
    · min_priority 이상 후보(primary)만 먼저 시도한다 (k를 채우려고 무관한 문헌까지 내려가지 않도록).
    · primary로 원문을 한 편도 못 얻었을 때만, 제목에 진단명이 있거나 리뷰인 weak_min~(min_priority-1)점
      후보를 '약한 출처(tier=weak)'로 시도한다 (지침·리뷰가 없는 희귀질환 대비). weak_fallback=False로 끔."""
    log = log or (lambda s: None)
    names = [name] + [s for s in dict.fromkeys(synonyms) if s and s.strip().lower() != name.lower()]
    folder = os.path.join(outdir, folder_name or slug(name))
    queries, cands = {}, {}

    def merge(meta, tag):
        key = meta.get("pmid") or meta.get("epmc_id")
        c = cands.setdefault(key, {"found_by": []})
        c["found_by"].append(tag)
        for f, v in meta.items():
            if v and not c.get(f):
                c[f] = v

    # [1] PubMed — 진단명의 통째 매핑 MeSH 개념부터
    concepts, per_name = mesh_concepts(names)
    log("  [MeSH 개념] " + ("; ".join(f"{n} → {', '.join(v)}" for n, v in per_name.items())
                            if per_name else "없음 (진단명이 MeSH에 통째로 매핑되지 않음 → T0는 구절[tiab]만)"))
    for tag, build in PUBMED_QUERIES:
        term = build(names, concepts)
        n, pmids, warns = pubmed_search(term, per_query)
        if per_query < n <= FULL_TAKE:        # v0.8: 결과가 적은 검색식은 전부 가져온다 (Best Match 상위 N은 호출마다 흔들림)
            n, pmids, warns = pubmed_search(term, n)
        note = "; ".join(warns)
        if pmids and not (tag == "T0" and concepts) and _all_names_dropped(warns, names):
            note += " → 모든 진단명 구절이 PubMed 색인에 없어 결과를 버림"
            n, pmids = 0, []
        queries[f"pubmed_{tag}"] = {"term": term, "count": n, "warning": note}
        for r, p in enumerate(pmids, 1):
            merge({"pmid": p}, f"pubmed:{tag}#{r}")
    meta = pubmed_meta([c["pmid"] for c in cands.values() if c.get("pmid")])
    for c in cands.values():
        if c.get("pmid") in meta:
            c.update({f: v for f, v in meta[c["pmid"]].items() if v or f not in c})
    # [1] Europe PMC (OA)
    q, n, hits, err = epmc_search(names, per_query)
    queries["europepmc_oa"] = {"term": q, "count": n, "warning": err}
    for r, h in enumerate(hits, 1):
        merge(h, f"europepmc#{r}")

    # [2] 정리
    for c in cands.values():
        c["key"] = _key(c)
        c["excluded"] = exclusion(c) or ("" if on_topic(c, names, concepts)
                                         else "관련성 없음 (제목·초록에 진단명 없고 MeSH 불일치)")
        c["priority"], c["priority_reasons"], c["_tie"] = priority(c, names, concepts)
        c["doc_kind"] = DOC_KIND[c["_tie"][0]]
    # v0.8.1: 공동 게재(같은 제목, 다른 PMID/학술지)는 제외하지 않고 '대체 사본'으로 표시 — 원본을 받으면 건너뛰고,
    # 원본이 비OA·실패면 사본을 시도한다 (어느 학술지 사본이 OA인지는 받아 봐야 알기 때문)
    by_title = {}
    for c in sorted([c for c in cands.values() if not c["excluded"]],
                    key=lambda c: (not c.get("pmcid"), -c["priority"], c["_tie"])):
        t = _norm(c.get("title", ""))
        if t and t in by_title:
            c["dup_of"] = by_title[t]
        elif t:
            by_title[t] = c["key"]
    kept = sorted([c for c in cands.values() if not c["excluded"]],
                  key=lambda c: (-c["priority"], bool(c.get("dup_of")), c["_tie"]))   # v0.8.4 검토: 같은 점수면 공동 게재 원본(PMC 우선)을 사본보다 먼저
    n_ex = len(cands) - len(kept)
    qs = " ".join(f"{k.split('_', 1)[1]}:{v['count']}" for k, v in queries.items() if k.startswith("pubmed_"))
    log(f"  [PubMed {qs} · EuropePMC(OA):{queries['europepmc_oa']['count']}] 후보 {len(cands)}편 (제외 {n_ex})")
    dropped = {}                    # 같은 '색인에 없는 구절' 경고는 검색식별로 반복하지 않고 한 줄로
    for qk, qv in queries.items():           # (k는 원문 목표 수 — 덮어쓰지 않도록 다른 이름)
        for w in [x.strip() for x in (qv["warning"] or "").split(";") if x.strip()]:
            if "→" in w:                     # 구절 전부 무시 → 결과 버림 표시는 따로
                w, tail = w.split("→", 1)
                log(f"    [!] {qk}: →{tail}")
            if w.startswith("ERROR"):
                log(f"    [!] {qk}: {w}")
                continue
            ph = re.sub(r"\[[^\]]*\]$", "", w).strip()
            if ph:
                dropped.setdefault(ph, []).append(qk.replace("pubmed_", "").replace("europepmc_oa", "EPMC"))
    if dropped:
        log("    [!] PubMed 색인에 없는 구절(검색에서 무시됨): "
            + "; ".join(f"{ph} ({','.join(ts)})" for ph, ts in dropped.items())[:220])

    # [3] 원문 수집
    docs, tries = [], [0]

    def attempt(c, tier):
        tries[0] += 1
        got, res = (None, None) if refresh else (find_cached(c, folder, cache_dir), None)
        if got and got.get("stale"):          # 예전 변환 규칙으로 만든 PMC 원문 → 다시 받아 변환 (실패하면 기존 것 사용)
            res2, _ = fetch_fulltext(c)
            if res2 and res2["source"] in JATS_SOURCES:
                if res2["source"] != got["source"] and os.path.exists(got["path"]):
                    os.remove(got["path"])
                path, pdf_path, nch = save_doc(c, res2, folder)
                res = res2
                got = {"source": res2["source"], "url": res2.get("url", ""), "license": res2.get("license", ""),
                       "version": "", "path": path, "pdf_path": pdf_path, "chars": nch, "cached": False,
                       "reparsed": True}
        if got is None:
            res, note = fetch_fulltext(c)
            c["fetch_notes"] = note
            if res:
                path, pdf_path, nch = save_doc(c, res, folder)
                got = {"source": res["source"], "url": res.get("url", ""), "license": res.get("license", ""),
                       "version": res.get("version", ""), "path": path, "pdf_path": pdf_path,
                       "chars": nch, "cached": False}
        c["fetched"] = bool(got)
        mark = "" if tier == "primary" else " [기준 논문]" if tier == "traced" else " [약한 출처]"
        if got:
            sup = None
            if supplements:      # 캐시에 부록 기록(<key>_suppl.json)이 있으면 재사용, 없으면 지금 받음
                sup = (None if refresh or res is not None else reusable_suppl(load_suppl_manifest(c, folder))) \
                      or fetch_supplements(c, res, folder, figures=figures)
            to_cache(folder, c["key"], cache_dir)
            s_ok = [f for f in (sup or {}).get("files", []) if f.get("path")]
            crefs = doc_criteria_refs(c, res) if (trace and tier != "traced") else []
            docs.append({**{f: c.get(f, "") for f in ("key", "pmid", "pmcid", "doi", "title", "journal", "year",
                                                      "pubtypes", "priority", "priority_reasons", "found_by",
                                                      "doc_kind", "language", "dup_of", "traced_from", "cite_count", "abstract_signals")}, **got, "tier": tier,
                         "criteria_refs": [{k2: r[k2] for k2 in ("pmid", "doi", "title", "year", "n_cites", "sentences")} for r in crefs[:5]],
                         "_crefs_all": crefs,
                         "supplements": (sup or {}).get("files", []), "n_suppl": len(s_ok),
                         "suppl_notes": (sup or {}).get("notes", []),
                         "figures": (sup or {}).get("figures", []), "n_fig": len((sup or {}).get("figures", [])),
                         "license_code": (sup or {}).get("license_code", ""),
                         "suppl_missing": suppl_missing(sup)})
            log(f"    ✓ [{c['key']}] ({got['source']}{', 캐시' if got['cached'] else ''}){mark} "
                f"{c.get('title', '')[:90]}")
            if sup and sup["files"]:
                srcs = ",".join(sorted({f["source"] for f in sup["files"]}))
                log(f"        + 부록 {len(sup['files'])}개 (텍스트 {len(s_ok)}개, {srcs}): "
                    + ", ".join(f["file"] for f in sup["files"])[:100]
                    + (f"  · 이미지 등 {sum('건너뜀' in x for x in sup['notes'])}개 건너뜀"
                       if any('건너뜀' in x for x in sup['notes']) else ""))
                if any("PMC Cloud" in x for x in sup["notes"]):
                    log("        · PMC Cloud 실패 후 대체 경로 사용: "
                        + "; ".join(x for x in sup["notes"] if "PMC Cloud" in x)[:110])
            elif sup and sup.get("policy_excluded") and sup.get("n_listed"):
                log(f"        · 부록 {sup['n_listed']}개: PMC 정책상 미배포 (비CC 저자원고 {sup['license_code']}) "
                    "→ 출판사 사이트")
            elif sup and sup.get("n_listed"):      # 본문에 부록이 있다고 돼 있는데 못 받은 경우만 알림
                log(f"        · 부록 {sup['n_listed']}개 표시돼 있으나 미확보 — {'; '.join(sup['notes'])[:100]}")
            if sup and sup.get("figures"):
                log(f"        + 진단 관련 그림 {len(sup['figures'])}개: "
                    + "; ".join(f["caption"][:40] for f in sup["figures"])[:110])
        else:
            log(f"    ✗ [{c['key']}]{mark} {c.get('title', '')[:60]}  — {c.get('fetch_notes', '')[:110]}")

    def oa_available(c):
        """v0.8: 받을 수 있는 후보인지 가볍게 판정 (PMCID 있음 → OA, 없으면 Unpaywall is_oa 1회). 비OA는 시도 예산에 넣지 않는다."""
        if c.get("pmcid"):
            c["oa"] = "pmc"; return True
        if not c.get("doi"):
            c["oa"] = ""; return False
        raw, err, _ = http_get(f"https://api.unpaywall.org/v2/{urllib.parse.quote(c['doi'])}", {"email": EMAIL}, "application/json")
        try:
            u = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            u = {}
        c["oa"] = "unpaywall" if u.get("is_oa") else ""
        return bool(u.get("is_oa"))

    primary = [c for c in kept if c["priority"] >= min_priority]
    weak_pool = [c for c in kept if weak_min <= c["priority"] < min_priority
                 and (has_name(c.get("title", ""), names) or _pt(c) & _REVIEW)]
    if fetch:
        n_nonoa = 0
        cap = max(2, -(-3 * k // 5))          # v0.8.2: 한 문헌 종류(지침·기준·리뷰…)가 차지할 수 있는 최대 편수 (k=5 → 3)
        group = lambda c: c.get("dup_of") or c["key"]     # 공동 게재 묶음 (원본 key)

        def try_one(c, use_cap):
            """한 후보 시도. 반환: 'cap'(상한으로 미룸) / None(시도·건너뜀 완료)."""
            nonlocal n_nonoa
            if use_cap and sum(d.get("doc_kind") == c.get("doc_kind") for d in docs) >= cap:
                return "cap"
            if any(group(d) == group(c) for d in docs):          # v0.8.4 검토: 같은 묶음을 이미 받았으면 (원본·사본 어느 쪽이든)
                c["fetched"] = False; c["fetch_notes"] = f"공동 게재 사본 (같은 묶음 {group(c)} 확보)"; return None
            if not oa_available(c):                               # 유료·비OA 후보는 시도하지 않고 웹검색 힌트로 (예산 보존)
                c["fetched"] = False; c["fetch_notes"] = "비OA(사전 판정: PMCID 없음, Unpaywall OA 없음)"; n_nonoa += 1
                return None
            attempt(c, "primary")
            return None

        late = [c for c in primary if c.get("doc_kind") == "treatment" or not is_english(c)]   # v0.8.4: 치료 전용·비영어
        for c in late:
            c.setdefault("late_reason", "치료 전용" if c.get("doc_kind") == "treatment" else f"비영어({c.get('language')})")
        # 시도 순서 (v0.8.4 검토): ① 일반 후보(우선순위 순, 종류별 상한) ② 상한으로 미룬 일반 후보 ③ 치료 전용·비영어
        deferred = []
        for c in [c for c in primary if c not in late]:
            if len(docs) >= k or tries[0] >= max_tries:
                break
            if try_one(c, True) == "cap":
                deferred.append(c)
        for c in deferred + late:
            if len(docs) >= k or tries[0] >= max_tries:
                break
            try_one(c, False)
        if n_nonoa:
            log(f"    · 비OA 후보 {n_nonoa}편은 시도하지 않음 (웹검색 힌트로)")
        below = [c for c in kept if c["priority"] < min_priority]
        if below and len(docs) < k:
            log(f"    · 우선순위 {min_priority}점 미만 {len(below)}편은 기본 시도 대상 아님 (최고 {below[0]['priority']}점)")
        if weak_fallback and not docs and weak_pool:
            log(f"    · {min_priority}점 이상 후보로 원문 0편 → 제목에 진단명이 있거나 리뷰인 {weak_min}~{min_priority - 1}점 후보 "
                f"{len(weak_pool)}편을 약한 출처로 시도")
            for c in weak_pool:
                if len(docs) >= k or tries[0] >= max_tries:
                    break
                if any(group(d) == group(c) for d in docs):
                    continue
                if not oa_available(c):
                    c["fetched"] = False; c["fetch_notes"] = "비OA(사전 판정)"; continue
                attempt(c, "weak")

    # [3b] 기준 논문 추적 (v0.8.6): 확보 원문이 기준 문장에서 인용한 참고문헌을 받는다 (k와 별도, 최대 trace_max편)
    traced_hints = []
    if fetch and trace and docs:
        pool_refs = {}
        for d in docs:
            for r in d.pop("_crefs_all", []) or []:
                ck = r.get("pmid") or (r.get("doi") or "").lower() or _norm(r.get("title", ""))
                if not ck:
                    continue
                e = pool_refs.setdefault(ck, {**r, "n_cites": 0, "cited_by": []})
                e["n_cites"] += r["n_cites"]
                e["cited_by"].append(d["key"])
        for r in pool_refs.values():                      # 추적 점수: 인용 횟수 + 참고문헌 제목이 기준·정의·점수 논문이면 +3, 진단명이 있으면 +1
            r["trace_score"] = r["n_cites"] + (3 if KW_CRIT.search(r.get("title", "")) or re.search(r"\b(score|scale|RUCAM|algorithm)\b", r.get("title", ""), re.I) else 0) \
                               + (2 if KW_GUIDE.search(r.get("title", "")) else 0) + (1 if has_name(r.get("title", ""), names) else 0)   # 지침·가이던스 제목도 추적 대상
        # v0.8.7: 제목 점수 상위 후보의 PMID를 풀고 PubMed 초록·출판유형을 받아 신호를 더한다 (지침 출판유형 +2, 초록의 기준 어휘·수치 기준·제안 구절 +1씩)
        pre = sorted((r for r in pool_refs.values() if (r.get("pmid") or r.get("doi")) and not treatment_only({"title": r.get("title", "")})),
                     key=lambda r: (-r["trace_score"], -r["n_cites"]))[:max(trace_max * 4, 8)]
        for r in pre:
            r["pmid"] = r.get("pmid") or resolve_pmid(r)
        metas = pubmed_meta([r["pmid"] for r in pre if r.get("pmid")])
        for r in pre:
            mt = metas.get(r.get("pmid"), {})
            r["abstract"], r["pubtypes"] = mt.get("abstract", ""), mt.get("pubtypes", [])
            if mt.get("pmcid") and not r.get("pmcid"):
                r["pmcid"] = mt["pmcid"]
            ab = r["abstract"]
            r["abstract_signals"] = [x for x, ok in (("출판유형 지침", bool(_pt(r) & {re.sub(r"s$", "", p.lower()) for p in GUIDE_PT})),
                                                     ("초록 기준 어휘", bool(ab and KW_CRIT.search(ab))),
                                                     ("초록 수치 기준", bool(ab and CS_NUM.search(ab))),
                                                     ("초록 제안 구절", bool(ab and re.search(r"\b(we propose|proposed? (new |revised |updated )?(diagnostic |classification )?criteria|new criteria|scoring system|diagnostic (algorithm|score|scale))\b", ab, re.I)))) if ok]
            r["on_topic"] = has_name(r.get("title", ""), names) or has_name(ab, names) or title_on_topic(r.get("title", ""), names)   # 진단명 구절(제목·초록) 또는 핵심 단어 절반(제목)
            r["eligible"] = r["on_topic"] and (r["trace_score"] >= 3 or r["n_cites"] >= 2 or "출판유형 지침" in r["abstract_signals"])   # 자격은 주제·제목·인용·지침 유형으로
            r["trace_score"] += (2 if "출판유형 지침" in r["abstract_signals"] else 0) + sum(1 for x in r["abstract_signals"] if x != "출판유형 지침")
            if treatment_only({"title": r.get("title", ""), "abstract": ab}):
                r["trace_score"] = -1
        off = [r for r in pre if not r["on_topic"] and (r["trace_score"] >= 3 or r["n_cites"] >= 2)]
        if off:
            log("    · 추적 제외(제목·초록에 진단명 없음): " + "; ".join(r.get("title", "")[:45] for r in off[:4]))
        ranked = sorted((r for r in pre if r["eligible"] and r["trace_score"] >= 0),   # 초록 신호는 순서만 바꾼다(자격은 주제·제목·인용·지침 유형)
                        key=lambda r: (-r["trace_score"], -r["n_cites"], -_int(r.get("year"), 0)))[:max(trace_max * 3, 6)]   # 비OA가 많아 시도 폭은 넉넉히
        if ranked:
            log(f"    ↳ 기준 논문 추적: 확보 원문의 기준 문장이 인용한 참고문헌 {len(ranked)}편 "
                + "(" + "; ".join(f"{r.get('title', '')[:45]} ×{r['n_cites']}/{r['trace_score']}점"
                                  + (f"[{','.join(r['abstract_signals'])}]" if r.get("abstract_signals") else "") for r in ranked[:3]) + ")")
        n_traced = 0
        for r in ranked:
            if n_traced >= trace_max:
                break
            pmid = resolve_pmid(r)
            key = pmid or re.sub(r"[^A-Za-z0-9]+", "_", r.get("doi") or r.get("title", "x"))[:60]
            if key in {d["key"] for d in docs} or any((d.get("dup_of") or d["key"]) == key for d in docs):
                continue                                    # 이미 받은 원문(검색으로 찾은 것)은 다시 받지 않음
            c = cands.get(pmid) if pmid else None
            if c is None:
                c = cands.setdefault(key, {"found_by": [], "pmid": pmid, "doi": r.get("doi", ""), "pmcid": r.get("pmcid", ""),
                                            "title": r.get("title", ""), "year": r.get("year", ""), "journal": r.get("journal", "")})
                if pmid:
                    c.update({f: v for f, v in metas.get(pmid, {}).items() if v})
                c["key"] = key
            c["abstract_signals"] = r.get("abstract_signals", [])
            c["found_by"] = c.get("found_by", []) + [f"cited_by:{','.join(r['cited_by'])}#{r['n_cites']}"]
            c["traced_from"], c["cite_count"] = ",".join(r["cited_by"]), r["n_cites"]
            c["priority"] = 8 + min(r["n_cites"], 3)
            c["priority_reasons"] = [f"인용 추적: 확보 원문의 기준 문장에서 {r['n_cites']}회 인용 +{c['priority']}"]
            c["_tie"], c["doc_kind"] = (0, 0), "criteria_paper"
            c["excluded"] = c.get("excluded") or exclusion(c)
            if c["excluded"]:
                c["fetched"] = False; c["fetch_notes"] = f"인용 추적 후보 제외: {c['excluded']}"; continue
            if c.get("fetched"):
                continue
            if c.get("fetch_notes"):                            # 검색 단계에서 이미 시도·판정된 후보(비OA 등)는 힌트로만
                traced_hints.append(c)
                log(f"    · [{key}] [기준 논문, 인용 {r['n_cites']}회] {c.get('title', '')[:60]}  — 검색 단계에서 이미 시도: {c['fetch_notes'][:50]} → 웹검색 힌트")
                continue
            if not oa_available(c):
                c["fetched"] = False; c["fetch_notes"] = "기준 논문(인용 추적) — 비OA, 웹검색 힌트로"
                traced_hints.append(c)
                log(f"    ✗ [{key}] [기준 논문, 인용 {r['n_cites']}회] {c.get('title', '')[:60]}  — 비OA → 웹검색 힌트")
                continue
            before = len(docs)
            attempt(c, "traced")
            if len(docs) > before:
                n_traced += 1
                docs[-1]["traced_from"], docs[-1]["cite_count"] = c["traced_from"], c["cite_count"]
    for d in docs:
        d.pop("_crefs_all", None)
    # [3c] 기준 완전성 (v0.8.7): 받은 원문마다 본문 신호를 세고 상위 2편을 recommended에 적는다 (v0.8.8: 참고 순위, 거르지 않음)
    for d in docs:
        try:
            txt = open(d["path"], encoding="utf-8", errors="ignore").read() if d.get("path") else ""
        except OSError:
            txt = ""
        sig = criteria_signals(txt)
        d["text_format"] = text_format(d.get("source"))
        d["criteria_signals"] = {k2: v for k2, v in sig.items() if k2 not in ("completeness", "table_score", "content_score")}
        d["table_score"], d["content_score"], d["completeness"] = sig["table_score"], sig["content_score"], sig["completeness"]
    recommended = [d["key"] for d in sorted(docs, key=lambda d: (-d["completeness"], -_int(d.get("year"), 0)))[:2] if d["completeness"] > 0]
    if docs:
        log("    ◆ 완전성 상위(참고): " + (", ".join(recommended) if recommended else "없음(기준 신호 없음)")
            + " · 기준 완전성(형식 표점수+내용점수=합): "
            + "; ".join(f"[{d['key']}] {d['text_format']} {d['table_score']}+{d['content_score']}={d['completeness']}"
                        for d in sorted(docs, key=lambda d: -d["completeness"]))[:320])

    status = "fulltext_found" if docs else ("candidates_only" if kept else "no_candidates")
    got_keys = {d["key"] for d in docs}
    strong = [d for d in docs if d["tier"] in ("primary", "traced")]   # v0.8.6: 추적한 기준 논문은 약한 출처가 아님
    # 웹검색 판단 (위에서부터 첫 해당 사유)
    top_doc = max((d["priority"] for d in strong), default=None)
    got_groups = {d.get("dup_of") or d["key"] for d in docs}
    missed_better = [c for c in primary if c["key"] not in got_keys and top_doc is not None
                     and c["priority"] >= top_doc and c.get("doc_kind") != "treatment"     # v0.8.4 검토: 치료 전용은 '더 나은 후보' 아님
                     and (c.get("dup_of") or c["key"]) not in got_groups]                  # 받은 문헌의 공동 게재 사본도 아님
    has_core = any(d["doc_kind"] in ("guideline", "criteria", "criteria_paper") for d in strong)
    if not docs:
        why_need = ("원문 0편 — " + (f"관련 후보 없음 (검색 결과 {len(cands)}편 모두 제외)" if not kept
                                    else f"관련 후보 {len(kept)}편의 OA 원문 없음"))
        need = True
    elif not strong:
        need, why_need = True, f"확보 원문 {len(docs)}편이 모두 약한 출처 (우선순위 {min_priority}점 미만)"
    elif missed_better:
        need, why_need = True, (f"확보 원문보다 우선순위가 높거나 같은 후보 {len(missed_better)}편의 원문 미확보 "
                                f"(최상위: {missed_better[0].get('pmid') or missed_better[0]['key']})")
    elif len(docs) < k and not has_core:
        need, why_need = True, f"원문 {len(docs)}편(목표 {k})이고 지침·진단기준 문헌 없음"
    else:
        need, why_need = False, ""
    # 힌트: 확보 원문보다 나은 후보 → 나머지 primary → 약한 후보 (각 우선순위 순)
    pool = missed_better + [c for c in kept if c["key"] not in got_keys and c not in missed_better
                            and (c.get("dup_of") or c["key"]) not in got_groups
                            and (c in primary or c in weak_pool)]
    pool.sort(key=lambda c: c.get("doc_kind") == "treatment")   # v0.8.4 검토: 치료 전용 힌트는 뒤로 (안정 정렬)
    pool = traced_hints + [c for c in pool if c not in traced_hints]   # v0.8.6: 비OA 기준 논문은 맨 앞
    hints = [{"title": c.get("title", ""), "doi": c.get("doi", ""), "pmid": c.get("pmid", ""),
              "year": c.get("year", ""), "journal": c.get("journal", ""), "priority": c["priority"],
              "doc_kind": c["doc_kind"], "cite_count": c.get("cite_count", 0),
              "link": f"https://doi.org/{c['doi']}" if c.get("doi") else
                      (f"https://pubmed.ncbi.nlm.nih.gov/{c['pmid']}/" if c.get("pmid") else "")}
             for c in pool][:5]
    if need:
        log(f"    ⇒ 웹검색 권고: {why_need}")
    suppl_todo = [{"key": d["key"], "pmid": d.get("pmid", ""), "title": d.get("title", ""), **m}
                  for d in docs for m in d.get("suppl_missing", [])]
    if suppl_todo:
        log("    ⇒ 부록 수동·브라우저 확보 필요: " + "; ".join(f"[{x['key']}] {x['file']}" for x in suppl_todo)[:150])
    return {"diagnosis": name, "synonyms": names[1:], "mesh_concepts": concepts, "status": status,
            "n_traced_documents": sum(d["tier"] == "traced" for d in docs),
            "recommended": recommended if docs else [],
            "needs_web_search": need, "web_search_reason": why_need,
            "n_candidates": len(cands), "n_excluded": n_ex, "n_documents": len(docs),
            "n_weak_documents": len(docs) - len(strong),
            "suppl_to_fetch": suppl_todo,       # 원문은 받았지만 부록을 못 받은 것 (링크 포함)
            "queries": queries, "documents": docs, "web_search_hints": hints,
            "candidates": [{f: c.get(f) for f in ("key", "pmid", "pmcid", "doi", "epmc_id", "title", "year",
                                                   "journal", "language", "pubtypes", "found_by", "priority", "doc_kind",
                                                   "priority_reasons", "excluded", "dup_of", "late_reason", "fetched", "fetch_notes",
                                                   "traced_from", "cite_count", "abstract_signals")}
                           for c in sorted(cands.values(), key=lambda c: (bool(c["excluded"]), -c["priority"]))]}


# ═════════════════════════ 진단명 입력 ═════════════════════════
def names_from_report(path):
    t = open(path, encoding="utf-8").read()
    i = t.index("window.__DATA__ = ") + len("window.__DATA__ = ")
    D = json.JSONDecoder().raw_decode(t[i:])[0]
    return list(dict.fromkeys(x["name"] for x in D.get("final", []) + D.get("declined", [])))


def load_inputs(a):
    """→ [(진단명, [동의어])]"""
    items = []
    if a.report:
        items += [(n, []) for n in names_from_report(a.report)]
    if a.dx_file:
        for line in open(a.dx_file, encoding="utf-8"):
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if parts and not parts[0].startswith("#"):
                items.append((parts[0], parts[1:]))
    if a.dx:
        items += [(n.strip(), []) for n in a.dx.split(",") if n.strip()]
    syn = json.load(open(a.synonyms, encoding="utf-8")) if a.synonyms else {}
    syn = {k.lower(): v for k, v in syn.items()}
    seen, out = set(), []
    for n, s in items:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append((n, list(dict.fromkeys(s + syn.get(n.lower(), [])))))
    return out


# ═════════════════════════ main ═════════════════════════
def main():
    ap = argparse.ArgumentParser(description="진단명별 진단기준 원문 수집 (PubMed·Europe PMC·Unpaywall)")
    ap.add_argument("--report", help="파이프라인 리포트 HTML — Module 3 최종·기각 진단명을 입력으로 사용")
    ap.add_argument("--dx", help="쉼표로 구분한 진단명")
    ap.add_argument("--dx-file", help="한 줄에 하나: 진단명 | 동의어1 | 동의어2")
    ap.add_argument("--synonyms", help='동의어 JSON {"진단명": ["동의어", ...]}')
    ap.add_argument("--k", type=int, default=4, help="진단당 확보할 원문 수 (기본 4; v0.8.3: 3→4 — k=3이면 종류별 상한이 2편이라 같은 계열 지침 두 판본이 지침 자리를 다 차지할 수 있음)")
    ap.add_argument("--per-query", type=int, default=10, help="검색식별 후보 수 (기본 10)")
    ap.add_argument("--max-tries", type=int, default=10, help="진단당 원문 수집을 시도할 최대 후보 수 (기본 10)")
    ap.add_argument("--min-priority", type=int, default=3,
                    help="이 우선순위 점수 미만 후보는 원문을 받지 않음 (기본 3)")
    ap.add_argument("--no-weak-fallback", action="store_true",
                    help="기본 후보로 원문 0편일 때 '제목에 진단명이 있거나 리뷰인 1~2점 후보'를 약한 출처로 시도하지 않음")
    ap.add_argument("--out", default="dxcriteria_out", help="출력 폴더 (기본 dxcriteria_out)")
    ap.add_argument("--no-fetch", action="store_true", help="검색·정리만 하고 원문은 받지 않음")
    ap.add_argument("--refresh", action="store_true", help="이미 받은 원문도 다시 받음")
    ap.add_argument("--no-suppl", action="store_true", help="부록(supplementary) 파일을 받지 않음")
    ap.add_argument("--no-figures", action="store_true",
                    help="진단 알고리즘·기준 그림(캡션 선별, PMC Cloud의 CC 문헌만)을 받지 않음")
    ap.add_argument("--no-trace", action="store_true", help="기준 논문 추적(확보 원문이 기준 문장에서 인용한 참고문헌 받기)을 끔 (v0.8.6)")
    ap.add_argument("--trace-max", type=int, default=3, help="진단당 추적으로 추가 확보할 기준 논문 최대 편수 (기본 3)")
    ap.add_argument("--cache-dir", default="dxcriteria_cache",
                    help="실행 간 공유 원문 캐시 (기본 dxcriteria_cache, 빈 문자열이면 끔)")
    a = ap.parse_args()

    items = load_inputs(a)
    if not items:
        sys.exit("진단명이 없습니다. --report, --dx, --dx-file 중 하나를 지정하세요.")
    os.makedirs(a.out, exist_ok=True)
    if not a.no_fetch and not shutil.which("pdftotext"):
        try:
            import pypdf  # noqa: F401
        except ImportError:
            print("[!] pdftotext·pypdf가 없어 PDF는 텍스트 변환 없이 저장만 합니다.", file=sys.stderr)
    log = lambda s: print(s, file=sys.stderr)
    log(f"진단 {len(items)}건 · 진단당 원문 {a.k}편 목표 · 검색식별 {a.per_query}편 · 출력 {a.out}/\n")

    results = []
    for i, (name, syn) in enumerate(items, 1):
        log(f"{i:2d} {name}" + (f"  (동의어: {', '.join(syn)})" if syn else ""))
        r = retrieve(name, syn, a.k, a.per_query, a.max_tries, a.out, f"{i:02d}_{slug(name)}",
                     fetch=not a.no_fetch, refresh=a.refresh, log=log, min_priority=a.min_priority,
                     cache_dir=a.cache_dir or None, supplements=not a.no_suppl, figures=not a.no_figures,
                     weak_fallback=not a.no_weak_fallback, trace=not a.no_trace, trace_max=a.trace_max)
        r["no"] = i
        results.append(r)
        log(f"  → {r['status']} · 원문 {r['n_documents']}편"
            + (f" (약한 출처 {r['n_weak_documents']})" if r["n_weak_documents"] else "")
            + (f" (기준 논문 추적 {r['n_traced_documents']})" if r.get("n_traced_documents") else "")
            + (f" · 완전성 상위 {', '.join(r['recommended'])}" if r.get("recommended") else "")
            + ("  ⇒ 웹검색 필요" if r["needs_web_search"] else ""))
        with open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8") as f:   # 중간 저장
            json.dump(results, f, ensure_ascii=False, indent=1)

    head = ["#", "diagnosis", "synonyms", "mesh_concepts", "status", "needs_web_search", "web_search_reason",
            "n_candidates", "n_excluded", "n_documents", "n_weak_documents"]
    DOC_F = ("pmid", "doi", "title", "language", "doc_kind", "tier", "priority", "text_format", "table_score", "content_score", "completeness", "source", "license_code",
             "path", "n_suppl", "n_fig")
    for j in range(1, a.k + 1):
        head += [f"doc{j}_{x}" for x in DOC_F]
    head += ["traced_docs", "recommended", "web_hint1_title", "web_hint1_link", "suppl_to_fetch"]
    with open(os.path.join(a.out, "manifest.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(head)
        for r in results:
            row = [r["no"], r["diagnosis"], "; ".join(r["synonyms"]), "; ".join(r["mesh_concepts"]), r["status"],
                   "Y" if r["needs_web_search"] else "", r.get("web_search_reason", ""),
                   r["n_candidates"], r["n_excluded"], r["n_documents"], r["n_weak_documents"]]
            for j in range(a.k):
                d = r["documents"][j] if j < len(r["documents"]) else {}
                row += [d.get(x, "") for x in DOC_F]
            h = r["web_search_hints"][0] if r["web_search_hints"] else {}
            row += ["; ".join(f"{d.get('pmid') or d['key']} {d.get('title', '')[:60]}" for d in r["documents"] if d["tier"] == "traced"),
                    "; ".join(r.get("recommended", [])), h.get("title", ""), h.get("link", ""),
                    "; ".join(f"{x['key']}:{x['file']} {x['links'][0]}" for x in r.get("suppl_to_fetch", []))]
            w.writerow(row)

    st = lambda s: sum(r["status"] == s for r in results)
    docs = [d for r in results for d in r["documents"]]
    by = {}
    for d in docs:
        s = d["source"].split("_")[0] if d["source"].startswith("unpaywall") else d["source"]
        by[s] = by.get(s, 0) + 1
    log(f"\n원문 확보 {st('fulltext_found')}/{len(results)}건 · 후보만 {st('candidates_only')}건 · "
        f"후보 없음 {st('no_candidates')}건 → 웹검색 필요 {sum(r['needs_web_search'] for r in results)}건")
    n_weak = sum(d["tier"] == "weak" for d in docs)
    n_tr = sum(d["tier"] == "traced" for d in docs)
    n_sup = sum(d.get("n_suppl", 0) for d in docs)
    n_fig = sum(d.get("n_fig", 0) for d in docs)
    log(f"원문 {len(docs)}편 (" + ", ".join(f"{k} {v}" for k, v in by.items()) + ")"
        + (f" · 그중 약한 출처 {n_weak}편" if n_weak else "") + (f" · 기준 논문 추적 {n_tr}편" if n_tr else "")
        + (f" · 부록 텍스트 {n_sup}개" if n_sup else "") + (f" · 진단 그림 {n_fig}개" if n_fig else "")
        if docs else "원문 0편")
    log(f"저장: {a.out}/manifest.json, {a.out}/manifest.csv, {a.out}/NN_진단명/")


if __name__ == "__main__":
    main()
