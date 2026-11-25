"""Run the GW Analytics Flask server with the Swati AI assistant API enabled."""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Load configuration before server and assistant modules are initialized.
from assistent import assistant_bp
from server import app

if "assistant" not in app.blueprints:
    app.register_blueprint(assistant_bp)

if __name__ == "__main__":
    if not os.environ.get("OPENAI_API_KEY"):
        logging.getLogger(__name__).warning(
            "OPENAI_API_KEY is not set; Swati will use the built-in local analytics engine."
        )

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("GW_FLASK_PORT", "5000")),
        debug=False,
    )
