from __future__ import annotations

import unittest
from pathlib import Path


class AutomaticRecoveryContractTests(unittest.TestCase):
    def test_production_workflow_enforces_single_task_and_budgeted_execution(self) -> None:
        text = Path(".github/workflows/expert-team-production.yml").read_text(encoding="utf-8")
        self.assertIn("run-name: expert-${{ inputs.operation_id }}-${{ inputs.operation }}", text)
        self.assertIn("required: false", text.split("receipt_comment_id:", 1)[1].split("plan_json:", 1)[0])
        self.assertIn("Acquire global single-task execution lock", text)
        self.assertIn("Record idempotent duplicate dispatch", text)
        self.assertIn("Record BUSY without starting a paid task", text)
        self.assertIn("owner_operation_id == inputs.operation_id", text)
        self.assertIn("scripts.single_task_lock acquire", text)
        self.assertIn("scripts.run_with_heartbeat", text)
        self.assertIn("EXPERT_TEAM_MAX_BUDGET_USD", text)
        self.assertNotIn("group: expert-team-production", text)
        self.assertIn("Escalate failed production run to DeepSeek Top Supervisor", text)

    def test_one_step_controller_owns_normal_submission_and_startup_supervision(self) -> None:
        schema = Path("gpt_action_openapi.yaml").read_text(encoding="utf-8")
        workflow = Path(".github/workflows/operation-controller.yml").read_text(encoding="utf-8")
        controller = Path("scripts/operation_controller.py").read_text(encoding="utf-8")
        self.assertIn("operationId: submitExpertTeamOperation", schema)
        self.assertNotIn("operationId: createOperationReceipt", schema)
        self.assertIn("issue_comment", workflow)
        self.assertIn("scripts.operation_controller", workflow)
        self.assertIn("startup_timeout", controller)
        self.assertIn("MAX_CONTROLLER_INPUT_CHARS = 45_000", controller)

    def test_cancel_workflow_exists_and_does_not_route_user_cancel_to_deepseek(self) -> None:
        text = Path(".github/workflows/cancel-operation.yml").read_text(encoding="utf-8")
        self.assertIn("Cancel matching operation Runs and release lock", text)
        self.assertIn("scripts.cancel_operation", text)
        self.assertNotIn("deepseek-supervisor.yml", text)

    def test_independent_supervisor_publishes_after_resume_attempt(self) -> None:
        text = Path(".github/workflows/deepseek-supervisor.yml").read_text(encoding="utf-8")
        self.assertIn("group: deepseek-top-supervisor-${{ inputs.original_operation_id }}", text)
        self.assertIn("Run highest-level DeepSeek technical supervisor", text)
        self.assertIn("Plan validated recovery-reserve-compliant resume", text)
        resume_index = text.index("Execute one bounded original-operation resume")
        publish_index = text.index("Publish final GPT-readable supervisor result")
        self.assertLess(resume_index, publish_index)
        self.assertIn('DEEPSEEK_STEWARD_MAX_TOKENS: "12000"', text)

    def test_top_supervisor_override_is_schema_and_budget_validated(self) -> None:
        resume = Path("scripts/supervisor_resume.py").read_text(encoding="utf-8")
        self.assertIn("validate_execution_plan(effective_plan)", resume)
        self.assertIn('preflight_execution_plan(effective_plan, execution_phase="recovery")', resume)
        self.assertIn("may not increase the user's logical-task budget", resume)
        self.assertIn("logical_task_recovery_budget_already_consumed", resume)
        self.assertIn("effective_plan_source", resume)
        self.assertIn("deepseek_top_supervisor", resume)

    def test_runtime_plan_has_hard_budget_and_token_controls(self) -> None:
        schema = Path("execution_plan.schema.json").read_text(encoding="utf-8")
        entrypoint = Path("scripts/action_entrypoint.py").read_text(encoding="utf-8")
        runtime = Path("expert_team/dynamic_team.py").read_text(encoding="utf-8")
        budget = Path("expert_team/budget.py").read_text(encoding="utf-8")
        self.assertIn('"max_total_usd"', schema)
        self.assertIn('"recovery_reserve_ratio"', schema)
        self.assertIn('"max_completion_tokens"', schema)
        self.assertIn("preflight_execution_plan(payload)", entrypoint)
        self.assertIn('options={"max_tokens": max_completion_tokens}', runtime)
        self.assertIn("asyncio.wait_for", runtime)
        self.assertIn("partial_execution.json", runtime)
        self.assertIn("reserved_recovery_budget_usd", budget)
        self.assertIn("includes_all_declared_fallback_retries", budget)

    def test_action_schema_exposes_single_task_ledger_and_cancel(self) -> None:
        text = Path("gpt_action_openapi.yaml").read_text(encoding="utf-8")
        self.assertIn("version: 1.7.0", text)
        self.assertIn("operationId: submitExpertTeamOperation", text)
        self.assertIn("operationId: dispatchExpertTeamOperation", text)
        self.assertIn("operationId: cancelExpertTeamOperation", text)
        self.assertIn("operationId: dispatchDeepSeekSupervisor", text)
        self.assertIn("operationId: getOperationState", text)
        self.assertIn("operationId: getOperationCostPreflight", text)
        self.assertIn("operationId: getOperationAudit", text)
        self.assertNotIn("operationId: listExpertTeamRuns", text)
        self.assertNotIn("operationId: getOperationStatus", text)

    def test_internal_repair_allows_only_reserved_budget_retry(self) -> None:
        text = Path("scripts/managed_operation.py").read_text(encoding="utf-8")
        self.assertEqual(text.count("repair_command = _entrypoint_command("), 1)
        self.assertIn("_safe_unchanged_retry", text)
        self.assertIn("_recovery_reserve_allows_whole_retry", text)
        self.assertIn("Do not retry an unchanged 402", text)
        self.assertIn("recovery reserve is insufficient", text)
        self.assertIn("top-supervisor escalation", text)

    def test_repair_delivery_has_no_direct_main_and_protects_control_files(self) -> None:
        text = Path("scripts/repair_delivery.py").read_text(encoding="utf-8")
        self.assertIn("Direct push to main is forbidden", text)
        self.assertIn("pending_human_review", text)
        self.assertIn("SENSITIVE_PREFIXES", text)
        self.assertNotIn('git", "push", "origin", "HEAD:main', text)


if __name__ == "__main__":
    unittest.main()
