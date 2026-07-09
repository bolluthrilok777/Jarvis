from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    mode: str = "light"


class CommandRequest(BaseModel):
    command: str
    mode: str = "light"


class ConfirmRequest(BaseModel):
    action_id: str
    confirm: bool
