"""
Routing Engine — Frontdesk AI
Determines final routing action after AI classification.
Applies business-configured auto-send policies and hardcoded safety rules.
"""

import logging
from app.models.schemas import ClassificationResult, RecommendedAction, Intent

logger = logging.getLogger(__name__)

# Intents that can NEVER be auto-sent regardless of any setting.
# Hardcoded — cannot be overridden by business rules, plan tier, or toggle.
NEVER_AUTO_SEND = {
    Intent.emergency,       # Always escalate + hold
    Intent.complaint,       # Always hold — risk of making things worse
    Intent.billing,         # Always hold — financial sensitivity
    Intent.speak_to_owner,  # Always hold — explicit human request
    Intent.cancellation,    # Always hold — retention opportunity
}

# Intents eligible for auto-send when toggle is on and confidence is high
AUTO_SEND_ELIGIBLE = {
    Intent.faq,
    Intent.unknown,  # Only if confidence is very high
}

# Minimum confidence required for auto-send
AUTO_SEND_MIN_CONFIDENCE = 0.85


def determine_routing(
    classification: ClassificationResult,
    business_rules: list[dict],
    plan_tier: str = "starter",
    auto_respond_enabled: bool = False,
) -> RecommendedAction:
    """
    Final routing decision after classification + safety gates.

    Priority order:
    1. Hardcoded escalation (always wins)
    2. Hardcoded never-auto-send intents
    3. Low confidence — always queue
    4. Starter plan — always queue
    5. Growth/Pro + auto_respond OFF — owner chose manual
    6. Growth/Pro + auto_respond ON + eligible intent + high confidence → auto-send
    7. Default: approval queue
    """

    # ── Priority 1: Escalation — always wins ─────────────────────────────────
    if classification.recommended_action == RecommendedAction.escalate_immediately:
        logger.info(f"Routing: ESCALATE (reason: {classification.escalation_reason})")
        return RecommendedAction.escalate_immediately

    # ── Priority 2: Hardcoded sensitive intents — never auto-send ─────────────
    if classification.intent in NEVER_AUTO_SEND:
        logger.info(f"Routing: QUEUE (hardcoded — intent '{classification.intent}' never auto-sends)")
        return RecommendedAction.approval_queue

    # ── Priority 3: Low confidence — must have human review ───────────────────
    if not classification.safe_to_auto_send:
        logger.info(f"Routing: QUEUE (confidence {classification.confidence:.2f} flagged unsafe)")
        return RecommendedAction.approval_queue

    # ── Priority 4: Starter plan — always manual ───────────────────────────────
    if plan_tier == "starter":
        logger.info(f"Routing: QUEUE (Starter plan — manual approval required)")
        return RecommendedAction.approval_queue

    # ── Priority 5: Growth/Pro but owner turned off auto-respond ──────────────
    if not auto_respond_enabled:
        logger.info(f"Routing: QUEUE ({plan_tier} plan — auto-respond disabled by owner)")
        return RecommendedAction.approval_queue

    # ── Priority 6: Growth/Pro + auto-respond ON ──────────────────────────────
    # Auto-send if intent is eligible and confidence meets threshold
    if (
        classification.intent in AUTO_SEND_ELIGIBLE
        and classification.confidence >= AUTO_SEND_MIN_CONFIDENCE
        and classification.safe_to_auto_send
    ):
        logger.info(
            f"Routing: AUTO-SEND (intent={classification.intent}, "
            f"conf={classification.confidence:.2f} >= {AUTO_SEND_MIN_CONFIDENCE})"
        )
        return RecommendedAction.auto_send

    # ── Default: approval queue ────────────────────────────────────────────────
    logger.info(f"Routing: QUEUE (default — intent {classification.intent} not eligible for auto-send)")
    return RecommendedAction.approval_queue


def get_queue_priority(classification: ClassificationResult) -> str:
    """Returns queue display priority: urgent | high | normal"""
    if classification.urgency_score >= 4:
        return "urgent"
    if classification.urgency_score >= 3:
        return "high"
    return "normal"