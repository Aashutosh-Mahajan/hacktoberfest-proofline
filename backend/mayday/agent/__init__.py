"""Proofline agent layer (Gemma 4 via Ollama). Depends only on mayday.core (protocols + bus)."""
from .evidence import EvidenceStore
from .gateway import Gateway, ModelError, OllamaGateway, make_gateway
from .orchestrator import Orchestrator
from .verifier import Verifier

__all__ = ["Orchestrator", "OllamaGateway", "make_gateway", "Gateway", "ModelError", "EvidenceStore", "Verifier"]
