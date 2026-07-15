import json
import logging
import os
from typing import List, Dict, Any

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from config.observability import get_diagnostics_callbacks
from src.utils.model_router import get_model_router, TaskType
from src.utils.json_parsing import RobustJsonOutputParser, normalize_sub_task_list

logger = logging.getLogger(__name__)


class SubTaskSpec(BaseModel):
    name: str
    description: str
    focus: str = "general"


class SubTaskPlanResponse(BaseModel):
    sub_tasks: List[SubTaskSpec] = Field(
        description="Ordered sub-tasks that decompose the user journey"
    )


class OrchestratorAgent:
    """Decomposes a user journey into sub-tasks for specialized sub-agents."""

    def __init__(self):
        self.router = get_model_router()
        self.llm = self.router.get_llm(TaskType.ORCHESTRATION)

    def _load_prompt(self) -> ChatPromptTemplate:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        local_path = os.path.abspath(
            os.path.join(current_dir, "..", "..", "prompts", "orchestrator_task_decomposer.txt")
        )
        with open(local_path, "r", encoding="utf-8") as f:
            return ChatPromptTemplate.from_template(f.read())

    async def decompose_journey(
        self,
        url: str,
        credentials: Dict[str, str],
        journey_description: str,
    ) -> List[Dict[str, Any]]:
        """Split a user journey into ordered sub-tasks for sub-agent distribution."""
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
