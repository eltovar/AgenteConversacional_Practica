# state_manager.py (NUEVO)
from enum import Enum
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

class ConversationStatus(str, Enum):
    RECEPTION_START = "RECEPTION_START"
    AWAITING_CLARIFICATION = "AWAITING_CLARIFICATION"
    AWAITING_LEAD_NAME = "AWAITING_LEAD_NAME"
    TRANSFERRED_INFO = "TRANSFERRED_INFO"
    TRANSFERRED_LEADSALES = "TRANSFERRED_LEADSALES"
    WELCOME_SENT = "WELCOME_SENT"

class ConversationState(BaseModel):
    session_id: str
    status: ConversationStatus = ConversationStatus.RECEPTION_START
    lead_data: Dict[str, Any] = Field(default_factory=dict)
    history: List = Field(default_factory=list)

class StateManager:
    def __init__(self):
        # Simula la persistencia en memoria (Respuesta a Q3)
        self.sessions: Dict[str, ConversationState] = {}

    def get_state(self, session_id: str) -> ConversationState:
        if session_id not in self.sessions:
            self.sessions[session_id] = ConversationState(session_id=session_id)
        return self.sessions[session_id]

    def update_state(self, state: ConversationState):
        self.sessions[state.session_id] = state