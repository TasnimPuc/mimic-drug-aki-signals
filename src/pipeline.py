"""BigQuery (MIMIC-IV, all admissions with anti-infectives) -> KDIGO-creatinine AKI cohort -> crude + adjusted
single-drug signals -> drug-pair interactions -> penalised all-exposure model -> IPTW positive control ->
eMAR sensitivity -> report + .pth (bucket only)."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from . import cohort, penalized, plots, signals
from .bq import BigQuerySource, materialize, read
from .config import Config
from .metrics import md, suppress
from .trainer import history

log = logging.getLogger("aki")


def run(cfg: Config, source=None) -> dict:
    t0 = time.time()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)
    for noisy in ("google", "urllib3", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    out = Path(cfg.work_dir)
    for sub in ("figures", "model", "report", "tables"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    from .store import make_store
    store = make_store(cfg)
    src = source or BigQuerySource(cfg, store, cfg.datasets())
    device = torch.device(cfg.device or ("cuda" if torch.cuda.is_available() else "cpu"))
    log.info("device: %s | bucket: %s", device, store.uri())

    # %% Cohort from BigQuery | All adult admissions with a systemic anti-infective (full MIMIC-IV). Exposures, baseline creatinine, KDIGO outcome and confounders are computed in BigQuery and cached as Parquet in your bucket.
    # show: overview
    def load(source_name):
        c = read(materialize(src, store, f"aki_cohort_{source_name}", cohort.cohort_sql(cfg, source_name), cfg.cache_dir))
        e = read(materialize(src, store, f"aki_expo_{source_name}", cohort.exposures_sql(cfg, source_name), cfg.cache_dir))
        return signals.design(c, e)
    df, covs, drugs = load(cfg.exposure_source)
    overview = pd.Series({"admissions": len(df), "patients": df.subject_id.nunique(), "AKI (KDIGO-Cr, 7 d)": f"{df.aki.mean():.1%}",
                          "AKI stage ≥2": f"{df.aki_stage2.mean():.1%}", "anti-infectives seen": len(drugs),
                          "covariates": len(covs)})
    fp = hashlib.sha1(json.dumps([len(df), int(df.aki.sum()), drugs, covs, cfg.seed]).encode()).hexdigest()[:8]
    key = lambda name: f"ckpt/{cfg.run_name}-{fp}/{name}"

    # %% Crude risks | AKI risk with vs without each anti-infective (no adjustment yet).
    # show: crude_t.head(20)
    crude_t = signals.crude(df, drugs)

    # %% Adjusted single-drug signals | One multivariable logistic model with all covariates and every drug given to ≥ `min_exposed` admissions; SEs clustered by patient; Benjamini–Hochberg FDR.
    # show: adj_t.head(20)
    adj_t, cov_t, fit0, model_drugs = signals.adjusted(df, covs, drugs, "aki", cfg.min_exposed, cfg.fdr)
    adj2, *_ = signals.adjusted(df, covs, drugs, "aki_stage2", cfg.min_exposed, cfg.fdr)

    # %% Drug-pair interactions (resumable) | For every pair co-prescribed ≥ `min_pair` times: base model + pair term. Interaction OR > 1 = worse than the two drugs' own effects combined. Finished pairs are stored in the bucket.
    # show: pair_t.head(20)
    plist = signals.pair_list(df, drugs, cfg.min_pair)
    pair_t = signals.pairs(df, covs, model_drugs, plist, fit0, store, key("pairs.json"), "aki", cfg.fdr,
                           cfg.ckpt_every_min)

    # %% Penalised all-exposure model (PyTorch, resumable) | All drugs and frequent pairs in one L2-shrunk logistic model; rare-exposure noise is pulled toward OR = 1.
    # show: history(st_pen, "neg_val_logloss")
    # show: pen_t.head(20)
    pen_model, st_pen, pen_t = penalized.train(df, covs, drugs, cfg, store, key("penalized"), device)

    # %% Positive control (IPTW) | Vancomycin + piperacillin-tazobactam vs vancomycin + cefepime — a known AKI signal. If the method finds it, the pipeline behaves as expected.
    # show: pd.Series(ctrl)
    ctrl, bal = signals.iptw_control(df, covs, cfg)

    # %% Sensitivity: eMAR exposures | Same adjusted model, but exposure = documented administration (eMAR, later years only) instead of prescription.
    # show: emar_cmp
    emar_cmp = pd.DataFrame()
    if cfg.run_emar_sensitivity and cfg.exposure_source != "emar":
        try:
            dfe, covse, drugse = load("emar")
            adj_e, *_ = signals.adjusted(dfe, covse, drugse, "aki", cfg.min_exposed, cfg.fdr)
            emar_cmp = adj_t[["drug", "adj_or"]].merge(adj_e[["drug", "adj_or", "n_exposed"]], on="drug",
                                                       suffixes=("_rx", "_emar"))
            plots.emar_vs_rx(emar_cmp, out / "figures" / "emar_vs_prescriptions.png")
        except Exception as e:                       # eMAR missing in this MIMIC version / too small
            log.warning("eMAR sensitivity skipped: %s", e)

    # %% Figures + tables | Volcano and forest plots; small cells (< 10 patients) removed from every published table.
    # show: pd.Series({"significant single drugs": int((adj_p.q < cfg.fdr).sum()), "significant pairs": int((pair_p.q < cfg.fdr).sum()) if len(pair_p) else 0})
    crude_p, adj_p, pair_p = signals.report_tables(crude_t, adj_t, pair_t)
    adj2_p = suppress(adj2, ("n_exposed", "aki_exposed"))
    for name, t in (("crude", crude_p), ("adjusted_single", adj_p), ("adjusted_single_stage2", adj2_p),
                    ("pair_interactions", pair_p), ("covariates", cov_t), ("penalized", suppress(pen_t, ("n_exposed",))),
                    ("iptw_balance", bal), ("emar_sensitivity", suppress(emar_cmp, ("n_exposed",)))):
        t.to_csv(out / "tables" / f"{name}.csv", index=False)
    plots.volcano(adj_p, pair_p, out / "figures" / "volcano.png", cfg.fdr)
    plots.forest(adj_p, "drug", "adj_or", "ci_low", "ci_high", out / "figures" / "forest_single.png",
                 "Adjusted OR of AKI — single anti-infectives")
    if len(pair_p):
        plots.forest(pair_p.assign(pair=pair_p.drug_a + " + " + pair_p.drug_b), "pair", "joint_or", "joint_ci_low",
                     "joint_ci_high", out / "figures" / "forest_pairs.png", "Joint OR of AKI — drug pairs vs neither")

    # %% Export + report | `.pth` goes to /kaggle/working and your private bucket only. The report has aggregate numbers only.
    # show: pd.Series(summary["models"])
    md_dir = out / "model"
    torch.save({"state_dict": pen_model.state_dict(), "exposures": pen_t.exposure.tolist(), "covariates": covs,
                "note": "covariates are standardised inside penalized.build"}, md_dir / "aki_signal_model.pth")
    summary = dict(admissions=len(df), patients=int(df.subject_id.nunique()), aki_rate=float(df.aki.mean()),
                   aki2_rate=float(df.aki_stage2.mean()), drugs_seen=len(drugs), drugs_in_model=len(model_drugs),
                   pairs_tested=len(plist), significant_single=int((adj_p.q < cfg.fdr).sum()),
                   significant_pairs=int((pair_p.q < cfg.fdr).sum()) if len(pair_p) else 0,
                   positive_control=ctrl, penalized_best_epoch=st_pen.best_epoch,
                   runtime_min=round((time.time() - t0) / 60, 1), config=cfg.to_dict(),
                   models={"aki_signal_model.pth": str(md_dir / "aki_signal_model.pth")})
    (out / "metrics.json").write_text(json.dumps(summary, indent=1, default=float))
    from .report import build as build_report
    build_report(out, dict(crude=crude_p, adj=adj_p, adj2=adj2_p, pair=pair_p, cov=cov_t,
                           pen=suppress(pen_t, ("n_exposed",)), bal=bal, emar=emar_cmp), summary)
    store.put_dir(out / "report", f"runs/{cfg.run_name}/report")
    store.put_dir(md_dir, f"runs/{cfg.run_name}/model")
    log.info("DONE in %.1f min", summary["runtime_min"])
    return summary
