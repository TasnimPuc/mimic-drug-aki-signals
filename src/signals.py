"""Signal detection: crude rates, confounder-adjusted ORs (one multivariable model, patient-clustered SEs),
drug-pair interactions (resumable, BH-FDR), large-scale penalised regression (PyTorch, resumable),
and an IPTW positive control (vancomycin + piperacillin-tazobactam vs vancomycin + cefepime)."""
from __future__ import annotations

import itertools
import json
import logging
import time
import warnings

import numpy as np
import pandas as pd

from .metrics import suppress

log = logging.getLogger(__name__)
CONT = ["age", "hours_admit_to_t0", "cr_base", "n_cr_base", "n_cr_post", "n_drugs"]


def design(cohort: pd.DataFrame, expo: pd.DataFrame):
    """-> (df with covariates + one 0/1 column per drug, covariate names, all drug names)"""
    df = cohort.drop_duplicates("hadm_id").set_index("hadm_id")
    expo = expo[expo.hadm_id.isin(df.index)]
    wide = pd.crosstab(expo.hadm_id, expo.drug).clip(upper=1)
    drugs = sorted(wide.columns)
    df = df.join(wide, how="left")
    df[drugs] = df[drugs].fillna(0).astype(np.int8)
    x = pd.DataFrame(index=df.index)
    x["age_10y"] = (df.age.clip(18, 100) - 65) / 10
    x["female"] = df.female
    x["log_hours_admit_to_t0"] = np.log1p(df.hours_admit_to_t0.clip(0, 24 * 60))
    x["in_icu"] = df.in_icu
    x["log_cr_base"] = np.log(df.cr_base.clip(0.2, 10))
    x["log_n_cr_base"] = np.log1p(df.n_cr_base)
    x["log_n_cr_post"] = np.log1p(df.n_cr_post)            # surveillance intensity (more tests -> more AKI found)
    x["n_anti_infectives"] = df.n_drugs.clip(0, 10)
    for c in [c for c in df if c.startswith(("med_", "dx_")) and c != "dx_esrd"]:
        x[c] = df[c]
    adm = df.admission_type.fillna("unknown").str.upper()
    top = adm.value_counts().index[1:6]                       # most common type = reference
    for t in top:
        x[f"adm_{t.lower().replace(' ', '_')}"] = (adm == t).astype(int)
    x = x.astype(float)
    binary = [c for c in x if set(np.unique(x[c])) <= {0.0, 1.0}]
    rare = [c for c in binary if min(x[c].sum(), len(x) - x[c].sum()) < 10]     # (near-)constant -> singular model
    x = x.drop(columns=rare + [c for c in x if c not in binary and x[c].std() == 0])
    covs = list(x.columns)
    out = pd.concat([x, df[drugs], df[["subject_id", "aki", "aki_stage2"]]], axis=1).reset_index()
    return out, covs, drugs


def crude(df, drugs, outcome="aki") -> pd.DataFrame:
    rows = []
    for d in drugs:
        e = df[d] == 1
        rows.append(dict(drug=d, n_exposed=int(e.sum()), aki_exposed=int(df.loc[e, outcome].sum()),
                         risk_exposed=df.loc[e, outcome].mean(), risk_unexposed=df.loc[~e, outcome].mean()))
    t = pd.DataFrame(rows)
    t["crude_rr"] = t.risk_exposed / t.risk_unexposed
    return t.sort_values("n_exposed", ascending=False)


def _logit(y, X, groups):
    import statsmodels.api as sm
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return sm.Logit(y, X).fit(disp=0, method="newton", maxiter=100, cov_type="cluster",
                                      cov_kwds={"groups": groups})
        except Exception:
            return sm.Logit(y, X).fit(disp=0, method="lbfgs", maxiter=1000, cov_type="cluster",
                                      cov_kwds={"groups": groups})


def _bh(p):
    from statsmodels.stats.multitest import multipletests
    p = np.asarray(p, float)
    q = np.full(len(p), np.nan)
    ok = ~np.isnan(p)
    if ok.any():
        q[ok] = multipletests(p[ok], method="fdr_bh")[1]
    return q


