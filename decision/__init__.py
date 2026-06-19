"""Phase 4 decision engine — turns model predictions into risk-managed trade signals."""

from decision.edge_calculator import compute_edges
from decision.position_sizer import size_positions
from decision.risk_manager import apply_risk_limits
from decision.signal_generator import generate_signals

__all__ = [
    "compute_edges",
    "size_positions",
    "apply_risk_limits",
    "generate_signals",
]
