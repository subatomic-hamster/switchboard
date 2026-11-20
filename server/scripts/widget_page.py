"""Write a page that holds one approval conversation in the ElevenLabs browser widget.

    uv run python -m scripts.widget_page 7          # request id 7; prints the file's path
    uv run python -m scripts.widget_page 7 --open   # also open it in the default browser

This is the fallback for when no phone line is available. It needs the server running, the
ElevenLabs agent created (`scripts.create_agent --apply --write-env`), and PUBLIC_BASE_URL
reachable from the internet, because the agent calls the voice tools on this server.

The page is written under `data/` (not committed) and is meant to be opened from disk. It is
kept apart from the dashboard because the widget is third-party script and the dashboard
shows the one-time word.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser

import httpx

from app.agent_config import widget_page
from app.config import REPO_ROOT, get_settings


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("request_id", help="the pending request, as shown on the dashboard")
    parser.add_argument("--open", action="store_true", help="open the page in the browser")
    args = parser.parse_args()
    settings = get_settings()
    url = f"http://127.0.0.1:{settings.port}/api/approvals/{args.request_id}/widget"
    try:
        response = httpx.get(url, timeout=10)
    except httpx.HTTPError as exc:
        raise SystemExit(f"The server did not answer on port {settings.port}: {exc}") from exc
    if response.status_code == 404:
        raise SystemExit(f"No widget for request {args.request_id}: {response.json()['detail']}")
    response.raise_for_status()
    config = response.json()
    target = REPO_ROOT / "data" / f"approval-widget-{args.request_id}.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        widget_page(config["agent_id"], config["dynamic_variables"]), encoding="utf-8"
    )
    print(target)
    if args.open:
        webbrowser.open(target.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
