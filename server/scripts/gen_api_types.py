"""Regenerate `web/src/api-types.ts` from the FastAPI OpenAPI document.

    uv run python -m scripts.gen_api_types

Run it whenever `app/schemas.py` or a route signature changes. The TypeScript types are never
written by hand. `--check` exits non-zero if the committed file is out of date.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from app.config import REPO_ROOT, Settings
from app.db import Database
from app.main import create_app

WEB_DIR = REPO_ROOT / "web"
OUTPUT = WEB_DIR / "src" / "api-types.ts"


def openapi_document() -> str:
    # Built with blank settings and an in-memory database: the document depends on code only.
    app = create_app(Settings(_env_file=None), Database(None))
    return json.dumps(app.openapi(), indent=2, sort_keys=True)


def generate(target: Path) -> None:
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise SystemExit("pnpm was not found on PATH. Install it with `npm install -g pnpm`.")
    with tempfile.TemporaryDirectory() as tmp:
        spec = Path(tmp) / "openapi.json"
        spec.write_text(openapi_document(), encoding="utf-8")
        subprocess.run(  # noqa: S603 - fixed argument list, no shell
            [pnpm, "exec", "openapi-typescript", str(spec), "--output", str(target)],
            cwd=WEB_DIR,
            check=True,
        )


def main() -> int:
    if "--check" in sys.argv:
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / "api-types.ts"
            generate(fresh)
            current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
            if fresh.read_text(encoding="utf-8") != current:
                print("web/src/api-types.ts is out of date. Run `uv run python -m scripts.gen_api_types` in server/.")
                return 1
        print("web/src/api-types.ts is up to date.")
        return 0
    generate(OUTPUT)
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
