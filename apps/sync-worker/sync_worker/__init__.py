from dotenv import load_dotenv

# Locally the worker reads its own .env.worker (its Entra app) and the shared .env (sharepoint_rag.config),
# never the mcp-server's .env.mcp with its secret.
load_dotenv(".env.worker")
