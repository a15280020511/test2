require "yaml"

schema = YAML.load_file("gpt_action_openapi.yaml")
raise "missing openapi" unless schema["openapi"]
raise "Action schema version must be 1.5.0" unless schema.dig("info", "version") == "1.5.0"

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
  getOpenRouterModels
  getExecutionPlanSchema
  getDeepSeekStewardPolicy
  getActionRecoveryPolicy
  dispatchExpertTeamOperation
  getCurrentOperationStatus
  getExpertTeamRun
  getExpertTeamRunJobs
  listExpertTeamRunArtifacts
  getExpertTeamResult
  getDeepSeekStewardResult
  getAutoRepairResult
  getExpertTeamOperationMetadata
  downloadExpertTeamArtifact
]
missing = required_operation_ids - operation_ids
raise "missing operationIds: #{missing.join(',')}" unless missing.empty?
raise "listExpertTeamRuns must not be exposed as a normal control-plane Action" if operation_ids.include?("listExpertTeamRuns")
raise "getOperationStatus must not remain exposed; use permanent getCurrentOperationStatus" if operation_ids.include?("getOperationStatus")

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

runtime_paths = [
  "/repos/a15280020511/test2/contents/runtime_results/model_intelligence_latest.json",
  "/repos/a15280020511/test2/contents/runtime_results/current_operation_status.json",
  "/repos/a15280020511/test2/contents/runtime_results/{operation_id}/expert_team_result.json",
  "/repos/a15280020511/test2/contents/runtime_results/{operation_id}/deepseek_steward_result.json",
  "/repos/a15280020511/test2/contents/runtime_results/{operation_id}/auto_repair_result.json",
  "/repos/a15280020511/test2/contents/runtime_results/{operation_id}/metadata.json",
]

runtime_paths.each do |path|
  operation = schema.dig("paths", path, "get")
  raise "missing runtime-result GET operation: #{path}" unless operation
  ref = operation.fetch("parameters").find { |parameter| parameter["name"] == "ref" }
  raise "missing ref parameter: #{path}" unless ref
  raise "ref must be required: #{path}" unless ref["required"] == true
  enum = ref.dig("schema", "enum")
  raise "ref must be pinned to runtime-results: #{path}" unless enum == ["runtime-results"]
end

current_path = "/repos/a15280020511/test2/contents/runtime_results/current_operation_status.json"
current = schema.dig("paths", current_path, "get")
raise "permanent current status endpoint missing" unless current
raise "permanent current status must expose HTTP 200" unless current.dig("responses", "200")

history_path = "/repos/a15280020511/test2/contents/runtime_results/status/{operation_id}.json"
raise "per-operation history status must not be exposed as primary Action control plane" if schema.dig("paths", history_path)

main_paths = [
  "/repos/a15280020511/test2/contents/execution_plan.schema.json",
  "/repos/a15280020511/test2/contents/DEEPSEEK_STEWARD.md",
  "/repos/a15280020511/test2/contents/ACTION_RECOVERY.md",
]

main_paths.each do |path|
  operation = schema.dig("paths", path, "get")
  raise "missing main-branch GET operation: #{path}" unless operation
  ref = operation.fetch("parameters").find { |parameter| parameter["name"] == "ref" }
  raise "missing ref parameter: #{path}" unless ref
  raise "ref must be required: #{path}" unless ref["required"] == true
  enum = ref.dig("schema", "enum")
  raise "ref must be pinned to main: #{path}" unless enum == ["main"]
end

puts "GPT Action OpenAPI strict permanent-control-plane contract OK"
