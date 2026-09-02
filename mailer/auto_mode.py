"""Auto-Mode für Kampagnen — Watchdog + Multi-Armed Bandit.

Zwei kooperierende Komponenten die die Sende-Loop selbstständig
optimieren:

  Watchdog:  Sliding-Window über die letzten N Sends. Klassifiziert
             live die Failure-Rate pro Bucket (hard_bounce, spam_reject,
             auth). Trippt wenn Schwellwerte überschritten werden →
             Campaign soll pausieren (Notfall-Stop).

  Bandit:    Epsilon-Greedy Multi-Armed Bandit für Template-Wahl.
             Statt random.choice() über die HTMLs wählt der Bandit
             gewichtet basierend auf (successes / total) pro Template,
             mit gelegentlicher Exploration.

Beide sind threadsafe (RLock intern), state-in-memory, optional als
JSON-Blob serialisierbar für DB-Persistierung.
"""
from __future__ import annotations
import json
import random
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


# ── Watchdog ────────────────────────────────────────────────

# Failure-Buckets die den Watchdog interessieren. Kommen aus dem
# _classify_error in campaigns.py — hier duplizieren wir das nicht,
# der Caller übergibt einfach den etype als String.
CRITICAL_TYPES = {"spam_reject", "smtp_blocked", "smtp_suspended", "auth_fail"}
HARD_TYPES = {"mailbox_not_found", "permanent_reject"}


@dataclass
class WatchdogVerdict:
    action: str          # "ok" | "pause"
    reason: str = ""
    stats: dict = field(default_factory=dict)


class Watchdog:
    """Sliding-Window Health-Check für aktive Kampagnen.

    Trippt wenn:
      * hard-bounce-rate (mailbox_not_found + permanent_reject) über
        hard_bounce_pct
      * spam-reject-rate (spam_reject + smtp_blocked) über spam_reject_pct
      * auth_fail-rate über 20% (SMTP-Pool tot)
    Alle Schwellwerte werden nur ab min_samples ausgewertet damit die
    ersten paar zufälligen Bounces nicht sofort pausieren.
    """

    def __init__(self, window_size: int = 200,
                 hard_bounce_pct: float = 5.0,
                 spam_reject_pct: float = 8.0,
                 auth_fail_pct: float = 20.0,
                 min_samples: int = 50):
        self._lock = threading.RLock()
        self._window: deque = deque(maxlen=window_size)
        self._hard_pct = hard_bounce_pct
        self._spam_pct = spam_reject_pct
        self._auth_pct = auth_fail_pct
        self._min = min_samples
        # Letzter Verdict für die UI
        self._last_verdict: Optional[WatchdogVerdict] = None
        # Kumulierte Zähler (nicht sliding — Gesamtcampaign)
        self._total_sends = 0
        self._total_fails = 0
        self._counter_by_type: dict = {}

    def record(self, success: bool, error_type: str = ""):
        """Ein Send-Event registrieren. error_type nur relevant wenn
        success=False."""
        with self._lock:
            self._total_sends += 1
            entry = ("ok", "") if success else ("fail", error_type or "other")
            self._window.append(entry)
            if not success:
                self._total_fails += 1
                self._counter_by_type[error_type or "other"] = \
                    self._counter_by_type.get(error_type or "other", 0) + 1

    def _pct(self, matcher_set) -> float:
        if not self._window:
            return 0.0
        hits = sum(1 for outcome, etype in self._window
                    if outcome == "fail" and etype in matcher_set)
        return 100.0 * hits / len(self._window)

    def check(self) -> WatchdogVerdict:
        """Sofort auswerten — vom Worker regelmäßig gerufen (z.B. alle
        25 Sends). Wenn kritisch → action="pause"."""
        with self._lock:
            if len(self._window) < self._min:
                v = WatchdogVerdict("ok", "warm-up",
                                     stats=self._stats_dict())
                self._last_verdict = v
                return v

            hard = self._pct(HARD_TYPES)
            spam = self._pct({"spam_reject", "smtp_blocked"})
            auth = self._pct({"auth_fail"})

            if auth > self._auth_pct:
                reason = (f"SMTP-Pool auth-fail rate {auth:.1f}% "
                          f"> {self._auth_pct}% — alle Accounts tot?")
                v = WatchdogVerdict("pause", reason, stats=self._stats_dict())
            elif spam > self._spam_pct:
                reason = (f"Spam-Reject rate {spam:.1f}% > {self._spam_pct}% "
                          f"in den letzten {len(self._window)} Sends — "
                          f"Content/Domain wird geblockt.")
                v = WatchdogVerdict("pause", reason, stats=self._stats_dict())
            elif hard > self._hard_pct:
                reason = (f"Hard-Bounce rate {hard:.1f}% > {self._hard_pct}% "
                          f"in den letzten {len(self._window)} Sends — "
                          f"Lead-Liste veraltet oder Content wird bounced.")
                v = WatchdogVerdict("pause", reason, stats=self._stats_dict())
            else:
                v = WatchdogVerdict("ok", "healthy",
                                     stats=self._stats_dict())
            self._last_verdict = v
            return v

    def _stats_dict(self) -> dict:
        window_n = len(self._window)
        return {
            "window_size": window_n,
            "window_hard_pct": round(self._pct(HARD_TYPES), 2),
            "window_spam_pct": round(self._pct({"spam_reject", "smtp_blocked"}), 2),
            "window_auth_pct": round(self._pct({"auth_fail"}), 2),
            "total_sends": self._total_sends,
            "total_fails": self._total_fails,
            "by_type": dict(self._counter_by_type),
        }

    @property
    def last_verdict(self) -> Optional[WatchdogVerdict]:
        return self._last_verdict


