from pydantic import BaseModel, Field
from typing import Optional, List
from enum import Enum
from datetime import datetime
import uuid


# ── Enums ──────────────────────────────────────────────────────────────────

class ChannelType(str, Enum):
    sms = "sms"
    web_form = "web_form"
    chat_widget = "chat_widget"

class MessageDirection(str, Enum):
    inbound = "inbound"
    outbound = "outbound"

class MessageStatus(str, Enum):
    received = "received"
    processing = "processing"
    classified = "classified"
    draft_generated = "draft_generated"
    sent = "sent"
    escalated = "escalated"
    dismissed = "dismissed"

class Intent(str, Enum):
    booking_request = "booking_request"
    quote_request = "quote_request"
    faq = "faq"
    complaint = "complaint"
    emergency = "emergency"
    billing = "billing"
    cancellation = "cancellation"
    speak_to_owner = "speak_to_owner"
    unknown = "unknown"

class Sentiment(str, Enum):
    positive = "positive"
    neutral = "neutral"
    frustrated = "frustrated"
    urgent = "urgent"
    distressed = "distressed"

class RecommendedAction(str, Enum):
    auto_send = "auto_send"
    approval_queue = "approval_queue"
    escalate_immediately = "escalate_immediately"

class QueueItemStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    edited_and_sent = "edited_and_sent"
    dismissed = "dismissed"

class PlanTier(str, Enum):
    starter = "starter"
    growth = "growth"
    pro = "pro"

class ContactPreference(str, Enum):
    sms = "sms"
    email = "email"


# ── Inbound webhook payloads ────────────────────────────────────────────────

class InboundFormPayload(BaseModel):
    channel_id: str
    sender_name: Optional[str] = None
    sender_phone: Optional[str] = None
    sender_email: Optional[str] = None
    contact_preference: Optional[str] = "sms"   # "sms" or "email"
    body: str

class InboundChatPayload(BaseModel):
    channel_id: str
    session_id: Optional[str] = None
    sender_name: Optional[str] = None
    body: str


# ── Classification ──────────────────────────────────────────────────────────

class ClassificationResult(BaseModel):
    """Strict schema returned by Claude classification call."""
    intent: Intent = Intent.unknown
    urgency_score: int = Field(ge=1, le=5, default=1)
    sentiment: Sentiment = Sentiment.neutral
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    requires_human_review: bool = True
    recommended_action: RecommendedAction = RecommendedAction.approval_queue
    safe_to_auto_send: bool = False
    reasoning_short: str = ""
    escalation_reason: Optional[str] = None

    def apply_safety_gates(self, confidence_threshold: float = 0.75) -> "ClassificationResult":
        if self.confidence < confidence_threshold:
            self.requires_human_review = True
            self.safe_to_auto_send = False
        if self.intent == Intent.emergency:
            self.recommended_action = RecommendedAction.escalate_immediately
            self.requires_human_review = True
            self.safe_to_auto_send = False
            if not self.escalation_reason:
                self.escalation_reason = "emergency_detected"
        human_review_intents = {Intent.complaint, Intent.billing, Intent.speak_to_owner}
        if self.intent in human_review_intents:
            self.requires_human_review = True
            self.safe_to_auto_send = False
            if self.recommended_action == RecommendedAction.auto_send:
                self.recommended_action = RecommendedAction.approval_queue
        return self


# ── Message ─────────────────────────────────────────────────────────────────

class MessageCreate(BaseModel):
    business_id: str
    conversation_id: Optional[str] = None
    contact_id: Optional[str] = None
    channel_id: str
    channel_type: ChannelType
    direction: MessageDirection = MessageDirection.inbound
    body: str
    sender_identifier: Optional[str] = None
    sender_name: Optional[str] = None

class MessageResponse(BaseModel):
    id: str
    business_id: str
    conversation_id: Optional[str]
    contact_id: Optional[str]
    channel_type: ChannelType
    direction: MessageDirection
    body: str
    sender_name: Optional[str]
    status: MessageStatus
    intent: Optional[Intent]
    urgency_score: Optional[int]
    sentiment: Optional[Sentiment]
    confidence: Optional[float]
    requires_human_review: Optional[bool]
    safe_to_auto_send: Optional[bool]
    recommended_action: Optional[RecommendedAction]
    reasoning_short: Optional[str]
    received_at: datetime
    processed_at: Optional[datetime]


# ── Draft ────────────────────────────────────────────────────────────────────

class DraftResponse(BaseModel):
    id: str
    message_id: str
    draft_body: str
    draft_status: str
    facts_verified: bool
    model_version: str
    created_at: datetime


# ── Approval Queue ────────────────────────────────────────────────────────────

class QueueItemResponse(BaseModel):
    id: str
    message_id: str
    draft_id: str
    business_id: str
    status: QueueItemStatus
    priority: str
    message: Optional[MessageResponse] = None
    draft: Optional[DraftResponse] = None
    queued_at: datetime

class ApproveRequest(BaseModel):
    reviewer_id: str

class EditAndSendRequest(BaseModel):
    reviewer_id: str
    edited_body: str

class DismissRequest(BaseModel):
    reviewer_id: str
    reason: Optional[str] = None


# ── Conversation ──────────────────────────────────────────────────────────────

class ConversationResponse(BaseModel):
    id: str
    business_id: str
    contact_id: Optional[str]
    channel_type: ChannelType
    status: str
    created_at: datetime
    last_message_at: Optional[datetime]
    message_count: Optional[int] = 0


# ── Analytics ─────────────────────────────────────────────────────────────────

class DashboardSummary(BaseModel):
    new_leads: int = 0
    avg_first_response_seconds: Optional[float] = None
    auto_handled_count: int = 0
    human_reviewed_count: int = 0
    urgent_count: int = 0
    booking_requests_captured: int = 0
    period: str = "today"


# ── Webhook responses ─────────────────────────────────────────────────────────

class WebhookAck(BaseModel):
    message_id: str
    status: str = "received"
