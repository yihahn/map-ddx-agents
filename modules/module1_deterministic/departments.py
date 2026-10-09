from pathlib import Path

# Input: none. Output: this hospital's department roster as an ordered {English name: code} mapping
# (DEPARTMENTS), and the roster rendered for a prompt (as_prompt_list). Algorithm: the codes are
# the hospital's own and the English names are the standard rendering of each Korean department
# name, which is kept as a trailing comment so the two can be checked against each other. The file
# exists because recruit_specialists used to invent specialty names from the vignette alone and
# named services this hospital does not run — "Clinical Toxicology" was convened for PT09 — so a
# conference was assembled from departments no one could actually be called from. Picking from this
# roster makes every recruited department one that exists here, and the code travels with the name
# so a downstream reader can match it to the hospital's own records.

DEPARTMENTS: dict[str, str] = {
    "Hematology": "HEM",                                    # 혈액내과
    "Otorhinolaryngology": "ENT",                           # 이비인후과
    "Emergency Medicine": "EM",                             # 응급의학과
    "Medical Genetics Center": "MG",                        # 의학유전학센터
    "Radiology": "DR",                                      # 영상의학과
    "Obstetrics and Gynecology": "OBY",                     # 산부인과
    "Infectious Diseases": "INF",                           # 감염내과
    "Pulmonology": "PLM",                                   # 호흡기내과
    "Ophthalmology": "OPH",                                 # 안과
    "Colorectal Surgery": "CRS",                            # 대장항문외과
    "Dermatology": "DER",                                   # 피부과
    "Endocrinology": "END",                                 # 내분비내과
    "Neurology": "NR",                                      # 신경과
    "Nephrology": "NPH",                                    # 신장내과
    "Vascular Surgery": "VAS",                              # 혈관외과
    "General Surgery": "GS",                                # 일반외과
    "Kidney and Pancreas Transplant Surgery": "KT",         # 신.췌장이식외과
    "Urology": "URO",                                       # 비뇨의학과
    "Cardiology": "CV",                                     # 심장내과
    "Allergy": "ALG",                                       # 알레르기내과
    "Plastic Surgery": "PS",                                # 성형외과
    "Medical Oncology": "ONC",                              # 종양내과
    "Gastroenterology": "GI",                               # 소화기내과
    "Cardiovascular and Thoracic Surgery": "CS",            # 심장혈관흉부외과
    "Orthopedic Surgery": "OS",                             # 정형외과
    "Rehabilitation Medicine": "RM",                        # 재활의학과
    "Rheumatology": "RHE",                                  # 류마티스내과
    "Neurosurgery": "NS",                                   # 신경외과
    "Psychiatry": "PSY",                                    # 정신건강의학과
    "Critical Care and Trauma Surgery": "ACS",              # 중환자.외상외과
    "Hospitalist Internal Medicine": "MHU",                 # 통합내과
    "Pediatric and Adolescent Medicine": "PAM",             # 소아청소년전문과
    "Pediatric Cardiology": "PCD",                          # 소아청소년심장과
    "Pediatric Infectious Diseases": "PID",                 # 소아감염과
    "Pediatric Endocrinology and Metabolism": "PEM",        # 소아내분비대사과
    "Pediatric Hematology-Oncology": "PHO",                 # 소아청소년종양혈액과
    "Pediatric Nephrology": "NEP",                          # 소아신장과
    "Pediatric Respiratory and Allergy Center": "CRA",      # 소아호흡기.알레르기센터
    "Pediatric Gastroenterology and Nutrition": "PGN",      # 소아소화기 영양과
    "Pediatric Emergency Medicine": "PEC",                  # 소아응급의학
    "Pediatric Critical Care": "PCC",                       # 소아중환자과
    "Pediatric Neurosurgery": "PNS",                        # 소아신경외과
    "Pediatric Psychiatry": "PPS",                          # 소아정신건강의학과
    "Pediatric Cardiac Surgery": "PCS",                     # 소아심장외과
    "Gastrointestinal Surgery": "ST",                       # 위장관외과
    "Liver Transplantation and Hepatobiliary Surgery": "LTS",# 간이식및간담도외과
    "Breast and Endocrine Surgery": "BE",                   # 유방내분비외과
    "Breast Surgery": "BR",                                 # 유방외과
    "Geriatric Internal Medicine": "GIM",                   # 노년내과
    "Hand Surgery": "HOS",                                  # 수부외과
    "Hepatobiliary and Pancreatic Surgery": "HBP",          # 간담도췌외과
    "Endocrine Surgery": "ES",                              # 내분비외과
}


def as_prompt_list() -> str:
    """The roster as one `name (code)` per line, which is how recruit_specialists shows it."""
    return "\n".join(f"{name} ({code})" for name, code in DEPARTMENTS.items())


_LOOKUP = {name.lower(): name for name in DEPARTMENTS}
_LOOKUP.update({code.lower(): name for name, code in DEPARTMENTS.items()})
_LOOKUP.update({f"{name} ({code})".lower(): name for name, code in DEPARTMENTS.items()})


def resolve(text: str) -> str | None:
    """The roster name `text` refers to, or None when it names no department on the roster.

    Shown the roster as `Name (CODE)` lines and told to copy a name exactly, the model copied the
    whole line — every department of PT09's first conference came back as "Nephrology (NPH)" and
    was rejected, leaving the conference empty. Which half of that line is the name was obvious to
    whoever wrote the prompt and not to the model reading it, so the three forms it could
    reasonably return are all accepted here rather than ruled out by an instruction.
    """
    return _LOOKUP.get(" ".join(text.split()).lower())
