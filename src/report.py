from __future__ import annotations

import shutil
from pathlib import Path

from .metrics import md


def build(out: Path, t: dict, s: dict) -> str:
    rep = out / "report"
    (rep / "figures").mkdir(parents=True, exist_ok=True)
    for f in (out / "figures").glob("*.png"):
        shutil.copy(f, rep / "figures" / f.name)
    for f in (out / "tables").glob("*.csv"):
        shutil.copy(f, rep / f.name)
    c = s["config"]
    pc = s["positive_control"]
    ctrl = "Not estimable (too few admissions in one arm)." if not pc else f"""
| | {c['control_drug']} + {c['control_a']} | {c['control_drug']} + {c['control_b']} |
|---|---|---|
| admissions | {pc['n_treated']:,} | {pc['n_comparator']:,} |
| crude AKI risk | {pc['crude_risk_treated']:.1%} | {pc['crude_risk_comparator']:.1%} |
| IPTW AKI risk | {pc['iptw_risk_treated']:.1%} | {pc['iptw_risk_comparator']:.1%} |

IPTW risk difference **{pc['risk_difference']:+.1%}** (95% CI {pc['rd_ci'][0]:+.1%} to {pc['rd_ci'][1]:+.1%}),
odds ratio **{pc['odds_ratio']:.2f}** ({pc['or_ci'][0]:.2f}–{pc['or_ci'][1]:.2f}); largest |SMD| after weighting
{pc['max_abs_smd_after']:.3f} (< 0.1 = balanced). Published studies report more AKI with piperacillin-tazobactam than with
cefepime when combined with vancomycin; finding the same direction here is a sanity check of the pipeline."""
    pair_cols = ["drug_a", "drug_b", "n_pair", "aki_pair", "interaction_or", "ci_low", "ci_high", "joint_or", "q", "signal"]
    text = f"""# Results — Drug-induced AKI signal detection (MIMIC-IV)

**Design:** active-comparator cohort. Every adult admission that received a systemic anti-infective is included, so
everyone is "being treated for an infection". t0 = first anti-infective; regimen = everything started within
{c['exposure_window_h']} h. **Outcome:** AKI by KDIGO creatinine criteria within {c['outcome_days']} days
(≥ 1.5 × baseline, or ≥ +0.3 mg/dL within 48 h); baseline = lowest creatinine in the {c['baseline_days']} days before t0.
Excluded: baseline creatinine ≥ {c['max_baseline_creat']} mg/dL and ESRD/dialysis codes.
**Exposure source:** `{c['exposure_source']}`.

{s['admissions']:,} admissions ({s['patients']:,} patients) · AKI {s['aki_rate']:.1%} · stage ≥ 2 {s['aki2_rate']:.1%} ·
{s['drugs_seen']} anti-infectives seen, {s['drugs_in_model']} with ≥ {c['min_exposed']} exposures in the adjusted
model · {s['pairs_tested']} pairs with ≥ {c['min_pair']} co-exposures tested.

## Positive control (IPTW)
{ctrl}

## Single anti-infectives — adjusted odds ratios
Logistic regression with every drug plus age, sex, admission type, ICU at t0, baseline creatinine, number of
creatinine tests before and after t0, number of anti-infectives, nephrotoxic co-medications (NSAID, loop diuretic,
ACEi/ARB, vasopressor, calcineurin inhibitor, PPI, IV contrast, statin) and comorbidities. SEs are clustered by
patient. q = Benjamini–Hochberg (FDR {c['fdr']}). **{s['significant_single']}** drugs signal at q < {c['fdr']}.

{md(t['adj'].drop(columns=['p']))}

![volcano](figures/volcano.png)
![forest single](figures/forest_single.png)

AKI stage ≥ 2 (more specific outcome):

{md(t['adj2'][['drug', 'n_exposed', 'adj_or', 'ci_low', 'ci_high', 'q', 'signal']])}

## Drug pairs — interaction
`interaction_or` > 1: the pair carries more risk than the two drugs' own effects multiplied. `joint_or` = the pair
vs neither drug. **{s['significant_pairs']}** pairs signal at q < {c['fdr']}.

{md(t['pair'][[x for x in pair_cols if x in t['pair']]].head(40)) if len(t['pair']) else '–'}

{'![forest pairs](figures/forest_pairs.png)' if len(t['pair']) else ''}

## Penalised all-exposure model
One L2-shrunk model with every drug and pair (≥ {c['pen_min_exposed']} exposures). Pair rows are interaction terms.

{md(t['pen'].head(30))}

## Sensitivity: eMAR administrations instead of prescriptions
{md(t['emar']) if len(t['emar']) else 'Not run or not available.'}

{'![emar](figures/emar_vs_prescriptions.png)' if len(t['emar']) else ''}

## Crude risks
{md(t['crude'].head(40))}

---
Signals are **hypotheses**, not proof of causation: residual confounding by indication and severity remains, and
creatinine is only measured when clinicians order it. Runtime {s['runtime_min']} min. Aggregate results only; rows
with fewer than 10 patients are removed; no patient-level data or model weights in this repo.
"""
    (rep / "REPORT.md").write_text(text, encoding="utf-8")
    return text
