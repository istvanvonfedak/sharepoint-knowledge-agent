import os

import uvicorn

from .app import app

uvicorn.run(app, host=os.environ.get("INFERENCE_BIND_HOST", "127.0.0.1"), port=int(os.environ.get("INFERENCE_PORT", "8000")))
