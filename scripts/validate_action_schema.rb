require "yaml"

schema = YAML.load_file("gpt_action_openapi.yaml")
raise "missing openapi" unless schema["openapi"]

schemas = schema.dig("components", "schemas")
raise "components.schemas must be an object" unless schemas.is_a?(Hash)

operation_ids = schema.fetch("paths").values.flat_map do |path_item|
  path_item.values.filter_map { |operation| operation.is_a?(Hash) ? operation["operationId"] : nil }
end

required_operation_ids = %w[
  getOpenRouterModels
  getExecutionPlanSchema
  getDeepSeekStewardPolicy
  getActionRecoveryPolicy
  dispatchExpertTeamOperation
  listExpertTeamRuns
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

puts "GPT Action OpenAPI strict contract OK"
