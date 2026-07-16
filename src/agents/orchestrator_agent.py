"""Decomposes NFE browser journeys into ordered, specialized work phases."""

import json
import logging
from typing import List, Dict, Any

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from config.observability import get_diagnostics_callbacks
from src.utils.model_router import get_model_router, TaskType
from src.utils.json_parsing import RobustJsonOutputParser, normalize_sub_task_list
from src.utils.prompt_loader import load_prompt_text

logger = logging.getLogger(__name__)


class SubTaskSpec(BaseModel):
    """A single named phase in an orchestrated browser journey."""

    name: str
    description: str
    focus: str = "general"


class SubTaskPlanResponse(BaseModel):
    """Structured LLM output containing an ordered journey decomposition."""

    sub_tasks: List[SubTaskSpec] = Field(
        description="Ordered sub-tasks that decompose the user journey"
    )


class OrchestratorAgent:
    """Decomposes a user journey into sub-tasks for specialized sub-agents."""

    def __init__(self):
        """Configure orchestration models through the shared failover router."""
        self.router = get_model_router()
        self.llm = self.router.get_llm(TaskType.ORCHESTRATION)

    def _load_prompt(self) -> ChatPromptTemplate:
        """Load the journey-decomposition prompt from the repository.

        Returns:
            A chat prompt template for sub-task planning.

        Raises:
            OSError: If the prompt file cannot be read.
        """
        return ChatPromptTemplate.from_template(
            load_prompt_text("orchestrator_task_decomposer")
        )

    async def decompose_journey(
        self,
        url: str,
        credentials: Dict[str, str],
        journey_description: str,
    ) -> List[Dict[str, Any]]:
        """Split a user journey into ordered pipeline phases.

        Args:
            url: Target application URL.
            credentials: Credential values keyed by logical names.
            journey_description: Natural-language end-to-end journey.

        Returns:
            Ordered sub-task dictionaries with name, description, and focus.
        """
        if not journey_description or not journey_description.strip():
            return [{
                "name": "main_flow",
                "description": "Execute the complete user journey",
                "focus": "general",
            }]

        prompt = self._load_prompt()
        config = {
            "run_name": "orchestrator_task_decomposer",
            "tags": ["orchestrator", "task_decomposition"],
            "callbacks": get_diagnostics_callbacks(),
        }
        inputs = {
            "url": url,
            "credentials_json": json.dumps(credentials),
            "journey_description": journey_description,
        }

        try:
            def build_structured(llm):
                """Bind the planner model to the sub-task response schema."""
                return prompt | llm.with_structured_output(
                    SubTaskPlanResponse,
                    method="json_schema",
                )

            response = await self.router.ainvoke_with_failover(
                TaskType.ORCHESTRATION,
                build_structured,
                inputs,
                config=config,
            )
            if isinstance(response, SubTaskPlanResponse):
                raw = [task.model_dump() for task in response.sub_tasks]
            elif isinstance(response, dict):
                # Some structured-output providers return decoded dictionaries
                # instead of Pydantic instances.
                raw = response.get("sub_tasks", response)
            else:
                raw = getattr(response, "sub_tasks", [])
            sub_tasks = normalize_sub_task_list(raw)
        except Exception as structured_err:
            logger.warning(
                "Structured orchestrator output failed (%s). Falling back to JSON parser.",
                structured_err,
            )
            try:
                def build_json(llm):
                    """Build the legacy JSON-parser chain for model fallback."""
                    return prompt | llm | RobustJsonOutputParser()

                payload = await self.router.ainvoke_with_failover(
                    TaskType.ORCHESTRATION,
                    build_json,
                    inputs,
                    config=config,
                )
                sub_tasks = normalize_sub_task_list(payload)
            except Exception as e:
                logger.warning(f"Orchestrator decomposition failed, using single sub-task: {e}")
                return [{
                    "name": "main_flow",
                    "description": journey_description,
                    "focus": "general",
                }]

        try:
            if not isinstance(sub_tasks, list) or not sub_tasks:
                raise ValueError("Orchestrator returned empty or invalid sub-task list")

            validated = []
            for i, task in enumerate(sub_tasks):
                validated.append({
                    "name": task.get("name", f"sub_task_{i + 1}"),
                    "description": task.get("description", journey_description),
                    "focus": task.get("focus", "general"),
                })
            logger.info(f"Orchestrator decomposed journey into {len(validated)} sub-tasks.")
            return validated
        except Exception as e:
            logger.warning(f"Orchestrator decomposition failed, using single sub-task: {e}")
            return [{
                "name": "main_flow",
                "description": journey_description,
                "focus": "general",
            }]
