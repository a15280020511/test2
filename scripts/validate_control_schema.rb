require "yaml"

schema = YAML.load_file("deepseek_control_openapi.yaml")
raise "missing OpenAPI version" unless schema["openapi"] == "3.1.0"
raise "control schema version must be 1.1.0" unless schema.dig("info", "version") == "1.1.0"

paths = schema.fetch("paths")
operation_ids = []
paths.each_value do |path_item|
  next unless path_item.is_a?(Hash)
  path_item.each do |method, operation|
    next unless %w[get post put patch delete].include?(method.to_s.downcase)
    next unless operation.is_a?(Hash)
    operation_ids << operation["operationId"] if operation["operationId"]
  end
end

required = %w[
  dispatchDeepSeekControl
  dispatchDeepSeekRescue
  listDeepSeekControlRuns
  getDeepSeekControlRun
  getDeepSeekControlRunJobs
  listDeepSeekControlArtifacts
  downloadDeepSeekControlArtifact
]
missing = required - operation_ids
raise "missing control operationIds: #{missing.join(',')}" unless missing.empty?

control_path = "/repos/a15280020511/test2/actions/workflows/deepseek-control.yml/dispatches"
inputs = schema.dig(
  "paths", control_path, "post", "requestBody", "content", "application/json",
  "schema", "properties", "inputs"
)
raise "control dispatch inputs missing" unless inputs
operations = inputs.dig("properties", "operation", "enum")
expected_operations = %w[START STATUS CANCEL FORCE_CANCEL RESTART REVIEW DIAGNOSE REPAIR]
raise "control operations mismatch" unless operations == expected_operations

direct_work_dispatch = "/repos/a15280020511/test/actions/workflows/think-tank.yml/dispatches"
raise "control Action must not expose direct work dispatch" if paths.key?(direct_work_dispatch)

errors = []
walk = lambda do |node, path|
  case node
  when Hash
    type = node["type"]
    object_type = type == "object" || (type.is_a?(Array) && type.include?("object"))
    errors << "object schema missing properties at #{path.join('/')}" if object_type && !node.key?("properties")
    node.each { |key, value| walk.call(value, path + [key.to_s]) }
  when Array
    node.each_with_index { |value, index| walk.call(value, path + [index.to_s]) }
  end
end
walk.call(schema, [])
raise errors.join("\n") unless errors.empty?

control = File.read(".github/workflows/deepseek-control.yml")
raise "control workflow must use the mandatory priority controller" unless control.include?("scripts.deepseek_priority_control")
raise "control workflow must not bypass the priority controller" if control.include?("python -m scripts.cross_repo_control")
raise "control workflow must expose REPAIR" unless control.include?("- REPAIR")
raise "control workflow must use task-scoped concurrency" unless control.include?("deepseek-control-${{ inputs.task_id }}-${{ inputs.revision }}")
raise "control workflow monitor timeout is missing" unless control.include?("timeout-minutes: 210")
raise "control workflow must preserve a diagnostic log" unless control.include?("control.log")
raise "control workflow must redact bearer authorization tickets" unless control.include?("Redact bearer authorization ticket")
raise "control workflow must remove the raw ticket before publishing" unless control.include?("payload.pop('control_ticket', None)")
raise "control workflow must retain only a ticket hash receipt" unless control.include?("control_ticket_receipt")

priority = File.read("scripts/deepseek_priority_control.py")
required_priority_tokens = [
  "mandatory_entry_gate",
  "ENTRY_BLOCKED",
  "READY requires an effective plan",
  "automatic_repair",
  "REPAIR_PR_CREATED",
  "redact_ticket",
  "control_ticket_receipt",
  "redirect_stdout",
]
required_priority_tokens.each do |token|
  raise "priority controller contract missing #{token}" unless priority.include?(token)
end

rescue_workflow = File.read(".github/workflows/deepseek-rescue.yml")
raise "rescue workflow must expose force cancellation" unless rescue_workflow.include?("FORCE_CANCEL")
raise "rescue workflow must remain checkout-independent" if rescue_workflow.include?("actions/checkout")
raise "rescue workflow must use an independent inline implementation" unless rescue_workflow.include?("python - <<'PY'")

sentinel = File.read(".github/workflows/deepseek-control-failure-sentinel.yml")
raise "control sentinel must remain checkout-independent" if sentinel.include?("actions/checkout")
raise "control sentinel requires Actions write for bounded routing" unless sentinel.include?("actions: write")
raise "control sentinel must watch the production controller" unless sentinel.include?("- DeepSeek Control Plane")
raise "control sentinel must dispatch the highest DeepSeek supervisor" unless sentinel.include?("deepseek-supervisor.yml/dispatches")
raise "control sentinel must classify control-repository repair" unless sentinel.include?("CONTROL_REPOSITORY")
raise "control sentinel retry must be bounded" unless sentinel.include?("run_attempt") && sentinel.include?("< 2")
raise "control sentinel must not automatically resume paid work" unless sentinel.include?('"retry_dispatch_json": "{}"')

controller = File.read("scripts/cross_repo_control.py")
required_controller_tokens = [
  "CONTROL_TICKET_SECRET",
  "DUPLICATE_ACTIVE",
  "force-cancel",
  "monitor_run",
  "final result-quality and publication review",
  "create_repair_pr",
  "REPAIR_PR_CREATED",
]
required_controller_tokens.each do |token|
  raise "controller contract missing #{token}" unless controller.include?(token)
end

puts "Independent DeepSeek-first control-plane contract OK"
