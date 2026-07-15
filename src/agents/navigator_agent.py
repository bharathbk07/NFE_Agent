import json
import logging
import os
from typing import List, Dict, Any, Optional

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, ConfigDict, Field

from config.settings import settings
from config.observability import get_diagnostics_callbacks
from src.utils.model_router import get_model_router, TaskType
from src.utils.json_parsing import (
    RobustJsonOutputParser,
    normalize_step_list,
)

logger = logging.getLogger(__name__)

USE_LANGSMITH_PROMPTS = os.getenv("USE_LANGSMITH_PROMPTS", "false").lower() == "true"


class PlaywrightStep(BaseModel):
    model_config = ConfigDict(extra="allow")

    action: str
    url: Optional[str] = None
    selector: Optional[str] = None
    value: Optional[str] = None
    timeout: Optional[int] = None


class StepPlanResponse(BaseModel):
    steps: List[PlaywrightStep] = Field(
        description="Ordered Playwright interaction steps for the user journey"
    )


class NavigatorAgent:
    def __init__(self):
        self.router = get_model_router()
        self.llm = self.router.get_llm(TaskType.NAVIGATION)

    def _load_local_prompt(self) -> ChatPromptTemplate:
        prompt_name = "navigator_agent_step_planner"
        current_dir = os.path.dirname(os.path.abspath(__file__))
        local_path = os.path.abspath(
            os.path.join(current_dir, "..", "..", "prompts", f"{prompt_name}.txt")
        )
        logger.info(f"Loading local prompt template from: {local_path}")
        with open(local_path, "r", encoding="utf-8") as f:
            return ChatPromptTemplate.from_template(f.read())

    def _load_prompt(self, prefer_local: bool = True) -> ChatPromptTemplate:
        """
        Loads the local step planner prompt by default.
        LangSmith Hub pulls are opt-in (USE_LANGSMITH_PROMPTS=true) to avoid
        blocking the async event loop.
        """
        if prefer_local or not USE_LANGSMITH_PROMPTS:
            return self._load_local_prompt()

        if settings.LANGCHAIN_TRACING_V2 and settings.LANGCHAIN_API_KEY:
            try:
                from langsmith import Client

                prompt_name = "navigator_agent_step_planner"
                logger.info(
                    f"Attempting to pull prompt '{prompt_name}' from LangSmith Hub..."
                )
                client = Client()
                return client.pull_prompt(prompt_name)
            except Exception as e:
                logger.warning(
                    f"Failed to pull prompt from LangSmith Hub: {e}. "
                    "Falling back to local version."
                )

        return self._load_local_prompt()

    def _steps_from_structured(self, response: Any) -> List[Dict[str, Any]]:
        if isinstance(response, StepPlanResponse):
            raw_steps = response.steps
        elif isinstance(response, dict):
            raw_steps = response.get("steps", [])
        else:
            raw_steps = getattr(response, "steps", []) or []

        steps: List[Dict[str, Any]] = []
        for step in raw_steps:
            if hasattr(step, "model_dump"):
                payload = step.model_dump(exclude_none=True)
            elif isinstance(step, dict):
                payload = {k: v for k, v in step.items() if v is not None}
            else:
                continue
            if "action" in payload:
                steps.append(payload)
        return steps

    async def _invoke_planner(
        self,
        url: str,
        credentials: Dict[str, str],
        journey_description: str,
    ) -> List[Dict[str, Any]]:
        prompt = self._load_prompt(prefer_local=True)
        config = {
            "run_name": "navigator_agent_step_planner",
            "tags": ["navigator", "playwright_planning"],
            "callbacks": get_diagnostics_callbacks(),
        }
        inputs = {
            "url": url,
            "credentials_json": json.dumps(credentials),
            "journey_description": journey_description,
        }

        def build_structured(llm):
            return prompt | llm.with_structured_output(
                StepPlanResponse,
                method="json_schema",
            )

        try:
            response = await self.router.ainvoke_with_failover(
                TaskType.NAVIGATION,
                build_structured,
                inputs,
                config=config,
            )
            steps = self._steps_from_structured(response)
            if steps:
                return steps
        except Exception as structured_err:
            logger.warning(
                "Structured step planning failed (%s). Falling back to JSON parser.",
                structured_err,
            )

        def build_json(llm):
            return prompt | llm | RobustJsonOutputParser()

        payload = await self.router.ainvoke_with_failover(
            TaskType.NAVIGATION,
            build_json,
            inputs,
            config=config,
        )
        return normalize_step_list(payload)

    def plan_steps(
        self,
        url: str,
        credentials: Dict[str, str],
        journey_description: str,
    ) -> List[Dict[str, Any]]:
        """
        Translates a natural language user journey and credentials into sequential Playwright actions.
        """
        import asyncio

        return asyncio.run(
            self.aplan_steps(url, credentials, journey_description)
        )

    async def aplan_steps(
        self,
        url: str,
        credentials: Dict[str, str],
        journey_description: str,
    ) -> List[Dict[str, Any]]:
        """
        Asynchronously translates a natural language user journey and credentials
        into sequential Playwright actions.
        """
        logger.info("Requesting Gemini (via LangChain) to compile steps from user flow...")

        try:
            steps = await self._invoke_planner(url, credentials, journey_description)
            if not steps:
                raise ValueError("Planner returned no steps")
            logger.info(f"Successfully generated {len(steps)} steps.")
            return steps
        except Exception as e:
            logger.error("Failed to plan steps: %s", e)
            logger.error("Returning minimal fallback navigation steps.")
            return [{"action": "navigate", "url": url}, {"action": "wait_for_load"}]
