from app.api import create_app
from app.logging import configure_logging

configure_logging()
app = create_app()
