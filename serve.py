"""Production server. Run: .venv\\Scripts\\python serve.py  (then open http://localhost:8000)"""
import os

from waitress import serve

from app import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Corgi Markets running on http://localhost:{port}")
    serve(app, host=os.environ.get("HOST", "0.0.0.0"), port=port, threads=8)
