"""Hermetic Hermes 0.18.2 CLI contract fixture used only by Goal L E2E."""

from __future__ import annotations

import json
import sys


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "version":
        print("Hermes Agent v0.18.2 (Nico contract fixture)")
        return 0
    if args[:2] == ["sessions", "export"]:
        session_id = args[args.index("--session-id") + 1]
        print(
            json.dumps(
                {
                    "id": session_id,
                    "messages": [
                        {"role": "assistant", "content": "Hermes adapter contract complete"}
                    ],
                }
            )
        )
        return 0
    if args and args[0] == "chat":
        session_id = args[args.index("--resume") + 1] if "--resume" in args else "goal-l-hermes"
        print("Hermes adapter contract complete")
        print(f"session_id: {session_id}", file=sys.stderr)
        return 0
    print("unsupported Hermes contract invocation", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
