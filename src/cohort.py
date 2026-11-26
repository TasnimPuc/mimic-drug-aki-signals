"""Active-comparator cohort: every adult admission that received a systemic anti-infective.

t0 = first anti-infective start. Regimen = all anti-infectives started in [t0, t0 + 24 h].
Baseline creatinine = lowest value in the 7 days before t0 (KDIGO style). Outcome = AKI by KDIGO creatinine
criteria within 7 days: max creatinine >= 1.5 x baseline, or >= baseline + 0.3 mg/dL within 48 h.
Covariates: age, sex, admission type, ICU at t0, baseline creatinine, nephrotoxic co-medications around t0,
comorbidities (ICD-9/10), number of anti-infectives and number of creatinine tests after t0.
Exports two tables: one row per admission (covariates + outcome) and one row per admission x drug.
"""
from __future__ import annotations

# canonical name -> regex on the lower-cased drug / medication name (first match wins, order matters)
DRUGS = [
    ("piperacillin-tazobactam", r"piperacillin|zosyn"),
    ("ampicillin-sulbactam", r"ampicillin.?sulbactam|unasyn"),
    ("amoxicillin-clavulanate", r"amoxicillin.?clav|augmentin"),
    ("ceftazidime", r"ceftazidime"), ("ceftriaxone", r"ceftriaxone"), ("cefepime", r"cefepime"),
    ("cefazolin", r"cefazolin"), ("cefoxitin", r"cefoxitin"), ("cefuroxime", r"cefuroxime"),
    ("cephalexin", r"cephalexin|keflex"), ("ceftaroline", r"ceftaroline"), ("cefpodoxime", r"cefpodoxime"),
    ("meropenem", r"meropenem"), ("ertapenem", r"ertapenem"), ("imipenem", r"imipenem"), ("aztreonam", r"aztreonam"),
    ("vancomycin", r"vancomycin"), ("daptomycin", r"daptomycin"), ("linezolid", r"linezolid"),
    ("ampicillin", r"ampicillin"), ("amoxicillin", r"amoxicillin"), ("nafcillin", r"nafcillin"),
    ("oxacillin", r"oxacillin"), ("penicillin", r"penicillin"),
    ("levofloxacin", r"levofloxacin"), ("ciprofloxacin", r"ciprofloxacin"), ("moxifloxacin", r"moxifloxacin"),
    ("azithromycin", r"azithromycin"), ("doxycycline", r"doxycycline"), ("metronidazole", r"metronidazole|flagyl"),
    ("clindamycin", r"clindamycin"), ("gentamicin", r"gentamicin"), ("tobramycin", r"tobramycin"),
    ("amikacin", r"amikacin"), ("trimethoprim-sulfamethoxazole", r"sulfamethoxazole|bactrim|trimethoprim"),
    ("nitrofurantoin", r"nitrofurantoin"), ("rifampin", r"rifampin|rifampicin"), ("tigecycline", r"tigecycline"),
    ("colistin/polymyxin", r"colistin|polymyxin"), ("acyclovir", r"acyclovir"), ("ganciclovir", r"ganciclovir"),
    ("fluconazole", r"fluconazole"), ("voriconazole", r"voriconazole"), ("micafungin", r"micafungin"),
    ("amphotericin B", r"amphotericin"),
]
# co-medications (confounders) in [t0 - 24 h, t0 + 24 h]
COMEDS = {
    "nsaid": r"ibuprofen|ketorolac|naproxen|indomethacin|celecoxib|diclofenac|meloxicam",
    "loop_diuretic": r"furosemide|bumetanide|torsemide",
    "acei_arb": r"lisinopril|enalapril|captopril|ramipril|benazepril|quinapril|losartan|valsartan|irbesartan|olmesartan|candesartan|telmisartan",
    "vasopressor": r"norepinephrine|vasopressin|phenylephrine|dopamine|^epinephrine",
    "calcineurin": r"tacrolimus|cyclosporine",
    "ppi": r"pantoprazole|omeprazole|esomeprazole|lansoprazole",
    "iv_contrast": r"iohexol|iodixanol|iopamidol|ioversol|omnipaque|visipaque",
    "statin": r"atorvastatin|simvastatin|rosuvastatin|pravastatin",
}
# comorbidity -> (ICD-9 regex, ICD-10 regex)
DX = {
    "ckd": (r"^585", r"^N18"), "esrd": (r"^5856|^V451", r"^N186|^Z992"), "diabetes": (r"^250", r"^E(0[89]|1[0-3])"),
    "heart_failure": (r"^428", r"^I50"), "liver": (r"^57[01]", r"^K7[0-6]"), "hypertension": (r"^40[1-5]", r"^I1[0-6]"),
    "sepsis": (r"^038|^99591|^99592|^78552", r"^A4[01]|^R652"), "cancer": (r"^1[4-9][0-9]|^20[0-8]", r"^C"),
}
ROUTES = r"^(IV|PO|NG|OG|IM|NU|ORAL|G TUBE|J TUBE|PB)"
ORAL = r"^(PO|NG|OG|NU|ORAL|G TUBE|J TUBE)"
EXCL = r"lock|ophth|ointment|cream|gel|inhal|neb|otic|eye|desensit|topical|vaginal|irrigation|enema"
EMAR_EVENTS = ("Administered", "Started", "Delayed Administered", "Restarted", "Delayed Started",
               "Partial Administered", "Bolus from IV Drip", "Administered in Other Location")


