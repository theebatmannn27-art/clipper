import os
import uvicorn

host = os.environ.get("HOST", "0.0.0.0")
port = int(os.environ.get("PORT", "8080"))
print(f"[clipper] serving on {host}:{port}")
uvicorn.run("app.main:app", host=host, port=port, log_level="info")
