import os
import json
import logging
from langchain_core.messages import HumanMessage
from src.graph import graph

logger = logging.getLogger("TestRun")

def test_graph_execution():
    logger.info("Starting test of LangGraph compilation and flow execution...")
    
    # Initialize input state for the graph
    initial_state = {
        "target_url": "https://httpbin.org/forms/post",
        "credentials": {"username": "tester", "password": "passwordsuper"},
        "user_journey_steps": [
            "Navigate to target URL",
            "Fill out input[name=\"custname\"] with username input",
            "Fill out textarea[name=\"comments\"] with password input",
            "Click the button element",
            "Wait 2 seconds"
        ],
        "messages": [
            HumanMessage(content="Start the flow analysis journey.")
        ]
    }
    
    # Execute the graph asynchronously
    import asyncio
    logger.info("Invoking LangGraph compiled workflow asynchronously...")
    final_state = asyncio.run(graph.ainvoke(initial_state))
    
    assert "correlations" in final_state, "Correlations were not computed"
    assert "dependencies" in final_state, "Dependencies were not mapped"
    assert len(final_state["messages"]) > 1, "Expected AI response message in final state"
    
    ai_msg = final_state["messages"][-1]
    logger.info("Graph completed execution successfully!")
    logger.info(f"AI Output: {ai_msg.content}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test_graph_execution()
