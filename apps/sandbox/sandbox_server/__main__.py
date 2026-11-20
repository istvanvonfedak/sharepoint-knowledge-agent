import os

import uvicorn

from .app import app

# No access log: request paths carry session ids, which are as good as the session's files.
uvicorn.run(app, host=os.environ.get("SANDBOX_BIND_HOST", "127.0.0.1"), port=int(os.environ.get("SANDBOX_PORT", "8000")),
            access_log=False)
