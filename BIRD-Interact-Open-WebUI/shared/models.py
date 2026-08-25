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
    # Graph validation deliberately lives in the repository so malformed
    # requests use the stable INVALID_REQUEST response instead of FastAPI's
    # default 422 payload.
    include: Any = None
    hops: Any = 5
    paths: Any = 5


class KnowledgeGraphExpandRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    depth: Any = None
    nodes: Any = None


class KnowledgeGraphRequest(BaseModel):
    task_id: str
    knowledge_id: Any = None
    include: Any = None
    expand: Any = None


class SearchSemanticContextRequest(BaseModel):
    task_id: str
    queries: Any = None
    top_k: Any = None
    resource_types: Any = None


class MetadataSearchRequest(BaseModel):
    """Request contract for the metadata-only semantic search route."""

    model_config = ConfigDict(extra="allow")
    task_id: str
    queries: Any = None
    top_k: Any = None


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
