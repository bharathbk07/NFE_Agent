import os
import sys
from dotenv import load_dotenv

# 1. Load variables from local .env file
print("🔄 Loading environment variables from .env...")
load_dotenv()

# Helper terminal color codes
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"

# 2. Check for required credentials
gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
langchain_api_key = os.getenv("LANGCHAIN_API_KEY")
langchain_tracing = os.getenv("LANGCHAIN_TRACING_V2", "false").lower() == "true"
langchain_project = os.getenv("LANGCHAIN_PROJECT", "default")
model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

print("\n🔍 Checking configuration settings...")
config_ok = True

if not gemini_api_key:
    print(f"{RED}❌ Error: Missing GEMINI_API_KEY (or GOOGLE_API_KEY) in environment variable/file.{RESET}")
    config_ok = False
else:
    print(f"{GREEN}✔ Google AI Studio Key Found: {gemini_api_key[:8]}...{gemini_api_key[-4:] if len(gemini_api_key) > 8 else ''}{RESET}")

if not langchain_api_key:
    print(f"{YELLOW}⚠ Warning: LANGCHAIN_API_KEY not found. LangSmith tracing will be disabled.{RESET}")
else:
    print(f"{GREEN}✔ LangSmith API Key Found: {langchain_api_key[:8]}...{RESET}")

if not langchain_tracing:
    print(f"{YELLOW}⚠ Warning: LANGCHAIN_TRACING_V2 is not set to 'true'. Telemetry tracing is disabled.{RESET}")
else:
    print(f"{GREEN}✔ LangSmith Tracing Enabled (LANGCHAIN_TRACING_V2 = true){RESET}")
    print(f"📦 Traces will be uploaded to project: '{langchain_project}'")

if not config_ok:
    print(f"\n{RED}❌ Script aborted. Please fix configuration errors above and run again.{RESET}")
    sys.exit(1)

# 3. Attempt LangChain & Google GenAI Initialization
print("\n🤖 Initializing LangChain's ChatGoogleGenerativeAI model...")
try:
    from langchain_google_genai import ChatGoogleGenerativeAI
    
    # Initialize the Gemini chat model
    llm = ChatGoogleGenerativeAI(
        model=model_name,
        temperature=0.3,
        # Force LangChain's standard tracing metadata to label the LLM invocation neatly
    )
    print(f"{GREEN}✔ Successfully initialized model: '{model_name}'{RESET}")
except Exception as e:
    print(f"{RED}❌ Failed to import or initialize ChatGoogleGenerativeAI: {e}{RESET}")
    sys.exit(1)

# 4. Trigger test completion with user-defined tracing metadata
print(f"\n📨 Sending test query to Gemini ({model_name})...")
test_prompt = "Explain in one creative sentence how APIs act as bridges."

try:
    # We pass configuration with a custom run name so it stands out in LangSmith
    response = llm.invoke(
        test_prompt,
        config={
            "run_name": "test_setup_diagnostics",
            "tags": ["initial-setup-test", "gemini-verification"]
        }
    )
    
    print(f"\n{GREEN}✨ SUCCESS! Received Response:{RESET}")
    print(f"--------------------------------------------------")
    print(f"{response.content}")
    print(f"--------------------------------------------------")
    
    # Trace/Langsmith checking validation message
    if langchain_api_key and langchain_tracing:
        print(f"\n{GREEN}📈 Your trace was sent successfully to LangSmith!{RESET}")
        print(f"🔗 Go to your LangSmith Dashboard: https://smith.langchain.com/")
        print(f"   Look inside project: '{langchain_project}' for run: 'test_setup_diagnostics'.")
    else:
        print(f"\n{YELLOW}💡 Note: API worked, but LangSmith tracking config was incomplete or disabled.{RESET}")
        
except Exception as e:
    print(f"{RED}❌ API Call Failed: {e}{RESET}")
    print("\n💡 Troubleshooting Steps:")
    print("1. Double check that your API key is correctly typed in the .env file.")
    print("2. Ensure you have internet access and standard outbound connections aren't blocked.")
    print("3. Ensure that your Gemini API quota hasn't been exceeded.")