"""End-to-end on synthetic MIMIC-IV-shaped tables with a planted signal (vancomycin + piperacillin-tazobactam).
The real BigQuery SQL runs on DuckDB (sqlglot); the pipeline must find the planted signal."""
import duckdb
import numpy as np
import pandas as pd

from src import cohort, signals
from src.bq import DuckDBSource
from src.config import Config
from src.store import LocalStore

DRUGS = {"vancomycin": ("Vancomycin", "IV"), "piperacillin-tazobactam": ("Piperacillin-Tazobactam", "IV"),
         "cefepime": ("CefePIME", "IV"), "ceftriaxone": ("CeftriaXONE", "IV"), "metronidazole": ("MetroNIDAZOLE", "PO"),
         "levofloxacin": ("Levofloxacin", "PO")}


def _tables(n=3000, seed=0):
    r = np.random.default_rng(seed)
    T0 = pd.Timestamp("2150-03-01")
    hadm = np.arange(1, n + 1) + 20_000_000
    subj = np.arange(1, n + 1) + 10_000_000
    t0 = [T0 + pd.Timedelta(hours=float(h)) for h in r.uniform(2, 72, n)]
    rx, lab, emar, dx = [], [], [], []
    aki_true = np.zeros(n, bool)
    for i in range(n):
        vanc = r.random() < 0.5
        ptz = r.random() < 0.45
        cef = (not ptz) and r.random() < 0.5
        cro = r.random() < 0.3
        other = r.choice(["metronidazole", "levofloxacin"]) if r.random() < 0.4 else None
        sick = r.random() < 0.3
        given = [d for d, on in (("vancomycin", vanc), ("piperacillin-tazobactam", ptz), ("cefepime", cef),
                                 ("ceftriaxone", cro), (other, other is not None)) if on]
        if not given:
            given = ["ceftriaxone"]
        for j, d in enumerate(given):
            name, route = DRUGS[d]
            rx.append((subj[i], hadm[i], t0[i] + pd.Timedelta(hours=j * 3), name, route))
            emar.append((subj[i], hadm[i], t0[i] + pd.Timedelta(hours=j * 3 + 1), name, "Administered"))
        if r.random() < 0.2:
            rx.append((subj[i], hadm[i], t0[i] + pd.Timedelta(hours=2), "Furosemide", "IV"))
        rx.append((subj[i], hadm[i], t0[i] + pd.Timedelta(hours=1), "Vancomycin Ophthalmic", "OU"))    # excluded
        if i % 50 == 0:
            rx.append((subj[i], hadm[i], t0[i] + pd.Timedelta(hours=1), "Vancomycin Oral Liquid", "PO"))
        p = 0.08 + 0.10 * sick + (0.25 if (vanc and ptz) else 0.0)
        aki_true[i] = r.random() < p
        base = r.uniform(0.6, 1.4)
        lab.append((subj[i], t0[i] - pd.Timedelta(hours=float(r.uniform(2, 100))), base))
        for k in range(int(r.integers(1, 5))):
            v = base * (1.7 if (aki_true[i] and k == 1) else r.uniform(0.9, 1.1))
            lab.append((subj[i], t0[i] + pd.Timedelta(hours=float(12 + 24 * k)), v))
        if sick:
            dx.append((hadm[i], "A419", 10))
        if i % 97 == 0:
            dx.append((hadm[i], "N186", 10))                                       # ESRD -> excluded
    pres = pd.DataFrame(rx, columns=["subject_id", "hadm_id", "starttime", "drug", "route"])
    em = pd.DataFrame(emar, columns=["subject_id", "hadm_id", "charttime", "medication", "event_txt"])
    le = pd.DataFrame(lab, columns=["subject_id", "charttime", "valuenum"]).assign(itemid=50912)
    pts = pd.DataFrame({"subject_id": subj, "gender": r.choice(["F", "M"], n), "anchor_age": r.integers(18, 90, n),
                        "anchor_year": 2150})
    adm = pd.DataFrame({"hadm_id": hadm, "subject_id": subj, "admittime": T0,
                        "admission_type": r.choice(["EW EMER.", "URGENT", "ELECTIVE", "OBSERVATION ADMIT"], n)})
    dxi = pd.DataFrame(dx, columns=["hadm_id", "icd_code", "icd_version"])
    icu = pd.DataFrame({"hadm_id": hadm[::3], "intime": T0, "outtime": T0 + pd.Timedelta(days=5)})
    return {"hosp": dict(prescriptions=pres, emar=em, labevents=le, patients=pts, admissions=adm, diagnoses_icd=dxi),
            "icu": dict(icustays=icu)}


