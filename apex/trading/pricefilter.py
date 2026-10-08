"""Filtre des prix externes (DexScreener) : un pool minuscule ou un relevé isolé ne doit
jamais produire un faux x1000 (qui fausserait labels, stratégies et bandit)."""
from __future__ import annotations


class PriceGuard:
    def __init__(self, max_jump: float = 5.0, confirm_tol: float = 0.25):
        self.max_jump = max_jump              # saut maximal accepté sans confirmation (×5 ou ÷5)
        self.confirm_tol = confirm_tol        # le relevé suivant doit être à ±25 % du relevé suspect
        self.last: dict[str, float] = {}
        self.suspect: dict[str, float] = {}

    def seed(self, mint: str, price: float) -> None:
        if price > 0 and mint not in self.last:
            self.last[mint] = price

    def accept(self, mint: str, price: float) -> bool:
        if price <= 0:
            return False
        ref = self.last.get(mint)
        if ref is None or (1 / self.max_jump) <= price / ref <= self.max_jump:
            self.last[mint] = price
            self.suspect.pop(mint, None)
            return True
        s = self.suspect.get(mint)
        if s is not None and abs(price / s - 1) <= self.confirm_tol:
            self.last[mint] = price           # saut confirmé par deux relevés consécutifs
            self.suspect.pop(mint, None)
            return True
        self.suspect[mint] = price
        return False
