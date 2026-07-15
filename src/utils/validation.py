import json
import logging
import re
import os
from typing import Dict, Any, Tuple, Optional

from langchain_core.messages import HumanMessage

from src.utils.model_router import get_model_router, TaskType
from src.agents.intent_router import get_latest_human_text

logger = logging.getLogger(__name__)


def _normalize_message_content(content: Any) -> str:
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _clean_text(content: str) -> str:
    return (
        content.replace("“", '"')
        .replace("”", '"')
        .replace("’", "'")
        .replace("‘", "'")
        .replace("\u2028", "\n")
        .replace("\u2029", "\n")
    )


async def _parse_journey_payload(content: str) -> Tuple[str, Dict[str, str], str, bool]:
    """
    Parse a single user message into url/credentials/journey.
    Returns (url, credentials, journey, parsed_successfully).
    """
    content_cleaned = _clean_text(content)
    url = ""
    credentials: Dict[str, str] = {}
    journey = ""
    parsed_successfully = False

    # 1. Direct JSON
    try:
        data = json.loads(content_cleaned)
        url = data.get("target_url") or data.get("url") or ""
        credentials = data.get("credentials") or {}
        raw_journey = (
            data.get("user_journey_steps")
            or data.get("journey")
            or data.get("steps")
            or ""
        )
        # Chrome/Puppeteer recorder: { title, steps: [...] }
        if not url and isinstance(data, dict):
            # URL may be inside navigate steps
            steps = data.get("steps")
            if isinstance(steps, list):
                for step in steps:
                    if isinstance(step, dict) and step.get("type") == "navigate":
                        url = step.get("url") or url
                if not journey:
                    journey = json.dumps(data, indent=2)
        if isinstance(raw_journey, list) and not journey:
            # Keep structured list as text description when items are strings;
            # otherwise dump JSON for recorder-style steps.
            if raw_journey and all(isinstance(s, str) for s in raw_journey):
                journey = "\n".join(str(s) for s in raw_journey)
            else:
                journey = json.dumps(raw_journey, indent=2)
        elif raw_journey and not journey:
            journey = str(raw_journey)
        if url or journey:
            parsed_successfully = bool(url) or bool(journey)
    except Exception:
        pass

    # 2. Fix missing commas between array string elements
    if not parsed_successfully:
        try:
            fixed_content = re.sub(r'"\s*\n\s*"', '",\n"', content_cleaned)
            data = json.loads(fixed_content)
            url = data.get("target_url") or data.get("url") or ""
            credentials = data.get("credentials") or {}
            raw_journey = data.get("user_journey_steps") or data.get("journey") or ""
            if isinstance(raw_journey, list):
                journey = "\n".join(str(s) for s in raw_journey)
            else:
                journey = str(raw_journey)
            if url:
                parsed_successfully = True
        except Exception:
            pass

    # 3. Regex fallback
    if not parsed_successfully:
        url_match = re.search(r'"(?:target_url|url)"\s*:\s*"([^"]+)"', content_cleaned)
        if url_match:
            url = url_match.group(1)

        bare_url = re.search(r"https?://[^\s\"']+", content_cleaned)
        if not url and bare_url:
            url = bare_url.group(0).rstrip(".,)")

        creds_match = re.search(r'"credentials"\s*:\s*\{([^}]+)\}', content_cleaned)
        if creds_match:
            pairs = re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', creds_match.group(1))
            for k, v in pairs:
                credentials[k] = v

        journey_match = re.search(
            r'"(?:user_journey_steps|journey)"\s*:\s*\[([^\]]+)\]', content_cleaned
        )
        if journey_match:
            steps = re.findall(r'"([^"]+)"', journey_match.group(1))
            if not steps:
                steps = re.findall(r"'([^']+)'", journey_match.group(1))
            if steps:
                journey = "\n".join(steps)
                if url:
                    parsed_successfully = True
        else:
            journey_str_match = re.search(
                r'"(?:user_journey_steps|journey)"\s*:\s*"([^"]+)"', content_cleaned
            )
            if journey_str_match:
                journey = journey_str_match.group(1)
                if url:
                    parsed_successfully = True
            elif url:
                journey = content_cleaned
                parsed_successfully = True

    # 4. LLM fallback for unstructured journey text that already has a URL signal
    if not parsed_successfully and ("http://" in content_cleaned or "https://" in content_cleaned):
                    try:
                        logger.info("Using LLM to extract target URL, credentials, and steps...")
                        import asyncio

                        llm = get_model_router().get_llm(TaskType.EXTRACTION)
                        current_dir = os.path.dirname(os.path.abspath(__file__))
                        prompt_path = os.path.abspath(
                            os.path.join(current_dir, "..", "..", "prompts", "input_extractor.txt")
                        )
                        with open(prompt_path, "r", encoding="utf-8") as f:
                            prompt_template = f.read()
                        extract_prompt = prompt_template.format(input_message=content)
                        from src.utils.model_router import invoke_llm_sync

                        response = await asyncio.to_thread(
                            invoke_llm_sync, llm, extract_prompt
                        )
                        resp_text = response.content
                        if isinstance(resp_text, list):
                            resp_text = "".join(str(p) for p in resp_text)
                        if "```" in resp_text:
                            resp_text = resp_text.split("```json")[-1].split("```")[0].strip()
                        extracted_data = json.loads(resp_text)
                        extracted_url = extracted_data.get("target_url") or extracted_data.get("url") or ""
                        if extracted_url:
                            url = extracted_url
                            credentials = extracted_data.get("credentials") or {}
                            raw_journey = (
                                extracted_data.get("user_journey_steps")
                                or extracted_data.get("journey")
                                or ""
                            )
                            if isinstance(raw_journey, list):
                                journey = "\n".join(str(s) for s in raw_journey)
                            else:
                                journey = str(raw_journey)
                            parsed_successfully = True
                    except Exception as e:
                        logger.warning(f"LLM fallback extraction failed: {e}")

    return url, credentials, journey, parsed_successfully


