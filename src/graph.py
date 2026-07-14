import json
import logging
from typing import Dict, Any, List
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import StateGraph, START, END

from src.agents.state import AgentState, RunRecord
from src.agents.navigator_agent import NavigatorAgent
from src.tools.playwright_tool import PlaywrightBrowserRecorder
from src.agents.analyst_agent import TrafficAnalystAgent

logger = logging.getLogger("AgentGraph")

# Nodes
async def plan_navigator_steps(state: AgentState) -> Dict[str, Any]:
    """Parses user input or messages to get steps."""
    logger.info("Node: plan_navigator_steps starting...")
    
    # Extract URL, credentials, and journey description from state or last message
    url = state.get("target_url", "")
    credentials = state.get("credentials", {})
    raw_steps = state.get("user_journey_steps", [])
    
    is_already_planned = isinstance(raw_steps, list) and len(raw_steps) > 0 and all(isinstance(s, dict) and "action" in s for s in raw_steps)
    
    journey = ""
    if not is_already_planned:
        journey_parts = []
        for step in raw_steps:
            if isinstance(step, str):
                journey_parts.append(step)
            elif isinstance(step, dict):
                journey_parts.append(json.dumps(step))
            else:
                journey_parts.append(str(step))
        journey = "\n".join(journey_parts)
    
    # Find the latest HumanMessage in the messages timeline
    if not url and state.get("messages"):
        for msg in reversed(state["messages"]):
            if isinstance(msg, HumanMessage):
                content = msg.content
                if isinstance(content, list):
                    parts = []
                    for part in content:
                        if isinstance(part, str):
                            parts.append(part)
                        elif isinstance(part, dict) and part.get("type") == "text":
                            parts.append(part.get("text", ""))
                    content = "\n".join(parts)
                elif not isinstance(content, str):
                    content = str(content) if content is not None else ""

                import re
                
                # Clean up smart quotes, curly quotes, and unicode line/paragraph separators
                content_cleaned = content.replace("“", "\"").replace("”", "\"").replace("’", "'").replace("‘", "'").replace("\u2028", "\n").replace("\u2029", "\n")
                
                # 1. Try to parse directly as JSON
                parsed_successfully = False
                try:
                    data = json.loads(content_cleaned)
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

                # 2. Try parsing after correcting missing commas between elements in arrays
                if not parsed_successfully:
                    try:
                        fixed_content = re.sub(r'"\s*\n\s*"', '",\n"', content_cleaned)
                        fixed_content = re.sub(r'"\s*\r?\n\s*"', '",\n"', fixed_content)
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

                # 3. Regex Fallback
                if not parsed_successfully:
                    url_match = re.search(r'"(?:target_url|url)"\s*:\s*"([^"]+)"', content_cleaned)
                    if url_match:
                        url = url_match.group(1)
                    
                    creds_match = re.search(r'"credentials"\s*:\s*\{([^}]+)\}', content_cleaned)
                    if creds_match:
                        creds_content = creds_match.group(1)
                        pairs = re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', creds_content)
                        for k, v in pairs:
                            credentials[k] = v
                    
                    journey_match = re.search(r'"(?:user_journey_steps|journey)"\s*:\s*\[([^\]]+)\]', content_cleaned)
                    if journey_match:
                        steps_content = journey_match.group(1)
                        steps = re.findall(r'"([^"]+)"', steps_content)
                        if not steps:
                            steps = re.findall(r"'([^']+)'", steps_content)
                        if steps:
                            journey = "\n".join(steps)
                            if url:
                                parsed_successfully = True
                    else:
                        journey_str_match = re.search(r'"(?:user_journey_steps|journey)"\s*:\s*"([^"]+)"', content_cleaned)
                        if journey_str_match:
                            journey = journey_str_match.group(1)
                            if url:
                                parsed_successfully = True
                        else:
                            journey = content

                # 4. LLM Fallback (extremely robust for unstructured / conversational text)
                if not parsed_successfully:
                    try:
                        logger.info("Using LLM to extract target URL, credentials, and steps from unstructured input...")
                        from langchain_google_genai import ChatGoogleGenerativeAI
                        from config.settings import settings
                        
                        llm = ChatGoogleGenerativeAI(
                            model=settings.GEMINI_MODEL,
                            google_api_key=settings.GEMINI_API_KEY,
                            temperature=0.1
                        )
                        
                        extract_prompt = (
                            "You are an expert input extractor. Given a natural language instruction or unstructured text from a user, "
                            "extract the target URL, credentials (like username and password), and the user journey steps.\n\n"
                            f"Input message: {content}\n\n"
                            "Output MUST be a JSON object with the following keys:\n"
                            "- target_url (string, target website to navigate to. If not found, leave as empty string)\n"
                            "- credentials (object with keys like username, password. If not found, leave as empty object)\n"
                            "- user_journey_steps (list of strings representing the steps of the journey. If not found, leave as empty list)\n\n"
                            "Output ONLY the raw JSON object. Do not wrap in markdown or backticks."
                        )
                        
                        response = await llm.ainvoke(extract_prompt)
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
                            raw_journey = extracted_data.get("user_journey_steps") or extracted_data.get("journey") or ""
                            if isinstance(raw_journey, list):
                                journey = "\n".join(str(s) for s in raw_journey)
                            else:
                                journey = str(raw_journey)
                    except Exception as e:
                        logger.warning(f"LLM fallback extraction failed: {e}")

                # If we successfully parsed a target URL, stop searching older messages
                if url:
                    break

    if not url:
        raise ValueError("No target URL was provided in the input, and none could be extracted from the user message. Please provide a valid 'target_url'.")

    if is_already_planned:
        steps = raw_steps
    else:
        navigator = NavigatorAgent()
        steps = await navigator.aplan_steps(url, credentials, journey)
    
    return {
        "target_url": url,
        "credentials": credentials,
        "user_journey_steps": steps
    }

