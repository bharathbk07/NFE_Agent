import os
import sys
import json
import logging
from config.settings import settings
from config.observability import initialize_observability
from src.agents.navigator_agent import NavigatorAgent
from src.tools.playwright_tool import PlaywrightBrowserRecorder
from src.agents.analyst_agent import TrafficAnalystAgent

# Set up logging
logging.basicConfig(
    level=logging.INFO if not settings.DEBUG_MODE else logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("BrowserFlowAnalyzer")

def run_analyzer(
    url: str,
    credentials: dict,
    journey_description: str,
    output_filepath: str = "correlation_metadata.json"
):
    initialize_observability()
    
    logger.info("Initializing Agent 1: Navigator...")
    navigator = NavigatorAgent()
    
    # 1. Translate NL description into structured Playwright steps
    steps = navigator.plan_steps(url, credentials, journey_description)
    logger.info(f"Generated steps:\n{json.dumps(steps, indent=2)}")
    
    recorder = PlaywrightBrowserRecorder(debug_mode=settings.DEBUG_MODE)
    
    # 2. Run 1: Primary execution
    logger.info("Starting RUN 1: Executing complete journey and capturing traffic...")
    run1_data = recorder.execute_journey(url, steps, clear_context=True)
    logger.info(f"RUN 1 Complete. Captured {len(run1_data['network_requests'])} requests.")
    
    # 3. Run 2: Fresh execution
    logger.info("Starting RUN 2: Executing journey again with cleared context...")
    run2_data = recorder.execute_journey(url, steps, clear_context=True)
    logger.info(f"RUN 2 Complete. Captured {len(run2_data['network_requests'])} requests.")
    
    # 4. Agent 2: Traffic Analyst
    logger.info("Initializing Agent 2: Traffic Analyst...")
    analyst = TrafficAnalystAgent()
    correlations, dependencies = analyst.analyze_runs(run1_data, run2_data)
    
    # 5. Build final report payload
    result = {
        "target_url": url,
        "journey_steps": steps,
        "correlations": correlations,
        "dependency_graph": dependencies,
        "session_summary": {
            "run1": {
                "request_count": len(run1_data["network_requests"]),
                "cookies_captured": len(run1_data["cookies"]),
                "local_storage_keys": list(run1_data["local_storage"].keys())
            },
            "run2": {
                "request_count": len(run2_data["network_requests"]),
                "cookies_captured": len(run2_data["cookies"]),
                "local_storage_keys": list(run2_data["local_storage"].keys())
            }
        }
    }
    
    # Write to local file
    with open(output_filepath, "w") as f:
        json.dump(result, f, indent=2)
        
    logger.info(f"Successfully generated structured metadata and saved to {output_filepath}")
    return result

if __name__ == "__main__":
    # If run directly, run a mock or check argument values
    # Users can pass configuration files or command line parameters
    target_url = "https://httpbin.org/forms/post"
    test_credentials = {"username": "testuser", "password": "securepassword123"}
    journey = """
    1. Navigate to target URL
    2. Fill out user name field using credentials username
    3. Fill out password field using credentials password
    4. Wait 1 second
    5. Click the submit button
    6. Wait for network requests to complete
    """
    
    if len(sys.argv) > 1:
        # Simple JSON input file execution
        config_path = sys.argv[1]
        with open(config_path, "r") as f:
            cfg = json.load(f)
            target_url = cfg.get("url", target_url)
            test_credentials = cfg.get("credentials", test_credentials)
            journey = cfg.get("journey", journey)
            
    run_analyzer(target_url, test_credentials, journey)
