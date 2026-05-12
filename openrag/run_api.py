"""
Run the OpenRag FastAPI application

Usage:
    python run_api.py

The API will be available at:
    - http://localhost:8000
    - API docs: http://localhost:8000/docs
    - ReDoc: http://localhost:8000/redoc
"""

import os
from dotenv import load_dotenv
import uvicorn

# 加载 .env 文件
load_dotenv()

if __name__ == "__main__":
    # Check if running in production mode
    app_env = os.getenv("APP_ENV", "development")
    is_production = app_env == "production"

    # Windows doesn't support reload mode well with multiprocessing
    # Use --reload flag manually on Linux/Mac if needed
    use_reload = not is_production and os.name != 'nt'

    uvicorn.run(
        "openrag.api.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=use_reload,  # Disable reload on Windows
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        workers=1  # Always use single worker (reload mode requires workers=1)
    )
