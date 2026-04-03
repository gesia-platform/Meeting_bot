"""Command-line entrypoint for the local meeting AI runtime."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "local_meeting_ai_runtime.app:app",
        host=os.getenv("DELEGATE_HOST", "127.0.0.1"),
        port=int(os.getenv("DELEGATE_PORT", "9010")),
        reload=False,
    )


if __name__ == "__main__":
    main()
