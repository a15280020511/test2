from __future__ import annotations

import json
import os
from pathlib import Path


def main() -> int:
    # These imports prove that the actual expert-team requirements were installed
    # inside the temporary plugin virtual environment.
    import agent_framework  # noqa: F401
    import openrouter  # noqa: F401

    operation_id = os.environ.get("TEST2_PLUGIN_OPERATION_ID", "plugin-smoke")
    output = Path("artifacts") / operation_id / "plugin-import-smoke.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "status": "PLUGIN_IMPORT_OK",
                "plugin": os.environ.get("TEST2_ACTIVE_PLUGIN"),
                "operation_id": operation_id,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
