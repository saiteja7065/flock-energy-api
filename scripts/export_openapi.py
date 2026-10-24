"""Write the API's OpenAPI document to openapi.json (run from the repo root)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402

spec = create_app().openapi()
Path("openapi.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
print(f"wrote openapi.json ({len(spec['paths'])} paths, OpenAPI {spec['openapi']})")