def adjusted(df, covs, drugs, outcome="aki", min_exposed=200, fdr=0.05):
    """One multivariable logistic model: covariates + every drug with >= min_exposed exposures."""
    import statsmodels.api as sm
    keep = [d for d in drugs if df[d].sum() >= min_exposed and df[d].sum() < len(df)]
    ev = int(df[outcome].sum())
    if ev < 10 or ev > len(df) - 10 or not keep:            # nothing to estimate
        empty = pd.DataFrame(columns=["drug", "n_exposed", "aki_exposed", "adj_or", "ci_low", "ci_high", "p", "q", "signal"])
        return empty, pd.DataFrame(columns=["covariate", "adj_or", "p"]), None, keep
    X = sm.add_constant(df[covs + keep].astype(float), has_constant="add")
    fit = _logit(df[outcome].to_numpy(), X, df.subject_id.to_numpy())
    ci = fit.conf_int()
    t = pd.DataFrame({"drug": keep, "n_exposed": [int(df[d].sum()) for d in keep],
                      "aki_exposed": [int(df.loc[df[d] == 1, outcome].sum()) for d in keep],
                      "adj_or": np.exp(fit.params[keep].to_numpy()), "ci_low": np.exp(ci.loc[keep, 0].to_numpy()),
                      "ci_high": np.exp(np.clip(ci.loc[keep, 1].to_numpy(), -50, 50)), "p": fit.pvalues[keep].to_numpy()})
    t["q"] = _bh(t.p)
    t["signal"] = np.where((t.q < fdr) & (t.adj_or > 1), "↑ risk", np.where((t.q < fdr) & (t.adj_or < 1), "↓ risk", ""))
    cov = pd.DataFrame({"covariate": covs, "adj_or": np.exp(fit.params[covs].to_numpy()), "p": fit.pvalues[covs].to_numpy()})
    return t.sort_values("p"), cov, fit, keep


def pair_list(df, drugs, min_pair):
    out = []
    for a, b in itertools.combinations(drugs, 2):
        n = int(((df[a] == 1) & (df[b] == 1)).sum())
        if n >= min_pair:
            out.append((a, b, n))
    return out


def pairs(df, covs, model_drugs, plist, fit0, store, key, outcome="aki", fdr=0.05, every_min=5.0):
    """Interaction per pair: base model + a*b. Interaction OR > 1 = the pair is worse than the two drugs'
    separate effects multiplied. Finished pairs are saved to the bucket; a new session continues."""
    import statsmodels.api as sm
    done = json.loads(store.get_text(key) or "{}") if store is not None else {}
    if done:
        log.info("[pairs] RESUME: %d/%d pairs already fitted", len(done), len(plist))
    y, g = df[outcome].to_numpy(), df.subject_id.to_numpy()
    t_save = time.time()
    for i, (a, b, n) in enumerate(plist):
        k = f"{a}|{b}"
        if k in done:
            continue
        cols = covs + sorted(set(model_drugs) | {a, b})
        X = sm.add_constant(df[cols].astype(float), has_constant="add")
        X["pair"] = ((df[a] == 1) & (df[b] == 1)).astype(float)
        try:
            f = _logit(y, X, g)
            ci = f.conf_int().loc["pair"]
            idx = [X.columns.get_loc(c) for c in (a, b, "pair")]
            beta = f.params.iloc[idx].to_numpy()
            V = f.cov_params().iloc[idx, idx].to_numpy()
            se = float(np.sqrt(np.ones(3) @ V @ np.ones(3)))
            done[k] = dict(n_pair=n, aki_pair=int(df.loc[X.pair == 1, outcome].sum()),
                           interaction_or=float(np.exp(f.params["pair"])), ci_low=float(np.exp(ci[0])),
                           ci_high=float(np.exp(ci[1])), p=float(f.pvalues["pair"]),
                           joint_or=float(np.exp(beta.sum())), joint_ci_low=float(np.exp(beta.sum() - 1.96 * se)),
                           joint_ci_high=float(np.exp(beta.sum() + 1.96 * se)))
        except Exception as e:                               # e.g. separation
            done[k] = dict(n_pair=n, error=str(e)[:100])
        if store is not None and (time.time() - t_save > every_min * 60 or i == len(plist) - 1):
            store.put_text(json.dumps(done), key)
            t_save = time.time()
            log.info("[pairs] %d/%d fitted, saved", len(done), len(plist))
    if store is not None:
        store.put_text(json.dumps(done), key)
    t = pd.DataFrame([dict(drug_a=k.split("|")[0], drug_b=k.split("|")[1], **v) for k, v in done.items()])
    if not len(t) or "p" not in t:
        return pd.DataFrame()
    t = t[t.p.notna()].copy()
    t["q"] = _bh(t.p)
    t["signal"] = np.where((t.q < fdr) & (t.interaction_or > 1), "synergy ↑",
                           np.where((t.q < fdr) & (t.interaction_or < 1), "less than expected", ""))
    return t.sort_values("p").drop(columns=[c for c in ("error",) if c in t])


