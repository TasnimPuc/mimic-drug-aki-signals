from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class Config:
    # --- data: MIMIC-IV on BigQuery (physionet-data), all admissions with a systemic anti-infective ---
    hosp_dataset: str = ""                # "" = auto-detect newest
    icu_dataset: str = ""
    exposure_source: str = "prescriptions"   # prescriptions (all years) | emar (administrations, later years only)
    run_emar_sensitivity: bool = True     # repeat the main model with eMAR-confirmed exposures
    exposure_window_h: int = 24           # drugs started within 24 h of the first anti-infective = the "regimen"
    baseline_days: int = 7                # baseline creatinine = lowest value in the 7 days before t0
    outcome_days: int = 7                 # AKI (KDIGO creatinine) within 7 days after t0
    min_age: int = 18
    max_baseline_creat: float = 4.0       # exclude probable advanced CKD / dialysis
    seed: int = 42

    # --- signal detection ---
    min_exposed: int = 200                # drugs reported in the adjusted model
    min_pair: int = 100                   # co-exposed pairs tested for interaction
    fdr: float = 0.05                     # Benjamini–Hochberg
    control_drug: str = "vancomycin"      # positive control: vancomycin + piperacillin-tazobactam
    control_a: str = "piperacillin-tazobactam"
    control_b: str = "cefepime"           #   vs vancomycin + cefepime (IPTW)
    n_boot: int = 200

    # --- large-scale penalised regression (PyTorch, resumable) ---
    pen_min_exposed: int = 50
    pen_l2: float = 1e-4                  # L2 on drug/pair coefficients (shrinks rare-exposure noise)
    pen_epochs: int = 40
    pen_batch: int = 4096
    pen_lr: float = 5e-3
    pen_patience: int = 5

    # --- outputs + checkpoints (private GCS bucket, auto-created) ---
    work_dir: str = "/kaggle/working/aki"
    cache_dir: str = "/tmp/aki_cache"
    project_id: str = ""
    bucket: str = ""
    bucket_location: str = "US"
    gcs_prefix: str = "mimic-drug-aki-signals"
    run_name: str = "main"
    local_store: str = ""
    ckpt_every_min: float = 5.0
    device: str = ""

    def datasets(self) -> dict:
        return {"hosp": (self.hosp_dataset, ["mimiciv_3_1_hosp", "mimiciv_hosp", "mimiciv_2_2_hosp"], "prescriptions"),
                "icu": (self.icu_dataset, ["mimiciv_3_1_icu", "mimiciv_icu", "mimiciv_2_2_icu"], "icustays")}

    def to_dict(self):
        return asdict(self)
