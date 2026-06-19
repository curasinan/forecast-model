from datetime import datetime

from pydantic import BaseModel, Field


class Prediction(BaseModel):
    """A single probability estimate from one predictor."""

    question_id: str
    predictor_name: str  # "bayesian", "classical_ml", "claude_llm", "ensemble"
    probability: float  # 0.0 to 1.0 (probability of YES)
    confidence: float = 0.5  # 0.0 to 1.0 (predictor's confidence in its own estimate)
    reasoning: str = ""  # Human-readable explanation
    created_at: datetime = Field(default_factory=datetime.now)
    evidence_count: int = 0


class EdgeSignal(BaseModel):
    """Quantifies the gap between our model and the market."""

    question_id: str
    model_prob: float  # Our model's probability
    market_prob: float  # Polymarket's implied probability (YES price)
    edge_pct: float  # model_prob - market_prob (positive = we think YES is underpriced)
    expected_value: float  # EV of buying YES at market price given our probability
    kelly_fraction: float  # Optimal fraction of bankroll to allocate
    confidence: float = 0.5
    vpin_flag: bool = False  # True if VPIN is elevated (informed trading detected)
    created_at: datetime = Field(default_factory=datetime.now)

    @property
    def has_edge(self) -> bool:
        return abs(self.edge_pct) > 0.05 and self.expected_value > 0

    @property
    def direction(self) -> str:
        if self.edge_pct > 0.05:
            return "YES underpriced"
        elif self.edge_pct < -0.05:
            return "NO underpriced"
        return "no edge"


class PredictionRecord(BaseModel):
    """Full prediction snapshot: all models on one question at one time."""

    question_id: str
    timestamp: datetime = Field(default_factory=datetime.now)
    predictions: list[Prediction] = []
    ensemble_probability: float | None = None
    edge: EdgeSignal | None = None