def _drug_case(col: str, oral_cond: str | None) -> str:
    whens = []
    for name, rx in DRUGS:
        if name == "vancomycin" and oral_cond:
            whens.append(f"WHEN REGEXP_CONTAINS(LOWER({col}), r'vancomycin') AND {oral_cond} THEN 'vancomycin (oral)'")
        whens.append(f"WHEN REGEXP_CONTAINS(LOWER({col}), r'{rx}') THEN '{name}'")
    return "CASE " + " ".join(whens) + " END"


def _exposures(source: str) -> str:
    if source == "emar":
        ev = ", ".join(f"'{e}'" for e in EMAR_EVENTS)
        case = _drug_case("e.medication", "REGEXP_CONTAINS(LOWER(e.medication), r'oral|po ')")
        return f"""
  SELECT e.subject_id, e.hadm_id, e.charttime AS t, {case} AS drug
  FROM {{hosp}}.emar e
  WHERE e.hadm_id IS NOT NULL AND e.charttime IS NOT NULL AND e.event_txt IN ({ev})
    AND NOT REGEXP_CONTAINS(LOWER(e.medication), r'{EXCL}')"""
    case = _drug_case("p.drug", f"REGEXP_CONTAINS(UPPER(IFNULL(p.route, '')), r'{ORAL}')")
    return f"""
  SELECT p.subject_id, p.hadm_id, p.starttime AS t, {case} AS drug
  FROM {{hosp}}.prescriptions p
  WHERE p.hadm_id IS NOT NULL AND p.starttime IS NOT NULL
    AND REGEXP_CONTAINS(UPPER(IFNULL(p.route, '')), r'{ROUTES}')
    AND NOT REGEXP_CONTAINS(LOWER(p.drug), r'{EXCL}')"""


def _base(cfg, source: str) -> str:
    """Shared CTEs: ex (exposures), idx (t0 per admission), expo (regimen)."""
    return f"""
WITH ex AS (
  SELECT * FROM ({_exposures(source)}
  ) WHERE drug IS NOT NULL
), idx AS (
  SELECT hadm_id, MIN(subject_id) AS subject_id, MIN(t) AS t0 FROM ex GROUP BY hadm_id
), expo AS (
  SELECT DISTINCT ex.hadm_id, ex.drug
  FROM ex JOIN idx USING (hadm_id)
  WHERE ex.t <= DATETIME_ADD(idx.t0, INTERVAL {cfg.exposure_window_h} HOUR)
)"""


def exposures_sql(cfg, source: str) -> str:
    return _base(cfg, source) + "\nSELECT hadm_id, drug FROM expo"


