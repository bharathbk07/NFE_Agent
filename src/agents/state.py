from typing import Annotated, Sequence, TypedDict, List, Dict, Any
# pyrefly: ignore [missing-import]
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

class NetworkRequestLog(TypedDict):
    url: str
    method: str
    headers: Dict[str, str]
    cookies: List[Dict[str, Any]]
    post_data: Any # JSON or raw text
    response_headers: Dict[str, str]
    response_body: str
    status: int

class RunRecord(TypedDict):
    run_id: int
    network_requests: List[NetworkRequestLog]
    cookies: List[Dict[str, Any]]
    local_storage: Dict[str, str]
    session_storage: Dict[str, str]
    screenshot_paths: List[str]

class CorrelationItem(TypedDict):
    request_url: str
    method: str
    location: str # "header" | "cookie" | "body" | "query" | "path"
    key: str
    dynamic_name: str
    run1_value: str
    run2_value: str
    reason: str

class DependencyChain(TypedDict):
    source_request: str # Request URL where value originated
    source_location: str # response headers or body json path
    target_request: str # Request URL where value is sent
    target_location: str # request headers, query, body, etc.
    value_key: str

class AgentState(TypedDict):
    """
    Type-safe core schema holding active states across state nodes.
    """
    messages: Annotated[Sequence[BaseMessage], add_messages]
    target_url: str
    credentials: Dict[str, str]
    user_journey_steps: List[Any]
    run_records: List[RunRecord]
    correlations: List[CorrelationItem]
    dependencies: List[DependencyChain]
    error_log: List[str]
