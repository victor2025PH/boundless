# Run API: uvicorn api.main:app --host 0.0.0.0 --port 8001 (8000 often in use)
import os
import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8001"))
    uvicorn.run("api.main:app", host="0.0.0.0", port=port, reload=False)
