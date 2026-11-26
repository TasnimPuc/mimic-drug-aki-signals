"""Project-specific texts for tools/build_notebook.py."""
ACCELERATOR = "none"            # statistics + a small PyTorch model: CPU is enough (GPU also works)
PROBES = ["mimiciv_3_1_hosp.prescriptions", "mimiciv_hosp.prescriptions", "mimiciv_3_1_icu.icustays",
          "mimiciv_icu.icustays"]

INTRO = """# 💊 Drug-induced AKI Signal Detector — MIMIC-IV (Kaggle)

**Pharmacovigilance, not prediction:** which anti-infectives, and which **combinations**, are followed by more acute
kidney injury (KDIGO creatinine) than expected after adjusting for patient severity and co-medications?

**Before Run All**
1. Accelerator **None (CPU)** is enough · Internet **On**.
2. Add-ons → **Google Cloud Services** → attach the Google account with PhysioNet BigQuery access → tick
   **BigQuery** and **Cloud Storage**.
3. Set `project_id` in the Config cell.

**Resume:** cohort tables (Parquet), finished pair models and the PyTorch model checkpoints are kept in a private bucket
(`gs://<project_id>-mimic-ckpt/mimic-drug-aki-signals/`). If the session stops, **Run All** again: BigQuery is skipped
and the analysis continues where it stopped.

Pipeline (~15–40 min): BigQuery cohort (all admissions with anti-infectives) → crude risks → adjusted single-drug ORs →
pair interactions (BH-FDR) → penalised all-exposure model → IPTW positive control (vancomycin + pip-tazo vs + cefepime)
→ eMAR sensitivity → report."""

CONFIG = """import sys
sys.path.insert(0, "/kaggle/working/repo")
from src.config import Config

cfg = Config(
    project_id="mimic-cxr-training",   # <-- your GCP project ID (billing + BigQuery); bucket is created automatically
    work_dir="/kaggle/working/aki",
    exposure_source="prescriptions",   # prescriptions (all years) | emar (administrations)
    run_emar_sensitivity=True,
    min_exposed=200, min_pair=100,     # minimum exposures for a drug / a pair to be tested
    fdr=0.05,
    run_name="main",                   # change to start a fresh set of checkpoints
)
cfg"""
