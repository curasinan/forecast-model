"""Level 3 Predictor: Claude as a structured reasoning engine.

This is where Claude receives the full evidence bundle and outputs a
calibrated probability with chain-of-thought reasoning.

Key design choice: Claude gets structured input (evidence + features + market data)
and must return structured JSON output (probability + confidence + reasoning).
The system prompt emphasizes CALIBRATION — when Claude says 70%, events like
that should happen ~70% of the time.

Uses Anthropic's Python SDK with the Messages API.
"""

import json
import logging

import anthropic

from src.config import settings
from src.models.evidence import EvidenceBundle
from src.models.prediction import Prediction
from src.models.question import Question
from src.predictors.base import BasePredictor

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a calibrated prediction analyst for tech industry events.

Your job:
1. Analyze evidence FOR and AGAINST the event happening.
2. Consider base rates for similar events (e.g., how often do AI labs hit announced timelines?).
3. Weigh source credibility — Reuters matters more than a random blog.
4. Output a CALIBRATED probability (0.0 to 1.0) for YES.
5. Explain your reasoning step by step.

CALIBRATION RULES:
- When you say 70%, events like that should actually happen about 70% of the time.
- Avoid extremes (below 0.05 or above 0.95) unless evidence is overwhelming.
- If evidence is sparse or contradictory, stay closer to 0.50 (maximum uncertainty).
- The market price represents the crowd's current estimate. You should deviate from it
  only when you have specific evidence-based reasons to do so.

RESPONSE FORMAT (strict JSON):
{
    "probability": <float 0.0-1.0>,
    "confidence": <float 0.0-1.0>,
    "reasoning": "<your step-by-step analysis>",
    "evidence_for": ["<key point 1>", "<key point 2>"],
    "evidence_against": ["<key point 1>", "<key point 2>"],
    "base_rate_estimate": "<your estimate of how often similar events happen>"
}

Respond ONLY with valid JSON. No markdown, no code blocks, just the JSON object."""


class LLMPredictor(BasePredictor):
    """Claude-powered structured reasoning predictor."""

    name = "claude_llm"

    def __init__(self, model: str | None = None):
        self.model = model or settings.claude_model
        self._client: anthropic.Anthropic | None = None

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            if not settings.anthropic_api_key:
                raise ValueError(
                    "ANTHROPIC_API_KEY not set. Add it to your .env file. "
                    "Get a key at https://console.anthropic.com/"
                )
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._client

    def _format_evidence(self, bundle: EvidenceBundle, max_items: int = 15) -> str:
        """Format evidence bundle into a readable string for Claude."""
        if not bundle.items:
            return "No evidence collected."

        lines = []
        for i, item in enumerate(bundle.items[:max_items], 1):
            date_str = item.published_at.strftime("%Y-%m-%d") if item.published_at else "unknown"
            sentiment = f"sentiment={item.sentiment_score:+.2f}" if item.sentiment_score else ""
            lines.append(
                f"{i}. [{item.source_name}] ({date_str}) {sentiment}\n"
                f"   Title: {item.title}\n"
                f"   Snippet: {item.content_snippet[:200]}"
            )

        if bundle.item_count > max_items:
            lines.append(f"\n... and {bundle.item_count - max_items} more items")

        return "\n\n".join(lines)

    def _build_prompt(
        self,
        question: Question,
        bundle: EvidenceBundle,
        features: dict[str, float],
    ) -> str:
        """Build the user prompt with all context."""
        evidence_text = self._format_evidence(bundle)

        # Format key features
        feature_summary = "\n".join(
            f"  - {k}: {v:.3f}" for k, v in sorted(features.items())
        )

        market_price = features.get("market_yes_price", 0.5)

        return f"""QUESTION: {question.title}

DESCRIPTION: {question.description}

DEADLINE: {question.deadline.strftime("%Y-%m-%d")}
DAYS REMAINING: {features.get("days_to_deadline", -1):.0f}

CURRENT MARKET PRICE (crowd estimate): YES=${market_price:.2f} ({market_price:.0%})

EVIDENCE ({bundle.item_count} items collected):
{evidence_text}

FEATURE SUMMARY:
{feature_summary}

Analyze this evidence and provide your calibrated probability estimate for YES.
Remember: only deviate from the market price if you have specific evidence-based reasons."""

    def _parse_response(self, text: str) -> dict:
        """Parse Claude's JSON response, handling common edge cases."""
        # Strip markdown code blocks if Claude wraps the response
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        if text.startswith("json"):
            text = text[4:].strip()

        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse Claude response as JSON: {text[:200]}")
            return {
                "probability": 0.5,
                "confidence": 0.2,
                "reasoning": f"Failed to parse response. Raw: {text[:500]}",
                "evidence_for": [],
                "evidence_against": [],
            }

        # Validate and clamp probability
        prob = float(result.get("probability", 0.5))
        prob = max(0.05, min(0.95, prob))
        result["probability"] = prob

        conf = float(result.get("confidence", 0.5))
        result["confidence"] = max(0.1, min(0.9, conf))

        return result

    async def predict(
        self,
        question: Question,
        bundle: EvidenceBundle,
        features: dict[str, float],
    ) -> Prediction:
        # Check if API key is available
        if not settings.anthropic_api_key:
            return Prediction(
                question_id=question.id,
                predictor_name=self.name,
                probability=0.5,
                confidence=0.0,
                reasoning="ANTHROPIC_API_KEY not set. Skipping LLM prediction.",
                evidence_count=bundle.item_count,
            )

        user_prompt = self._build_prompt(question, bundle, features)

        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )

            response_text = message.content[0].text
            result = self._parse_response(response_text)

            # Build readable reasoning
            reasoning_parts = [result.get("reasoning", "")]
            evidence_for = result.get("evidence_for", [])
            evidence_against = result.get("evidence_against", [])

            if evidence_for:
                reasoning_parts.append("\nEvidence FOR:")
                for e in evidence_for:
                    reasoning_parts.append(f"  + {e}")
            if evidence_against:
                reasoning_parts.append("\nEvidence AGAINST:")
                for e in evidence_against:
                    reasoning_parts.append(f"  - {e}")

            base_rate = result.get("base_rate_estimate", "")
            if base_rate:
                reasoning_parts.append(f"\nBase rate: {base_rate}")

            return Prediction(
                question_id=question.id,
                predictor_name=self.name,
                probability=result["probability"],
                confidence=result["confidence"],
                reasoning="\n".join(reasoning_parts),
                evidence_count=bundle.item_count,
            )

        except anthropic.APIError as e:
            logger.error(f"Claude API error: {e}")
            return Prediction(
                question_id=question.id,
                predictor_name=self.name,
                probability=0.5,
                confidence=0.0,
                reasoning=f"Claude API error: {e}",
                evidence_count=bundle.item_count,
            )