def _source(store):
    con = duckdb.connect()
    for schema, tabs in _tables().items():
        con.execute(f"CREATE SCHEMA {schema}")
        for name, df in tabs.items():
            con.register("tmp_df", df)
            con.execute(f"CREATE TABLE {schema}.{name} AS SELECT * FROM tmp_df")
            con.unregister("tmp_df")
    return DuckDBSource(con, {"hosp": "hosp", "icu": "icu"}, store)


def _cfg(tmp, work="o", **kw):
    base = dict(work_dir=str(tmp / work), cache_dir=str(tmp / f"cache_{work}"), local_store=str(tmp / "bucket"),
                min_exposed=100, min_pair=50, pen_min_exposed=30, pen_epochs=5, n_boot=20, device="cpu")
    base.update(kw)
    return Config(**base)


def test_cohort_sql(tmp_path):
    cfg = _cfg(tmp_path)
    src = _source(LocalStore(cfg.local_store, cfg.gcs_prefix))
    c = src.df(cohort.cohort_sql(cfg, "prescriptions"))
    e = src.df(cohort.exposures_sql(cfg, "prescriptions"))
    assert c.hadm_id.is_unique and len(c) == 3000 - len(range(0, 3000, 97))     # ESRD excluded
    assert set(e.drug) >= {"vancomycin", "piperacillin-tazobactam", "vancomycin (oral)"}
    assert not e.drug.str.contains("ophth", case=False).any()
    assert c.aki.between(0, 1).all() and 0.05 < c.aki.mean() < 0.5
    assert c.med_loop_diuretic.sum() > 300 and c.dx_sepsis.sum() > 500 and c.in_icu.sum() > 500
    em = src.df(cohort.exposures_sql(cfg, "emar"))
    assert set(em.drug) >= {"vancomycin", "cefepime"}


def test_pipeline_finds_planted_signal_and_resumes(tmp_path):
    from src.pipeline import run
    cfg = _cfg(tmp_path)
    s = run(cfg, source=_source(LocalStore(cfg.local_store, cfg.gcs_prefix)))
    pc = s["positive_control"]
    assert pc["odds_ratio"] > 1.5 and pc["rd_ci"][0] > 0                       # planted vanc + PTZ effect found
    pairs = pd.read_csv(tmp_path / "o" / "tables" / "pair_interactions.csv")
    hit = pairs[((pairs.drug_a == "piperacillin-tazobactam") & (pairs.drug_b == "vancomycin"))]
    assert len(hit) and hit.interaction_or.iloc[0] > 1.5
    assert (tmp_path / "o" / "report" / "REPORT.md").read_text().count("Positive control") == 1
    cfg2 = _cfg(tmp_path, "o2")                                                 # new session, same bucket
    s2 = run(cfg2, source=_source(LocalStore(cfg2.local_store, cfg2.gcs_prefix)))
    assert s2["significant_pairs"] == s["significant_pairs"]
    assert abs(s2["positive_control"]["odds_ratio"] - pc["odds_ratio"]) < 1e-9


def test_notebook_steps(tmp_path):
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "tools"))
    from nbsteps import exec_steps
    cfg = _cfg(tmp_path, run_emar_sensitivity=False)
    ns = exec_steps(root / "src" / "pipeline.py", cfg, source=_source(LocalStore(cfg.local_store, cfg.gcs_prefix)))
    assert ns["result"]["admissions"] > 2000
