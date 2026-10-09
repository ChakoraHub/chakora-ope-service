"""Application entrypoint alias for ope_service:app (standardized microservice structure)."""
from ope_service import app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("ope_service:app", host="0.0.0.0", port=8500, reload=True)