def cohort_sql(cfg, source: str) -> str:
    comeds = ",\n    ".join(f"MAX(IF(REGEXP_CONTAINS(LOWER(p.drug), r'{rx}'), 1, 0)) AS med_{k}" for k, rx in COMEDS.items())
    dx = ",\n    ".join(
        f"MAX(IF((icd_version = 9 AND REGEXP_CONTAINS(icd_code, r'{a}')) OR (icd_version = 10 AND REGEXP_CONTAINS(icd_code, r'{b}')), 1, 0)) AS dx_{k}"
        for k, (a, b) in DX.items())
    w = cfg.exposure_window_h
    return _base(cfg, source) + f"""
, cr AS (
  SELECT subject_id, charttime, valuenum FROM {{hosp}}.labevents
  WHERE itemid = 50912 AND valuenum BETWEEN 0.1 AND 25
), base AS (
  SELECT idx.hadm_id, MIN(cr.valuenum) AS cr_base, COUNT(*) AS n_cr_base
  FROM idx JOIN cr ON cr.subject_id = idx.subject_id
   AND cr.charttime >= DATETIME_SUB(idx.t0, INTERVAL {cfg.baseline_days} DAY) AND cr.charttime <= idx.t0
  GROUP BY 1
), post AS (
  SELECT idx.hadm_id, MAX(cr.valuenum) AS cr_max7,
    MAX(IF(cr.charttime <= DATETIME_ADD(idx.t0, INTERVAL 48 HOUR), cr.valuenum, NULL)) AS cr_max48,
    COUNT(*) AS n_cr_post
  FROM idx JOIN cr ON cr.subject_id = idx.subject_id
   AND cr.charttime > idx.t0 AND cr.charttime <= DATETIME_ADD(idx.t0, INTERVAL {cfg.outcome_days} DAY)
  GROUP BY 1
), comed AS (
  SELECT idx.hadm_id,
    {comeds}
  FROM idx JOIN {{hosp}}.prescriptions p ON p.hadm_id = idx.hadm_id
   AND p.starttime >= DATETIME_SUB(idx.t0, INTERVAL 24 HOUR) AND p.starttime <= DATETIME_ADD(idx.t0, INTERVAL {w} HOUR)
  GROUP BY 1
), dx AS (
  SELECT hadm_id,
    {dx}
  FROM {{hosp}}.diagnoses_icd WHERE hadm_id IN (SELECT hadm_id FROM idx)
  GROUP BY 1
), icu AS (
  SELECT idx.hadm_id, 1 AS in_icu
  FROM idx JOIN {{icu}}.icustays i ON i.hadm_id = idx.hadm_id
   AND idx.t0 >= DATETIME_SUB(i.intime, INTERVAL 6 HOUR) AND idx.t0 <= i.outtime
  GROUP BY 1
), nabx AS (
  SELECT hadm_id, COUNT(*) AS n_drugs FROM expo GROUP BY 1
)
SELECT idx.hadm_id, idx.subject_id,
  p.anchor_age + EXTRACT(YEAR FROM idx.t0) - p.anchor_year AS age,
  IF(p.gender = 'F', 1, 0) AS female,
  a.admission_type,
  DATETIME_DIFF(idx.t0, a.admittime, MINUTE) / 60.0 AS hours_admit_to_t0,
  IFNULL(icu.in_icu, 0) AS in_icu,
  base.cr_base, base.n_cr_base, post.cr_max7, post.cr_max48, post.n_cr_post,
  nabx.n_drugs,
  IF(post.cr_max7 >= 1.5 * base.cr_base OR post.cr_max48 >= base.cr_base + 0.3, 1, 0) AS aki,
  IF(post.cr_max7 >= 2.0 * base.cr_base, 1, 0) AS aki_stage2,
  {", ".join(f"IFNULL(comed.med_{k}, 0) AS med_{k}" for k in COMEDS)},
  {", ".join(f"IFNULL(dx.dx_{k}, 0) AS dx_{k}" for k in DX)}
FROM idx
JOIN {{hosp}}.patients p ON p.subject_id = idx.subject_id
JOIN {{hosp}}.admissions a ON a.hadm_id = idx.hadm_id
JOIN base ON base.hadm_id = idx.hadm_id
JOIN post ON post.hadm_id = idx.hadm_id
JOIN nabx ON nabx.hadm_id = idx.hadm_id
LEFT JOIN comed ON comed.hadm_id = idx.hadm_id
LEFT JOIN dx ON dx.hadm_id = idx.hadm_id
LEFT JOIN icu ON icu.hadm_id = idx.hadm_id
WHERE p.anchor_age + EXTRACT(YEAR FROM idx.t0) - p.anchor_year >= {cfg.min_age}
  AND base.cr_base < {cfg.max_baseline_creat}
  AND IFNULL(dx.dx_esrd, 0) = 0
"""
