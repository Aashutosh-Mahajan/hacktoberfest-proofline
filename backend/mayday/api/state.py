"""Bus subscriber that tracks current UI-relevant state for snapshots."""
from __future__ import annotations

import threading
from collections import deque

from mayday.core.bus import CONTRACT_VERSION


class StateTracker:
    def __init__(self, bus):
        self.lock = threading.Lock()
        self.sim: dict = {"scenario": None, "running": False, "sim_ms": 0, "speed": 1.0,
                          "fault_active": False, "chaos_hidden": False}
        self.agent_state = "idle"
        self.incident = None
        self.hyps: dict = {}
        self.rca = None
        self.deploys: list = []
        self.scoreboard: list = []
        self.recent: deque = deque(maxlen=480)
        self.pending = None
        self.model = {"backend": "scripted", "name": "none", "healthy": False, "warm": False}
        bus.subscribe(self.on_event)

    def clear_run(self):
        with self.lock:
            self.agent_state, self.incident, self.rca, self.pending = "idle", None, None, None
            self.hyps = {}
            self.deploys = []
            self.recent.clear()

    def on_event(self, env: dict) -> None:
        t, d = env["type"], env["data"]
        with self.lock:
            if t == "sim.status":
                self.sim = d
            elif t == "metrics.tick":
                self.recent.append({"ts": env["ts"], "m": d})
            elif t == "deploy.event":
                if not any(x["id"] == d["id"] for x in self.deploys):
                    self.deploys.append(d)
            elif t in ("incident.opened", "incident.updated"):
                self.incident = d["incident"]
            elif t == "incident.closed" and self.incident:
                self.incident = dict(self.incident, end_ts=d["end_ts"])
            elif t == "agent.state":
                self.agent_state = d["state"]
            elif t == "hypothesis.update":
                self.hyps[d["hypothesis"]["id"]] = d["hypothesis"]
            elif t == "rca.ready":
                self.rca = d["rca"]
            elif t == "action.proposed":
                self.pending = {"incident_id": d["incident_id"], "action": d["action"],
                                "approval_token": d["approval_token"], "expires_ts": d["expires_ts"]}
            elif t in ("action.executed",):
                self.pending = None
            elif t == "chaos.revealed":
                self.scoreboard.append({"round": d["round"], "correct": d["correct"], "scenario": d["scenario"],
                                        "agent_cause": d["agent_cause"]})

    def snapshot(self) -> dict:
        with self.lock:
            s = {"contract_version": CONTRACT_VERSION, "sim": dict(self.sim), "agent_state": self.agent_state,
                 "recent_metrics": list(self.recent), "hypotheses": list(self.hyps.values()),
                 "deploys": list(self.deploys), "scoreboard": list(self.scoreboard), "model": dict(self.model)}
            if self.incident:
                s["incident"] = self.incident
            if self.rca:
                s["rca"] = self.rca
            if self.pending:
                s["pending_action"] = self.pending
            return s
