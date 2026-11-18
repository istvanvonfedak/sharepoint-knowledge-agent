from dotenv import load_dotenv

# Locally the mcp-server reads its own .env.mcp (its Entra app) and the shared .env (sharepoint_rag.config),
# never the worker's .env.worker.
load_dotenv(".env.mcp")