async def run_automation(state: AgentState) -> Dict[str, Any]:
    """Runs the Playwright journeys (Run 1 and Run 2)."""
    import asyncio
    logger.info("Node: run_automation starting...")
    url = state["target_url"]
    steps = state.get("user_journey_steps", [])

    recorder = PlaywrightBrowserRecorder(debug_mode=False)
    run_records = []
    error_log = []
    
    try:
        logger.info("Executing RUN 1...")
        run1_data = await asyncio.to_thread(recorder.execute_journey, url, steps, True)
        run_records.append({
            "run_id": 1,
            "network_requests": run1_data["network_requests"],
            "cookies": run1_data["cookies"],
            "local_storage": run1_data["local_storage"],
            "session_storage": run1_data["session_storage"],
            "screenshot_paths": []
        })
    except Exception as e:
        logger.error(f"Error during RUN 1: {e}")
        error_log.append(f"Run 1 failed: {str(e)}")

    if run_records:
        try:
            logger.info("Executing RUN 2...")
            run2_data = await asyncio.to_thread(recorder.execute_journey, url, steps, True)
            run_records.append({
                "run_id": 2,
                "network_requests": run2_data["network_requests"],
                "cookies": run2_data["cookies"],
                "local_storage": run2_data["local_storage"],
                "session_storage": run2_data["session_storage"],
                "screenshot_paths": []
            })
        except Exception as e:
            logger.error(f"Error during RUN 2: {e}")
            error_log.append(f"Run 2 failed: {str(e)}")
            
    return {
        "run_records": run_records,
        "error_log": error_log
    }

