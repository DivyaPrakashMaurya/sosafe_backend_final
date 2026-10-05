"""Kept only so a server still started the old way (`uvicorn main:app`) runs the new app.

The entry point is app.main:create_app (see README.md and render.yaml). Once the
host starts it that way, this file can go.
"""

from app.main import create_app

app = create_app()
