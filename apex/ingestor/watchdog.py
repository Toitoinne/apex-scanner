"""Chien de garde du flux (cœur pur, testé).

- Source bloquée : websocket ouvert mais muet depuis `stall_s` → reconnexion forcée.
- Flux sain : au moins une source de logs a livré un événement pump.fun récemment, ET
  ce flux est cohérent avec PumpPortal (si PumpPortal annonce des créations alors que
  les logs n'en voient aucune, le flux de logs est considéré cassé).
- Panne : ouverture d'un TROU DE DONNÉES (pour ne jamais alerter ni apprendre sur des
  données incomplètes), bascule de secours sur Helius (plafonnée en crédits), alerte.
- Retour à la normale : fermeture du trou ; arrêt du secours Helius quand les sources
  gratuites sont saines depuis `release_after_s`.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

BACKUP = "helius"
CROSSCHECK = "pumpportal"


@dataclass
class Actions:
    reconnect: list[str] = field(default_factory=list)
    start_backup: bool = False
    stop_backup: bool = False
    gap_opened: float | None = None
    gap_closed: tuple[float, float] | None = None
    alert: str | None = None
    recovered: str | None = None


@dataclass
class FlowWatchdog:
    log_sources: list[str]
    stall_s: float = 30.0
    healthy_s: float = 20.0
    backup_after_s: float = 15.0
    release_after_s: float = 300.0
    outage_alert_s: float = 60.0
    crosscheck_window_s: float = 120.0
    crosscheck_min_creations: int = 5

    last_msg: dict[str, float] = field(default_factory=dict)
    last_event: dict[str, float] = field(default_factory=dict)
    connected_at: dict[str, float] = field(default_factory=dict)
    creations: dict[str, deque] = field(default_factory=dict)
    gap_start: float | None = None
    unhealthy_since: float | None = None
    free_healthy_since: float | None = None
    backup_running: bool = False
    alerted: bool = False

    # ---- observations ----
    def on_connect(self, src: str, now: float) -> None:
        self.connected_at[src] = now

    def on_disconnect(self, src: str) -> None:
        self.connected_at.pop(src, None)

    def on_message(self, src: str, now: float) -> None:
        self.last_msg[src] = now

    def on_event(self, src: str, now: float, is_creation: bool) -> None:
        self.last_event[src] = now
        if is_creation:
            self.creations.setdefault(src, deque()).append(now)

    # ---- évaluation ----
    def _n_creations(self, srcs: list[str], now: float) -> int:
        n = 0
        for s in srcs:
            dq = self.creations.get(s)
            if not dq:
                continue
            while dq and dq[0] < now - self.crosscheck_window_s:
                dq.popleft()
            n += len(dq)
        return n

    def _healthy(self, srcs: list[str], now: float) -> bool:
        alive = any(now - self.last_event.get(s, -1e18) <= self.healthy_s for s in srcs)
        if not alive:
            return False
        pp = self._n_creations([CROSSCHECK], now)
        if pp >= self.crosscheck_min_creations and self._n_creations(srcs, now) == 0:
            return False      # PumpPortal voit des lancements, les logs non : flux de logs cassé
        return True

    def evaluate(self, now: float, backup_allowed: bool) -> Actions:
        a = Actions()
        all_logs = self.log_sources + ([BACKUP] if self.backup_running else [])
        for s in all_logs:
            if s in self.connected_at:
                ref = max(self.last_msg.get(s, 0.0), self.connected_at[s])
                if now - ref > self.stall_s:
                    a.reconnect.append(s)
        free_ok = self._healthy(self.log_sources, now)
        ok = free_ok or (self.backup_running and self._healthy([BACKUP], now))
        if not ok:
            if self.unhealthy_since is None:
                self.unhealthy_since = now
                last = max([self.last_event.get(s, 0.0) for s in all_logs] or [0.0])
                self.gap_start = last if last > 0 else now
                a.gap_opened = self.gap_start
            down = now - self.gap_start  # durée réelle depuis le dernier événement reçu
            if not self.backup_running and backup_allowed and down >= self.backup_after_s:
                self.backup_running = True
                a.start_backup = True
            if not self.alerted and down >= self.outage_alert_s:
                self.alerted = True
                a.alert = (f"🔴 Flux pump.fun interrompu depuis {int(down)} s sur toutes les sources gratuites"
                           + (" — secours Helius activé." if self.backup_running else " — secours Helius indisponible (crédits)."))
        else:
            if self.gap_start is not None:
                a.gap_closed = (self.gap_start, now)
                if self.alerted:
                    a.recovered = f"✅ Flux rétabli après {int(now - self.gap_start)} s."
            self.gap_start = None
            self.unhealthy_since = None
            self.alerted = False
        if free_ok:
            self.free_healthy_since = self.free_healthy_since or now
        else:
            self.free_healthy_since = None
        if self.backup_running and (
            (self.free_healthy_since and now - self.free_healthy_since >= self.release_after_s) or not backup_allowed
        ):
            self.backup_running = False
            a.stop_backup = True
        return a


def overlaps_gap(gaps: list[tuple[float, float]], start: float, end: float, margin: float = 2.0,
                 min_len: float = 0.0) -> bool:
    """Vrai si l'intervalle [start, end] chevauche un trou de données plus long que `min_len`
    secondes (un trou encore ouvert compte comme infini)."""
    return any(ge - gs > min_len and gs - margin <= end and start <= ge + margin for gs, ge in gaps)
