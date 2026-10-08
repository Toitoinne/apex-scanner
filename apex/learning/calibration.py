"""Calibration en ligne des probabilités : Platt (SGD) et isotonique sur fenêtre glissante."""
from __future__ import annotations

import math
from collections import deque

import numpy as np
from sklearn.isotonic import IsotonicRegression

EPS = 1e-6


def logit(p: float) -> float:
    p = min(1 - EPS, max(EPS, p))
    return math.log(p / (1 - p))


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    e = math.exp(z)
    return e / (1 + e)


class PlattOnline:
    def __init__(self, lr: float = 0.01):
        self.a, self.b, self.lr = 1.0, 0.0, lr
        self.n = 0

    def transform(self, p: float) -> float:
        return sigmoid(self.a * logit(p) + self.b)

    def update(self, p: float, y: int) -> None:
        z = logit(p)
        q = sigmoid(self.a * z + self.b)
        g = q - y
        self.a -= self.lr * g * z
        self.b -= self.lr * g
        self.n += 1


class WindowIsotonic:
    def __init__(self, window: int = 5000, refit_every: int = 250):
        self.buf: deque[tuple[float, int]] = deque(maxlen=window)
        self.refit_every = refit_every
        self.model: IsotonicRegression | None = None
        self._since = 0

    def transform(self, p: float) -> float:
        if self.model is None:
            return p
        return float(self.model.predict([p])[0])

    def update(self, p: float, y: int) -> None:
        self.buf.append((p, y))
        self._since += 1
        if self._since >= self.refit_every and len(self.buf) >= 200:
            arr = np.array(self.buf)
            self.model = IsotonicRegression(y_min=0.001, y_max=0.999, out_of_bounds="clip").fit(arr[:, 0], arr[:, 1])
            self._since = 0


class Calibrator:
    """Maintient les deux méthodes en parallèle (l'une sert d'« ombre » à l'autre)."""

    def __init__(self, method: str = "platt", iso_window: int = 5000):
        self.method = method
        self.platt = PlattOnline()
        self.iso = WindowIsotonic(iso_window)

    def transform(self, p: float, method: str | None = None) -> float:
        m = method or self.method
        return self.iso.transform(p) if m == "isotonic" else self.platt.transform(p)

    def update(self, p: float, y: int) -> None:
        self.platt.update(p, y)
        self.iso.update(p, y)


def expected_calibration_error(ps: list[float] | np.ndarray, ys: list[int] | np.ndarray, bins: int = 10) -> float:
    ps, ys = np.asarray(ps, dtype=float), np.asarray(ys, dtype=float)
    if len(ps) == 0:
        return 0.0
    idx = np.minimum((ps * bins).astype(int), bins - 1)
    ece = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            ece += m.mean() * abs(ps[m].mean() - ys[m].mean())
    return float(ece)


def reliability_table(ps, ys, bins: int = 10) -> list[dict]:
    ps, ys = np.asarray(ps, dtype=float), np.asarray(ys, dtype=float)
    idx = np.minimum((ps * bins).astype(int), bins - 1)
    out = []
    for b in range(bins):
        m = idx == b
        out.append({"bin": b / bins, "n": int(m.sum()),
                    "p_mean": float(ps[m].mean()) if m.any() else None,
                    "freq": float(ys[m].mean()) if m.any() else None})
    return out