async def extract_inputs_from_message(
    state: Dict[str, Any],
    *,
    allow_state_reuse: bool = False,
) -> Tuple[str, Dict[str, str], str]:
    """
    Extracts target URL, credentials, and journey description.

    By default only the *latest* human message is considered, so casual follow-ups
    like "Hi" do not resurrect an older SauceDemo payload from thread history.
    Set allow_state_reuse=True for explicit follow-up analysis requests.
    """
    url = ""
    credentials: Dict[str, str] = {}
    journey = ""

    if allow_state_reuse:
        url = state.get("target_url", "") or ""
        credentials = dict(state.get("credentials") or {})
        raw_steps = state.get("user_journey_steps", [])
        is_already_planned = (
            isinstance(raw_steps, list)
            and len(raw_steps) > 0
            and all(isinstance(s, dict) and "action" in s for s in raw_steps)
        )
        if is_already_planned:
            journey = json.dumps(raw_steps)
        elif isinstance(raw_steps, list) and raw_steps:
            journey_parts = []
            for step in raw_steps:
                if isinstance(step, str):
                    journey_parts.append(step)
                elif isinstance(step, dict):
                    journey_parts.append(json.dumps(step))
                else:
                    journey_parts.append(str(step))
            journey = "\n".join(journey_parts)
        if url and journey:
            return url, credentials, journey

    latest = get_latest_human_text(state.get("messages"))
    if latest:
        parsed_url, parsed_creds, parsed_journey, ok = await _parse_journey_payload(latest)
        if parsed_url:
            url = parsed_url
        if parsed_creds:
            credentials = parsed_creds
        if parsed_journey:
            journey = parsed_journey
        if ok and url:
            return url, credentials, journey

    # Follow-up reuse: if latest message had no payload but state reuse was allowed
    if allow_state_reuse and state.get("target_url"):
        return (
            state.get("target_url", ""),
            dict(state.get("credentials") or {}),
            journey or get_latest_human_text(state.get("messages")),
        )

    return url, credentials, journey
