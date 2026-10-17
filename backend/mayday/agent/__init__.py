"""Proofline agent layer. Depends only on mayday.core (protocols + bus)."""
from .evidence import EvidenceStore
from .gateway import Gateway, ModelError, OllamaGateway, make_gateway
from .orchestrator import Orchestrator
from .scripted import ScriptedGateway
from .verifier import Verifier

__all__ = ["Orchestrator", "ScriptedGateway", "OllamaGateway", "make_gateway", "Gateway", "ModelError",
           "EvidenceStore", "Verifier"]
