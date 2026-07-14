import json
import logging
from typing import List, Dict, Any
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from config.settings import settings
from config.observability import get_diagnostics_callbacks

logger = logging.getLogger(__name__)

class NavigatorAgent:
    def __init__(self):
        # Configure ChatGoogleGenerativeAI model from langchain-google-genai
        self.llm = ChatGoogleGenerativeAI(
            model=settings.GEMINI_MODEL,
            google_api_key=settings.GEMINI_API_KEY,
            temperature=0.1
        )

    def _load_prompt(self) -> ChatPromptTemplate:
        """
        Loads the step planner prompt from LangSmith Hub, or falls back to
        the local versioned file if LangSmith is unavailable.
        """
        import os
        from langsmith import Client
        
        prompt_name = "navigator_agent_step_planner"
        
        # 1. Attempt to pull from LangSmith Hub if tracing is active
        if settings.LANGCHAIN_TRACING_V2 and settings.LANGCHAIN_API_KEY:
            try:
                logger.info(f"Attempting to pull prompt '{prompt_name}' from LangSmith Hub...")
                client = Client()
                # LangSmith Client returns a LangChain prompt template object
                return client.pull_prompt(prompt_name)
            except Exception as e:
                logger.warning(f"Failed to pull prompt '{prompt_name}' from LangSmith Hub: {e}. Falling back to local version.")
        
        # 2. Local fallback path
        try:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            local_path = os.path.abspath(os.path.join(current_dir, "..", "..", "prompts", f"{prompt_name}.txt"))
            logger.info(f"Loading local prompt template from: {local_path}")
            with open(local_path, "r", encoding="utf-8") as f:
                prompt_content = f.read()
            return ChatPromptTemplate.from_template(prompt_content)
        except Exception as e:
            logger.critical(f"Failed to load local prompt fallback: {e}")
            # Baseline fallback to prevent panic
            return ChatPromptTemplate.from_template(
                "You are an expert browser automation planner. Translate description to Playwright steps.\n"
                "Target URL: {url}\nCredentials: {credentials_json}\nJourney: {journey_description}"
            )

    def plan_steps(
        self,
        url: str,
        credentials: Dict[str, str],
        journey_description: str
    ) -> List[Dict[str, Any]]:
        """
        Translates a natural language user journey and credentials into sequential Playwright actions.
        Uses LangChain implementation with structured config for tracing.
        """
        prompt = self._load_prompt()

        # Combine into LangChain expression (run name is passed during invocation)
        chain = prompt | self.llm | JsonOutputParser()

        try:
            logger.info("Requesting Gemini (via LangChain) to compile steps from user flow...")
            
            # Trace Integration: config must declare explicit run_name
            config = {
                "run_name": "navigator_agent_step_planner",
                "tags": ["navigator", "playwright_planning"],
                "callbacks": get_diagnostics_callbacks()
            }
            
            steps = chain.invoke({
                "url": url,
                "credentials_json": json.dumps(credentials),
                "journey_description": journey_description
            }, config=config)
            
            logger.info(f"Successfully generated {len(steps)} steps.")
            return steps
        except Exception as e:
            logger.error(f"Failed to plan steps via LangChain Gemini: {e}")
            # Return basic fallback navigation
            return [{"action": "navigate", "url": url}, {"action": "wait_for_load"}]

    async def aplan_steps(
        self,
        url: str,
        credentials: Dict[str, str],
        journey_description: str
    ) -> List[Dict[str, Any]]:
        """
        Asynchronously translates a natural language user journey and credentials into sequential Playwright actions.
        """
        prompt = self._load_prompt()
        chain = prompt | self.llm | JsonOutputParser()

        try:
            logger.info("Requesting Gemini (via LangChain ainvoke) to compile steps from user flow...")
            config = {
                "run_name": "navigator_agent_step_planner",
                "tags": ["navigator", "playwright_planning"],
                "callbacks": get_diagnostics_callbacks()
            }
            
            steps = await chain.ainvoke({
                "url": url,
                "credentials_json": json.dumps(credentials),
                "journey_description": journey_description
            }, config=config)
            
            logger.info(f"Successfully generated {len(steps)} steps.")
            return steps
        except Exception as e:
            logger.error(f"Failed to plan steps via LangChain Gemini async: {e}")
            return [{"action": "navigate", "url": url}, {"action": "wait_for_load"}]
