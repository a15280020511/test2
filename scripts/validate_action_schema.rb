require "yaml"

schema = YAML.load_file("gpt_action_openapi.yaml")
raise "missing openapi" unless schema["openapi"]
raise "Action schema version must be 1.7.0" unless schema.dig("info", "version") == "1.7.0"

schemas = schema.dig("components", "schemas")
raise "components.schemas must be an object" unless schemas.is_a?(Hash)

http_methods = %w[get post put patch delete options head trace]
operation_ids = []
description_errors = []

schema.fetch("paths").each do |path, path_item|
  next unless path_item.is_a?(Hash)
  path_item.each do |method, operation|
    next unless http_methods.include?(method.to_s.downcase)
    next unless operation.is_a?(Hash)
    operation_id = operation["operationId"]
    operation_ids << operation_id if operation_id
    description = operation["description"]
    if description.is_a?(String) && description.length > 300
      description_errors << "#{method.upcase} #{path} operationId=#{operation_id}: description length #{description.length} exceeds 300"
    end
  end
end
raise description_errors.join("\n") unless description_errors.empty?

required_operation_ids = %w[
  createOperationReceipt
  getOpenRouterModels
  getExecutionPlanSchema
  getDeepSeekStewardPolicy
  getActionRecoveryPolicy
  dispatchExpertTeamOperation
  cancelExpertTeamOperation
  dispatchDeepSeekSupervisor
  getOperationState
  getCurrentOperationStatus
  getExpertTeamRun
  getExpertTeamRunJobs
  listExpertTeamRunArtifacts
  getExpertTeamResult
  getDeepSeekStewardResult
  getAutoRepairResult
  getExpertTeamOperationMetadata
  getOperationCostPreflight
  getOperationAudit
  getCancellationResult
  downloadExpertTeamArtifact
]
missing = required_operation_ids - operation_ids
raise "missing operationIds: #{missing.join(',')}" unless missing.empty?
raise "listExpertTeamRuns must not be exposed" if operation_ids.include?("listExpertTeamRuns")
raise "legacy getOperationStatus must not be exposed" if operation_ids.include?("getOperationStatus")

errors = []
walk = lambda do |node, path|
  case node
  when Hash
    type = node["type"]
    object_type = type == "object" || (type.is_a?(Array) && type.include?("object"))
    if object_type && !node.key?("properties")
      errors << "object schema missing properties at #{path.join('/')}"
    end
    node.each { |key, value| walk.call(value, path + [key.to_s]) }
  when Array
    node.each_with_index { |value, index| walk.call(value, path + [index.to_s]) }
  end
end
walk.call(schema, [])
raise errors.join("\n") unless errors.empty?

resolve_parameter = lambda do |parameter|
  next parameter unless parameter.is_a?(Hash) && parameter["$ref"]
  prefix = "#/components/parameters/"
  ref = parameter["$ref"]
  raise "unsupported parameter ref: #{ref}" unless ref.start_with?(prefix)
  name = ref.delete_prefix(prefix)
  schema.dig("components", "parameters", name) || raise("missing parameter component: #{name}")
end

find_parameter = lambda do |operation, name|
  (operation["parameters"] || []).map { |parameter| resolve_parameter.call(parameter) }.find { |parameter| parameter["name"] == name }
end

runtime_paths = [
  "/repos/a15280020511/test2/contents/runtime_results/model_intelligence_latest.json",
  "/repos/a15280020511/test2/contents/runtime_results/operations/{operation_id}/state.json",
  "/repos/a15280020511/test2/contents/runtime_results/current_operation_status.json",
  "/repos/a15280020511/test2/contents/runtime_results/{operation_id}/expert_team_result.json",
  "/repos/a15280020511/test2/contents/runtime_results/{operation_id}/deepseek_steward_result.json",
  "/repos/a15280020511/test2/contents/runtime_results/{operation_id}/auto_repair_result.json",
  "/repos/a15280020511/test2/contents/runtime_results/{operation_id}/metadata.json",
  "/repos/a15280020511/test2/contents/runtime_results/{operation_id}/cost_preflight.json",
  "/repos/a15280020511/test2/contents/runtime_results/{operation_id}/operation_audit.json",
  "/repos/a15280020511/test2/contents/runtime_results/{operation_id}/cancellation_result.json",
]

runtime_paths.each do |path|
  operation = schema.dig("paths", path, "get")
  raise "missing runtime-result GET operation: #{path}" unless operation
  ref = find_parameter.call(operation, "ref")
  raise "missing ref parameter: #{path}" unless ref
  raise "ref must be required: #{path}" unless ref["required"] == true
  raise "ref must be pinned to runtime-results: #{path}" unless ref.dig("schema", "enum") == ["runtime-results"]
end

operation_state_path = "/repos/a15280020511/test2/contents/runtime_results/operations/{operation_id}/state.json"
raise "operation state endpoint missing HTTP 200" unless schema.dig("paths", operation_state_path, "get", "responses", "200")

current_path = "/repos/a15280020511/test2/contents/runtime_results/current_operation_status.json"
raise "current dashboard endpoint missing" unless schema.dig("paths", current_path, "get", "responses", "200")

receipt_path = "/repos/a15280020511/test2/issues/15/comments"
raise "durable receipt endpoint missing" unless schema.dig("paths", receipt_path, "post", "responses", "201")

production_path = "/repos/a15280020511/test2/actions/workflows/expert-team-production.yml/dispatches"
production_inputs = schema.dig("paths", production_path, "post", "requestBody", "content", "application/json", "schema", "properties", "inputs")
raise "production dispatch inputs missing" unless production_inputs
required_inputs = production_inputs["required"] || []
raise "production dispatch must require operation_id" unless required_inputs.include?("operation_id")
raise "production dispatch must require operation" unless required_inputs.include?("operation")
raise "receipt_comment_id must be optional because server fallback is mandatory" if required_inputs.include?("receipt_comment_id")

cancel_path = "/repos/a15280020511/test2/actions/workflows/cancel-operation.yml/dispatches"
raise "cancel operation dispatch missing" unless schema.dig("paths", cancel_path, "post")

supervisor_path = "/repos/a15280020511/test2/actions/workflows/deepseek-supervisor.yml/dispatches"
raise "independent DeepSeek supervisor dispatch missing" unless schema.dig("paths", supervisor_path, "post")

main_paths = [
  "/repos/a15280020511/test2/contents/execution_plan.schema.json",
  "/repos/a15280020511/test2/contents/DEEPSEEK_STEWARD.md",
  "/repos/a15280020511/test2/contents/ACTION_RECOVERY.md",
]
main_paths.each do |path|
  operation = schema.dig("paths", path, "get")
  raise "missing main-branch GET operation: #{path}" unless operation
  ref = find_parameter.call(operation, "ref")
  raise "missing ref parameter: #{path}" unless ref
  raise "ref must be required: #{path}" unless ref["required"] == true
  raise "ref must be pinned to main: #{path}" unless ref.dig("schema", "enum") == ["main"]
end

puts "GPT Action OpenAPI v1.7 single-task, budget, cancellation, and DeepSeek contract OK"
