import os

# sync_worker.config requires its own app credentials at import; tests never call Entra.
os.environ.setdefault("AZURE_TENANT_ID", "test-tenant")
os.environ.setdefault("SYNC_AZURE_CLIENT_ID", "test-client")
os.environ.setdefault("SYNC_AZURE_CERT_PATH", "unused-in-tests.pem")
