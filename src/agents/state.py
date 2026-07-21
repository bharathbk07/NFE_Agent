"""Shared LangGraph state schemas for the NFE performance-testing pipeline."""

from typing import Annotated, NotRequired, Sequence, TypedDict, List, Dict, Any
# pyrefly: ignore [missing-import]
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

class NetworkRequestLog(TypedDict):
    """A captured HTTP exchange associated with a browser journey step."""

    url: str
    method: str
    headers: Dict[str, str]
    cookies: List[Dict[str, Any]]
    post_data: Any  # JSON or raw text
    response_headers: Dict[str, str]
    response_body: str
    status: int
    resource_type: NotRequired[str]
    initiator_type: NotRequired[str]
    mime_type: NotRequired[str]
    step_index: NotRequired[int]
    step_action: NotRequired[str]
    capture_source: NotRequired[str]  # "cdp" | "playwright" | "page_url" | "page_navigation"
    body_type: NotRequired[str]  # json | form | text | empty

class RunRecord(TypedDict):
    """Artifacts and browser state captured during one journey execution."""

    run_id: int
    network_requests: List[NetworkRequestLog]
    cookies: List[Dict[str, Any]]
    local_storage: Dict[str, str]
    session_storage: Dict[str, str]
    screenshot_paths: List[str]
    step_timeline: NotRequired[List[Dict[str, Any]]]


class CorrelationItem(TypedDict):
    """A request value observed to vary across repeated journey runs."""

    request_url: str
    method: str
    location: str  # "header" | "cookie" | "body" | "query" | "path"
    key: str
    dynamic_name: str
    run1_value: str
    run2_value: str
    reason: str
    step_index: int
    step_action: str


class DependencyChain(TypedDict):
    """An extract-to-pass relationship between source and target requests."""

    source_request: str  # Request URL where value originated
    source_location: str  # response headers or body json path
    source_step_index: int
    source_step_action: str
    target_request: str  # Request URL where value is sent
    target_location: str  # request headers, query, body, etc.
    target_step_index: int
    target_step_action: str
    value_key: str


class SubTask(TypedDict):
    """An ordered journey phase assigned to specialized pipeline agents."""

    name: str
    description: str
    focus: str  # authentication | navigation | form_input | transaction | general


class ParameterCandidate(TypedDict):
    """A user-supplied value eligible for load-test parameterization."""

    selector: str
    value: str
    variable_name: str
    is_credential: bool
    credential_name: NotRequired[str]
    propagations: List[str]


class TransactionGroup(TypedDict):
    """A logical load-test transaction containing UI and HTTP activity."""

    name: str
    description: str
    request_urls: List[str]
    http_requests: NotRequired[List[str]]
    http_entries: NotRequired[List[Dict[str, Any]]]
    ui_actions: NotRequired[List[str]]
    ui_steps: NotRequired[List[Dict[str, Any]]]
    step_indices: NotRequired[List[int]]


class AgentState(TypedDict):
    """Type-safe state exchanged among NFE LangGraph pipeline nodes."""
    messages: Annotated[Sequence[BaseMessage], add_messages]
    intent: NotRequired[str]
    target_url: NotRequired[str]
    credentials: NotRequired[Dict[str, str]]
    user_journey_steps: NotRequired[List[Any]]
    sub_tasks: NotRequired[List[SubTask]]
    run_records: NotRequired[List[RunRecord]]
    correlations: NotRequired[List[CorrelationItem]]
    dependencies: NotRequired[List[DependencyChain]]
    parameterizable_candidates: NotRequired[List[ParameterCandidate]]
    transactions: NotRequired[List[TransactionGroup]]
    performance_test_output: NotRequired[Dict[str, Any]]
    correlation_advice: NotRequired[Dict[str, Any]]
    cookie_correlation_notes: NotRequired[List[Dict[str, Any]]]
    recording_mode: NotRequired[str]  # "watch_me" | "reuse" | unset
    watch_me_status: NotRequired[str]
    recording_file: NotRequired[str]
    error_log: NotRequired[List[str]]
