"""Large-scale regression (in the spirit of Caster et al., 2010): ONE logistic model with all covariates, every drug and
every frequent drug pair at once. L2 shrinkage on the exposure coefficients pulls estimates from rare exposures
toward 'no effect', so the ranking is less driven by noise. PyTorch + resumable trainer (bucket checkpoints)."""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F

from .trainer import Checkpointer, TensorBatches, fit


class SignalModel(nn.Module):
    def __init__(self, n_cov, n_exp):
        super().__init__()
        self.cov = nn.Linear(n_cov, 1)
        self.exp = nn.Linear(n_exp, 1, bias=False)
        nn.init.zeros_(self.exp.weight)

    def forward(self, xc, xe):
        return (self.cov(xc) + self.exp(xe)).squeeze(-1)


def build(df, covs, drugs, cfg):
    singles = [d for d in drugs if df[d].sum() >= cfg.pen_min_exposed]
    prs = []
    for a, b in itertools.combinations(singles, 2):
        m = (df[a] == 1) & (df[b] == 1)
        if m.sum() >= cfg.pen_min_exposed:
            prs.append((f"{a} + {b}", m.to_numpy()))
    E = np.column_stack([df[s].to_numpy() for s in singles] + [m for _, m in prs]).astype(np.float32) \
        if singles else np.zeros((len(df), 0), np.float32)
    names = singles + [n for n, _ in prs]
    kinds = ["drug"] * len(singles) + ["pair (interaction)"] * len(prs)
    C = df[covs].to_numpy(np.float32)
    C = (C - C.mean(0)) / (C.std(0) + 1e-6)
    return C, E, names, kinds


def train(df, covs, drugs, cfg, store, key, device):
    C, E, names, kinds = build(df, covs, drugs, cfg)
    y = df.aki.to_numpy(np.float32)
    u = (pd.util.hash_pandas_object(df.subject_id.astype(str), index=False).to_numpy() % 100) / 100
    tr, va = np.flatnonzero(u < 0.9), np.flatnonzero(u >= 0.9)
    torch.manual_seed(cfg.seed)
    model = SignalModel(C.shape[1], E.shape[1]).to(device)
    T = {"c": torch.tensor(C, device=device), "e": torch.tensor(E, device=device), "y": torch.tensor(y, device=device)}
    batches = TensorBatches({}, cfg.pen_batch, cfg.seed, device, index=tr, tensors=T)
    opt = torch.optim.Adam(model.parameters(), lr=cfg.pen_lr)
    vi = torch.as_tensor(va, device=device)

    def loss_fn(m, b):
        return F.binary_cross_entropy_with_logits(m(b["c"], b["e"]), b["y"]) + cfg.pen_l2 * m.exp.weight.pow(2).sum()

    def evaluate(m):
        ll = F.binary_cross_entropy_with_logits(m(T["c"][vi], T["e"][vi]), T["y"][vi]).item()
        return {"neg_val_logloss": -ll}
    model, st = fit(model, opt, epochs=cfg.pen_epochs, batches=batches, loss_fn=loss_fn, evaluate=evaluate,
                    ckpt=Checkpointer(store, key, f"{cfg.work_dir}/ckpt/penalized"), metric="neg_val_logloss",
                    patience=cfg.pen_patience, ckpt_every_min=cfg.ckpt_every_min, log_every=0)
    w = model.exp.weight.detach().cpu().numpy().ravel()
    tab = pd.DataFrame({"exposure": names, "kind": kinds, "n_exposed": E.sum(0).astype(int),
                        "shrunk_or": np.exp(w)}).sort_values("shrunk_or", ascending=False)
    return model, st, tab
