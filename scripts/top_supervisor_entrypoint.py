from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from expert_team.deepseek_top_supervisor import run_deepseek_top_supervisor
from scripts.repair_utils import safe_operation_id, write_json


async def main() -> None:
    parser = argparse.ArgumentParser(description="Highest-level DeepSeek technical supervisor entrypoint")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--support-packet-json", required=True)
    args = parser.parse_args()

    operation_id = safe_operation_id(args.operation_id)
    output_dir = Path("artifacts") / operation_id
    metadata = {
        "operation_id": operation_id,
        "operation": "deepseek_supervisor",
        "steward_mode": "REPAIR",
        "status": "running",
    }
    write_json(output_dir / "metadata.json", metadata)

    try:
        result = await run_deepseek_top_supervisor(args.support_packet_json)
        result_path = output_dir / "deepseek_steward_result.json"
        write_json(result_path, result)
        metadata.update(
            {
                "status": "success",
                "readable_result_file": result_path.name,
                "result_file": result_path.name,
                "steward_decision": result.get("decision"),
                "steward_resume": result.get("resume"),
            }
        )
        write_json(output_dir / "metadata.json", metadata)
        print(json.dumps(metadata, ensure_ascii=False))
    except Exception as exc:
        metadata.update(
            {
                "status": "failure",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        write_json(output_dir / "metadata.json", metadata)
        print(json.dumps(metadata, ensure_ascii=False))
        raise


if __name__ == "__main__":
    asyncio.run(main())
