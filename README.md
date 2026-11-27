# Drug-induced AKI Signal Detector — MIMIC-IV 💊🫘

![repo health](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/TasnimPuc/bd-rooftop-solar-calculator/main/.github/health/badge.json)

This is a **pharmacovigilance** study, not a prediction model. It asks which anti-infectives, and which
**combinations**, are followed by more **acute kidney injury** than expected once patient severity, kidney function
and other nephrotoxic drugs are accounted for. The analysis uses all MIMIC-IV admissions that received a systemic
anti-infective (BigQuery, full data).

## Design
- **Active-comparator cohort:** every adult admission with a systemic anti-infective. Everyone is being treated for an
  infection, which reduces confounding by indication.
- **Time zero:** first anti-infective start. **Regimen:** all anti-infectives started within 24 h (from
  `prescriptions`, systemic routes only; oral vancomycin is separate; ophthalmic, topical and lock solutions excluded).
- **Outcome:** AKI by KDIGO creatinine criteria within 7 days (≥ 1.5 × baseline, or ≥ +0.3 mg/dL within 48 h).
  Baseline is the lowest creatinine in the 7 days before t0. Stage ≥ 2 is reported as a stricter outcome.
- **Excluded:** age < 18, baseline creatinine ≥ 4 mg/dL, ESRD/dialysis codes, no creatinine before and after t0.
- **Confounders:** age, sex, admission type, ICU at t0, baseline creatinine, creatinine testing intensity, number of
  anti-infectives, co-medications around t0 (NSAID, loop diuretic, ACEi/ARB, vasopressor, calcineurin inhibitor, PPI,
  IV contrast, statin) and comorbidities (CKD, diabetes, heart failure, liver disease, hypertension, sepsis, cancer;
  ICD-9 and ICD-10).

## Analyses
1. **Crude risks** with vs without each drug.
2. **Adjusted single-drug signals:** one multivariable logistic model with all drugs (≥ 200 exposures) and all
   confounders, standard errors clustered by patient, Benjamini–Hochberg FDR.
3. **Drug-pair interactions:** for every pair co-prescribed ≥ 100 times, an interaction OR (is the pair worse than
   both drugs' own effects multiplied?) plus a joint OR vs neither drug. BH-FDR.
4. **Penalised all-exposure model** (PyTorch): every drug and frequent pair in one L2-shrunk model, so that rare
   exposures do not produce noisy "signals" (large-scale regression, Caster et al., 2010).
5. **Positive control (IPTW):** vancomycin + piperacillin-tazobactam vs vancomycin + cefepime, a known AKI signal.
   Stabilised, trimmed weights, covariate balance (SMD) and bootstrap CIs. If the pipeline recovers this effect, the
   design behaves as expected.
6. **Sensitivity:** exposure = documented administration (`emar`) instead of prescription.

## Run on Kaggle (~15–40 min, CPU is enough)
1. Import `notebooks/kaggle_run.ipynb`. Internet **On** (a GPU is optional).
2. Add-ons → **Google Cloud Services**: attach the account that has PhysioNet BigQuery access and tick **BigQuery** +
   **Cloud Storage**.
3. Set `project_id` in the Config cell, then **Run All**. At the end, unzip `results_for_github.zip` here as `results/`.

### Fast and resumable
- **BigQuery runs once.** The cohort and exposure tables are exported as Parquet into a private, auto-created bucket
  (`gs://<project_id>-mimic-ckpt/mimic-drug-aki-signals/`). Later sessions only download them.
- **Continues where it stopped.** Finished pair models are saved to the bucket while the analysis runs. The PyTorch
  model saves model, optimizer and *position inside the epoch*. Run All again in a new session to continue.

## Outputs
`results/REPORT.md` with tables and figures: positive control, adjusted ORs, stage ≥ 2 ORs, pair interactions,
penalised ranking, eMAR sensitivity, volcano and forest plots. `aki_signal_model.pth` is written to
`/kaggle/working/aki/model/` and your bucket. **It is not published here.**

## Local tests / CI
```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu && pip install -r requirements.txt
python -m pytest -q            # synthetic tables with a planted vanc + pip-tazo signal: real SQL on DuckDB,
                               # the pipeline must recover the signal, and a second session must resume
python tools/build_notebook.py # regenerate the Kaggle notebook after editing src/
```

## Data, privacy and licence
- **MIMIC-IV:** Johnson A. et al., *Scientific Data* 10, 1 (2023). This is credentialed PhysioNet data: you need your
  own credentialing, CITI training and a signed DUA.
- This repository contains **code and aggregate results only**. Every published row describes ≥ 10 patients. No data
  is sent to any online LLM API.
- Code: MIT licence.

**Limitation:** signals are hypotheses, not proof of causation. Residual confounding by indication and severity
remains, and creatinine is only measured when clinicians order it.