# ── Multi-Armed Bandit ──────────────────────────────────────

class Bandit:
    """Epsilon-Greedy Multi-Armed Bandit über beliebige Arms (z.B.
    Template-IDs oder Template-Indizes).

    Score pro Arm = (successes + prior_alpha) / (total + prior_alpha
                                                   + prior_beta).
    Mit Beta(alpha=1, beta=1) startet jeder Arm bei 50%.

    pick(): mit Wahrscheinlichkeit epsilon exploriert (random arm),
    sonst wählt greedy nach höchstem Score. Ties werden zufällig
    aufgelöst.
    """

    def __init__(self, epsilon: float = 0.15,
                 prior_alpha: float = 1.0, prior_beta: float = 1.0):
        self._lock = threading.RLock()
        self._epsilon = float(epsilon)
        self._alpha0 = float(prior_alpha)
        self._beta0 = float(prior_beta)
        # arm_id → {"succ": int, "fail": int, "last": timestamp}
        self._arms: dict = {}

    def ensure(self, arm_ids):
        """Neue Arms mit 0/0 initialisieren."""
        with self._lock:
            for a in arm_ids:
                key = str(a)
                if key not in self._arms:
                    self._arms[key] = {"succ": 0, "fail": 0, "last": 0.0}

    def record(self, arm_id, success: bool):
        key = str(arm_id)
        with self._lock:
            a = self._arms.setdefault(key, {"succ": 0, "fail": 0, "last": 0.0})
            if success:
                a["succ"] += 1
            else:
                a["fail"] += 1
            a["last"] = time.time()

    def score(self, arm_id) -> float:
        key = str(arm_id)
        a = self._arms.get(key, {"succ": 0, "fail": 0})
        total = a["succ"] + a["fail"]
        return (a["succ"] + self._alpha0) / (total + self._alpha0 + self._beta0)

    def pick(self, arm_ids):
        """Einen Arm auswählen. Aufrufer muss die Kandidatenliste liefern
        (nicht der Bandit selber — der Pool kann pro Send variieren).
        Rückgabe: original arm_id aus der Kandidatenliste (nicht der
        Stringkey), damit der Caller mit seinen Typen weiterarbeiten kann."""
        if not arm_ids:
            return None
        arm_list = list(arm_ids)
        with self._lock:
            self.ensure(arm_list)
            if random.random() < self._epsilon:
                return random.choice(arm_list)
            scored = [(self.score(a), a) for a in arm_list]
            max_score = max(s for s, _ in scored)
            best = [a for s, a in scored if s == max_score]
            return random.choice(best)

    def reset_arm(self, arm_id):
        """Score zurücksetzen — z.B. wenn ein Template regeneriert wurde."""
        with self._lock:
            self._arms.pop(str(arm_id), None)

    def snapshot(self) -> dict:
        """State für Persistierung / UI."""
        with self._lock:
            return {
                "epsilon": self._epsilon,
                "prior_alpha": self._alpha0,
                "prior_beta": self._beta0,
                "arms": {str(k): dict(v) for k, v in self._arms.items()},
            }

    def load(self, blob):
        """State aus snapshot() zurückladen. Toleriert auch Strings
        (JSON) und leere/kaputte Payloads (dann Reset)."""
        if isinstance(blob, str):
            try:
                blob = json.loads(blob) if blob.strip() else {}
            except Exception:
                blob = {}
        blob = blob or {}
        with self._lock:
            self._epsilon = float(blob.get("epsilon", self._epsilon))
            self._alpha0 = float(blob.get("prior_alpha", self._alpha0))
            self._beta0 = float(blob.get("prior_beta", self._beta0))
            arms = blob.get("arms") or {}
            self._arms = {k: {"succ": int(v.get("succ", 0)),
                                "fail": int(v.get("fail", 0)),
                                "last": float(v.get("last", 0.0))}
                            for k, v in arms.items() if isinstance(v, dict)}

    def scores_table(self, arm_ids=None) -> list:
        """Für die UI: liste aller Arms sortiert nach Score, mit Zählern."""
        with self._lock:
            keys = [str(a) for a in arm_ids] if arm_ids else list(self._arms.keys())
            self.ensure(keys)
            rows = []
            for k in keys:
                stats = self._arms[k]
                total = stats["succ"] + stats["fail"]
                rows.append({
                    "arm": k,
                    "sends": total,
                    "succ": stats["succ"],
                    "fail": stats["fail"],
                    "score": round(self.score(k), 4),
                })
            rows.sort(key=lambda r: r["score"], reverse=True)
            return rows