async def analyse_traffic(state: AgentState) -> Dict[str, Any]:
    """Runs TrafficAnalystAgent to perform differential correlation and dependency mapping."""
    from urllib.parse import urlparse, parse_qs
    logger.info("Node: analyse_traffic starting...")
    
    error_log = state.get("error_log", [])
    records = state.get("run_records", [])
    
    if len(records) < 2:
        error_summary = "\n- ".join(error_log) if error_log else "Unknown automation error."
        error_msg = f"""### ⚠️ Automation Execution Failed

Playwright was unable to complete the user journey runs successfully.

**Error Details**:
- {error_summary}

Please verify the user journey steps or selectors. If credentials are required, make sure the flow includes a login sequence.
"""
        return {
            "messages": [AIMessage(content=error_msg)]
        }

    run1 = {"network_requests": records[0]["network_requests"]}
    run2 = {"network_requests": records[1]["network_requests"]}

    analyst = TrafficAnalystAgent()
    correlations, dependencies = analyst.analyze_runs(run1, run2)

    # Detect parameterizable values in user journey steps
    parameterizable_candidates = []
    creds_values = {str(val).lower(): key for key, val in state.get("credentials", {}).items() if val}
    user_steps = state.get("user_journey_steps", [])
    run1_requests = run1.get("network_requests", [])

    for step in user_steps:
        if isinstance(step, dict) and step.get("action") in ["fill", "select"]:
            val = str(step.get("value", ""))
            selector = step.get("selector", "")
            if not val or not selector:
                continue
                
            val_lower = val.lower()
            
            # Check if this value matches credentials
            is_credential = False
            cred_name = None
            for cred_val, cred_key in creds_values.items():
                if cred_val == val_lower or val_lower in cred_val or cred_val in val_lower:
                    is_credential = True
                    cred_name = cred_key
                    break
            
            # Find network requests that contain this value
            propagations = []
            for req in run1_requests:
                req_url = req.get("url", "")
                req_method = req.get("method", "")
                
                # Check query params
                try:
                    parsed = urlparse(req_url)
                    qs = parse_qs(parsed.query)
                    for q_key, q_vals in qs.items():
                        if any(val_lower in str(qv).lower() for qv in q_vals):
                            propagations.append(f"`{req_method}` Query Parameter `{q_key}` in `{req_url}`")
                except Exception:
                    pass
                
                # Check post data
                post_data = req.get("post_data")
                if post_data:
                    try:
                        post_str = json.dumps(post_data) if isinstance(post_data, (dict, list)) else str(post_data)
                        if val_lower in post_str.lower():
                            propagations.append(f"`{req_method}` Post Body in `{req_url}`")
                    except Exception:
                        pass
                        
                # Check headers (excluding standard metadata headers)
                for h_key, h_val in req.get("headers", {}).items():
                    if val_lower in str(h_val).lower() and h_key.lower() not in ["host", "referer", "user-agent", "accept-encoding", "accept-language", "connection", "accept"]:
                        propagations.append(f"`{req_method}` Header `{h_key}` in `{req_url}`")
            
            parameterizable_candidates.append({
                "selector": selector,
                "value": val,
                "is_credential": is_credential,
                "credential_name": cred_name,
                "propagations": list(set(propagations))
            })

    # Format output markdown for the chat UI
    summary_markdown = f"""### 🚀 Correlation Analysis Report

Successfully analyzed **{len(run1['network_requests'])}** HTTP requests across multiple clean runs.

#### 🔍 Detected Correlations ({len(correlations)} candidates)
"""
    if correlations:
        summary_markdown += "\n| Request URL | Method | Location | Parameter Key | Run 1 Value | Run 2 Value |\n|---|---|---|---|---|---|\n"
        for corr in correlations:
            summary_markdown += f"| `{corr['request_url']}` | `{corr['method']}` | `{corr['location']}` | `{corr.get('key') or corr.get('json_path')}` | `{corr['run1_value']}` | `{corr['run2_value']}` |\n"
    else:
        summary_markdown += "\n*No dynamic correlations found between Run 1 and Run 2.*"

    summary_markdown += "\n\n#### 🔗 Dependency Graph\n"
    if dependencies:
        for dep in dependencies:
            summary_markdown += f"- **{dep['value_key']}** propagates from: \n  - `{dep['source_location']}` in `{dep['source_request']}`\n  - to `{dep['target_location']}` in `{dep['target_request']}`\n"
    else:
        summary_markdown += "\n*No dependency propagation paths identified.*"

    summary_markdown += "\n\n#### ⚙️ User Flow Parameterization Candidates\n"
    if parameterizable_candidates:
        summary_markdown += "The following input fields in the user flow contain values that can be parameterized (e.g., dynamically injected from environment, variables, or credentials):\n\n"
        for cand in parameterizable_candidates:
            status = f"🔐 Parameterized via Credentials (`{cand['credential_name']}`)" if cand["is_credential"] else "✏️ Hardcoded Value"
            summary_markdown += f"- **Selector**: `{cand['selector']}`\n"
            summary_markdown += f"  - **Current Value**: `{cand['value']}`\n"
            summary_markdown += f"  - **Status**: {status}\n"
            if cand["propagations"]:
                summary_markdown += "  - **Observed in Network Traffic**:\n"
                for prop in cand["propagations"]:
                    summary_markdown += f"    - {prop}\n"
            else:
                summary_markdown += "  - **Observed in Network Traffic**: None (client-side only or not sent in request context)\n"
    else:
        summary_markdown += "\n*No parameterizable inputs detected in the user flow.*"

    # Add system response message to the messages timeline
    response_message = AIMessage(content=summary_markdown)

    return {
        "correlations": correlations,
        "dependencies": dependencies,
        "messages": [response_message]
    }

# Build LangGraph workflow
workflow = StateGraph(AgentState)

workflow.add_node("plan_navigator_steps", plan_navigator_steps)
workflow.add_node("run_automation", run_automation)
workflow.add_node("analyse_traffic", analyse_traffic)

workflow.add_edge(START, "plan_navigator_steps")
workflow.add_edge("plan_navigator_steps", "run_automation")
workflow.add_edge("run_automation", "analyse_traffic")
workflow.add_edge("analyse_traffic", END)

# Compile the graph
graph = workflow.compile()