def iptw_control(df, covs, cfg):
    """Positive control: among vancomycin-treated admissions, + piperacillin-tazobactam vs + cefepime (not both)."""
    from sklearn.linear_model import LogisticRegression
    v, a, b = cfg.control_drug, cfg.control_a, cfg.control_b
    if not all(c in df for c in (v, a, b)):
        return {}, pd.DataFrame()
    sub = df[(df[v] == 1) & ((df[a] == 1) ^ (df[b] == 1))].reset_index(drop=True)
    T = sub[a].to_numpy()
    if T.sum() < 20 or (1 - T).sum() < 20:
        return {}, pd.DataFrame()
    X, y = sub[covs].to_numpy(), sub.aki.to_numpy()

    def est(idx):
        ps = LogisticRegression(C=1.0, max_iter=3000).fit(X[idx], T[idx]).predict_proba(X[idx])[:, 1].clip(.01, .99)
        t, yy = T[idx], y[idx]
        w = np.where(t == 1, t.mean() / ps, (1 - t.mean()) / (1 - ps))           # stabilised
        lo, hi = np.percentile(w, [1, 99])
        w = w.clip(lo, hi)
        r1 = np.average(yy[t == 1], weights=w[t == 1])
        r0 = np.average(yy[t == 0], weights=w[t == 0])
        return r1, r0, w, ps
    r1, r0, w, ps = est(np.arange(len(sub)))
    rng = np.random.default_rng(cfg.seed)
    boot = []
    for _ in range(cfg.n_boot):
        i = rng.integers(0, len(sub), len(sub))
        if 0 < T[i].sum() < len(i):
            b1, b0, *_ = est(i)
            boot.append((b1 - b0, (b1 / (1 - b1)) / (b0 / (1 - b0))))
    boot = np.array(boot) if boot else np.full((1, 2), np.nan)
    res = dict(comparison=f"{v} + {a}  vs  {v} + {b}", n_treated=int(T.sum()), n_comparator=int((1 - T).sum()),
               crude_risk_treated=float(y[T == 1].mean()), crude_risk_comparator=float(y[T == 0].mean()),
               iptw_risk_treated=float(r1), iptw_risk_comparator=float(r0), risk_difference=float(r1 - r0),
               rd_ci=[float(np.nanpercentile(boot[:, 0], 2.5)), float(np.nanpercentile(boot[:, 0], 97.5))],
               odds_ratio=float((r1 / (1 - r1)) / (r0 / (1 - r0))),
               or_ci=[float(np.nanpercentile(boot[:, 1], 2.5)), float(np.nanpercentile(boot[:, 1], 97.5))])

    def smd(x, t, ww=None):
        ww = np.ones(len(x)) if ww is None else ww
        m1, m0 = np.average(x[t == 1], weights=ww[t == 1]), np.average(x[t == 0], weights=ww[t == 0])
        v1 = np.average((x[t == 1] - m1) ** 2, weights=ww[t == 1])
        v0 = np.average((x[t == 0] - m0) ** 2, weights=ww[t == 0])
        return (m1 - m0) / np.sqrt((v1 + v0) / 2 + 1e-12)
    bal = pd.DataFrame({"covariate": covs, "smd_before": [smd(X[:, j], T) for j in range(len(covs))],
                        "smd_after_iptw": [smd(X[:, j], T, w) for j in range(len(covs))]})
    res["max_abs_smd_after"] = float(bal.smd_after_iptw.abs().max())
    return res, bal


def report_tables(crude_t, adj_t, pair_t):
    return (suppress(crude_t, ("n_exposed", "aki_exposed")), suppress(adj_t, ("n_exposed", "aki_exposed")),
            suppress(pair_t, ("n_pair", "aki_pair")) if len(pair_t) else pair_t)