# ── Combined Auto-Mode Controller ───────────────────────────

class AutoModeController:
    """Bindet Watchdog + Bandit zusammen. Was der Worker anfassen muss:

      ctrl = AutoModeController(...)
      ctrl.bandit.ensure([1, 2, 3])      # Template-IDs
      arm  = ctrl.bandit.pick([1, 2, 3])
      ...send email using template `arm`...
      ctrl.report(arm, success=True/False, error_type="spam_reject")
      # Alle N sends:
      verdict = ctrl.check_watchdog()
      if verdict.action == "pause": break out of worker loop
    """

    def __init__(self, hard_bounce_pct: float = 5.0,
                 spam_reject_pct: float = 8.0,
                 auth_fail_pct: float = 20.0,
                 window_size: int = 200,
                 min_samples: int = 50,
                 bandit_epsilon: float = 0.15,
                 bandit_state: Optional[dict] = None):
        self.watchdog = Watchdog(window_size=window_size,
                                  hard_bounce_pct=hard_bounce_pct,
                                  spam_reject_pct=spam_reject_pct,
                                  auth_fail_pct=auth_fail_pct,
                                  min_samples=min_samples)
        self.bandit = Bandit(epsilon=bandit_epsilon)
        if bandit_state:
            self.bandit.load(bandit_state)

    def report(self, arm_id, success: bool, error_type: str = ""):
        self.bandit.record(arm_id, success)
        self.watchdog.record(success, error_type)

    def check_watchdog(self) -> WatchdogVerdict:
        return self.watchdog.check()

    def snapshot(self) -> dict:
        """Kompletter State für DB-Persistierung."""
        return {
            "bandit": self.bandit.snapshot(),
            "watchdog": {
                "last_verdict": (self.watchdog.last_verdict.__dict__
                                  if self.watchdog.last_verdict else None),
                "stats": self.watchdog._stats_dict(),
            },
        }
