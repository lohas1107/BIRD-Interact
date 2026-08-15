"""Pydantic models for inter-service communication."""

from pydantic import BaseModel, ConfigDict
from typing import Any, Dict, List, Optional


class InitTaskRequest(BaseModel):
    task_id: str
    task_data: Dict[str, Any]


class ExecuteSQLRequest(BaseModel):
    sql: str
    task_id: str


class ExecuteSQLResponse(BaseModel):
    result: str
    success: bool
    error: Optional[str] = None


class SubmitSQLRequest(BaseModel):
    sql: str
    task_id: str


class SubmitSQLResponse(BaseModel):
    passed: bool
    message: str
    reward: float = 0.0
    phase_completed: Optional[int] = None
    has_follow_up: bool = False
    follow_up_query: Optional[str] = None


class SchemaRequest(BaseModel):
    task_id: str


class TableSchemaRequest(BaseModel):
    task_id: str
    database_name: str
    from_table: str
    to_table: Optional[str] = None
    include: Optional[List[str]] = None
    max_hops: Optional[int] = 5
    max_paths: Optional[int] = 5


class KnowledgeGraphExpandRequest(BaseModel):
    # Keep validation in the graph repository so malformed requests use the
    # stable INVALID_REQUEST error contract instead of FastAPI's 422 shape.
    model_config = ConfigDict(extra="allow")
    depth: Any = None
    max_nodes: Any = None


class KnowledgeGraphRequest(BaseModel):
    task_id: str
    id: Any = None
    include: Any = None
    expand: Any = None


class ColumnMeaningRequest(BaseModel):
    task_id: str
    table_name: str
    column_name: str


class KnowledgeRequest(BaseModel):
    task_id: str
    knowledge_name: Optional[str] = None


class AskUserRequest(BaseModel):
    question: str
    task_id: str


class AskUserResponse(BaseModel):
    answer: str


class PhaseTransitionRequest(BaseModel):
    task_id: str
