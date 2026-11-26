from __future__ import annotations

import numpy as np
import pandas as pd


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def volcano(adj: pd.DataFrame, pair: pd.DataFrame, path, fdr):
    plt = _plt()
    fig, ax = plt.subplots(figsize=(7, 5))
    sets = [(adj, "adj_or", "drug", "single drug (adjusted OR)", "o")]
    if len(pair):
        sets.append((pair.assign(name=pair.drug_a + " + " + pair.drug_b), "interaction_or", "name",
                     "pair (interaction OR)", "^"))
    for t, col, lab, name, mk in sets:
        x, y = np.log2(t[col]), -np.log10(t.q.clip(lower=1e-300))
        ax.scatter(x, y, s=18, marker=mk, alpha=.7, label=name)
        for _, r in t[t.q < fdr].nlargest(8, col).iterrows():
            ax.annotate(r[lab], (np.log2(r[col]), -np.log10(max(r.q, 1e-300))), fontsize=6)
    ax.axhline(-np.log10(fdr), ls="--", c="grey", lw=.8)
    ax.axvline(0, c="k", lw=.6)
    ax.set(xlabel="log2 odds ratio", ylabel="-log10 q (BH)", title="AKI signals: anti-infectives and pairs")
    ax.legend(fontsize=7)
    ax.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def forest(t: pd.DataFrame, label: str, est: str, lo: str, hi: str, path, title, n=25):
    plt = _plt()
    t = t.dropna(subset=[est]).nsmallest(n, "q").iloc[::-1] if len(t) else t
    if not len(t):
        return
    fig, ax = plt.subplots(figsize=(7, 0.3 * len(t) + 1.2))
    yy = np.arange(len(t))
    ax.errorbar(t[est], yy, xerr=[t[est] - t[lo], t[hi] - t[est]], fmt="o", ms=4, capsize=2)
    ax.set_yticks(yy, t[label], fontsize=7)
    ax.axvline(1, c="k", lw=.7)
    ax.set_xscale("log")
    ax.set(xlabel="odds ratio (95% CI, log scale)", title=title)
    ax.grid(alpha=.3, axis="x")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def emar_vs_rx(m: pd.DataFrame, path):
    plt = _plt()
    if not len(m):
        return
    fig, ax = plt.subplots(figsize=(4.8, 4.6))
    ax.scatter(m.adj_or_rx, m.adj_or_emar, s=16)
    for _, r in m.iterrows():
        ax.annotate(r.drug, (r.adj_or_rx, r.adj_or_emar), fontsize=5)
    lim = [min(m.adj_or_rx.min(), m.adj_or_emar.min()) * .9, max(m.adj_or_rx.max(), m.adj_or_emar.max()) * 1.1]
    ax.plot(lim, lim, "k--", lw=.7)
    ax.set(xscale="log", yscale="log", xlabel="adjusted OR (prescriptions)", ylabel="adjusted OR (eMAR administrations)",
           title="Sensitivity: exposure source")
    ax.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
