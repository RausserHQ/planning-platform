# frozen_string_literal: true

# OpenProject 17.6.0-only Rails runner. Internal model use is deliberately
# version fenced; a mismatch is unsafe rather than a compatibility problem.
require "securerandom"

EXPECTED_OPENPROJECT_VERSION = "17.6.0"
abort "OpenProject version mismatch" unless OpenProject::VERSION.to_semver == EXPECTED_OPENPROJECT_VERSION

required = %w[
  PLANNING_PLATFORM_SERVICE_USER PLANNING_PLATFORM_PROJECT
  PLANNING_PLATFORM_ALERT_ASSIGNEE_LOGIN PLANNING_PLATFORM_WEBHOOK_URL
  OPENPROJECT_API_TOKEN_FILE OPENPROJECT_WEBHOOK_SECRET_FILE
]
abort "required configuration is absent" unless required.all? { |name| ENV[name].to_s.strip != "" }
token = File.read(ENV.fetch("OPENPROJECT_API_TOKEN_FILE")).strip
webhook_secret = File.read(ENV.fetch("OPENPROJECT_WEBHOOK_SECRET_FILE")).strip
abort "empty secret file" if token.empty? || webhook_secret.empty?
abort "API tokens are disabled" unless Setting.api_tokens_enabled?

TYPE_NAMES = %w[Idea Initiative Epic Story Task Decision Investigation Bug].freeze
STATUS_ROWS = {
  "Draft" => false, "Planning" => false,
  "Needs Input" => false, "Proposed" => false,
  "Ready" => false, "In Progress" => false,
  "Blocked" => false, "Review" => false,
  "Done" => true, "Superseded" => true, "Rejected" => true
}.freeze
FIELD_ROWS = {
  "Plan ID" => "string", "Node key" => "string", "Plan version" => "int",
  "Managed hash" => "string", "Repository" => "string", "Risk" => "string",
  "Agent eligible" => "bool", "Source requirements" => "text",
  "Planning commit" => "string", "Evidence state" => "string",
  "Alert fingerprint" => "string"
}.freeze
IDEA_HEADINGS = ["Problem", "Desired outcome", "Why now", "Constraints", "Non-goals", "Relevant repositories", "Existing context", "Success signal"].freeze
WEBHOOK_DESCRIPTION = "Planning Platform lifecycle events".freeze

def one!(scope, label)
  rows = scope.to_a
  abort "duplicate #{label}" if rows.length > 1
  return rows.first if rows.one?

  yield
end

ActiveRecord::Base.transaction do
  # xact-scoped lock prevents concurrent bootstrap races and rolls back every
  # partial mutation if any invariant fails.
  ActiveRecord::Base.connection.execute("SELECT pg_advisory_xact_lock(176001760)")
  abort "webhooks module unavailable" unless defined?(Webhooks::Webhook)
  project_identifier = ENV.fetch("PLANNING_PLATFORM_PROJECT")
  project = one!(Project.where("LOWER(identifier) = ?", project_identifier.downcase), "planning project") do
    Project.create!(
      name: ENV.fetch("PLANNING_PLATFORM_PROJECT_NAME", "Planning Platform"),
      identifier: project_identifier,
      workspace_type: "project",
      active: true,
      public: false
    )
  end
  abort "planning project is not an active private project workspace" unless \
    project.workspace_type == "project" && project.active? && !project.public?
  project.enabled_module_names = project.enabled_module_names | ["work_package_tracking"]
  abort "planning project work package module is disabled" unless \
    project.enabled_module_names.include?("work_package_tracking")

  service_login = ENV.fetch("PLANNING_PLATFORM_SERVICE_USER")
  service_user = one!(User.where("LOWER(login) = ?", service_login.downcase), "publisher service user") do
    password = "#{SecureRandom.urlsafe_base64(48)}Aa1!"
    User.create!(
      login: service_login,
      firstname: "Planning Platform",
      lastname: "Publisher",
      mail: ENV.fetch("PLANNING_PLATFORM_SERVICE_MAIL", "#{service_login}@planning-platform.invalid"),
      password: password,
      password_confirmation: password,
      status: User.statuses.fetch(:active),
      admin: false
    )
  end
  abort "publisher identity must be an active non-admin user" unless service_user.active? && !service_user.admin?
  alert_login = ENV.fetch("PLANNING_PLATFORM_ALERT_ASSIGNEE_LOGIN")
  alert_assignee = one!(
    User.where("LOWER(login) = ?", alert_login.downcase),
    "human alert assignee"
  ) { abort "human alert assignee is absent" }
  abort "alert assignee must be a distinct active person" unless alert_assignee.active? && alert_assignee != service_user

  api_token = Token::API.find_by_plaintext_value(token)
  if api_token.nil?
    Token::API.create!(
      user: service_user,
      value: Token::API.hash_function(token),
      data: { token_name: "Planning Platform publisher" }
    )
    api_token = Token::API.find_by_plaintext_value(token)
  end
  abort "publisher API token does not belong to service user" unless api_token&.user == service_user

  types = TYPE_NAMES.to_h do |name|
    type = one!(Type.where("LOWER(name) = ?", name.downcase), "Type #{name}") { Type.create!(name: name) }
    [name, type]
  end
  color = Color.first or abort "a configured Color is required"
  statuses = STATUS_ROWS.to_h do |name, is_closed|
    status = one!(Status.where("LOWER(name) = ?", name.downcase), "Status #{name}") do
      Status.create!(name: name, color: color, is_closed: is_closed, is_default: false, default_done_ratio: is_closed ? 100 : 0)
    end
    # Statuses are global. Reuse compatible standard-seed rows (notably
    # In progress and Rejected) without rewriting their color, progress ratio,
    # or global-default setting.
    abort "Status #{name} closed/open semantic mismatch" unless status.is_closed == is_closed
    [name, status]
  end
  fields = FIELD_ROWS.to_h do |name, field_format|
    field = one!(WorkPackageCustomField.where("LOWER(name) = ?", name.downcase), "WorkPackageCustomField #{name}") do
      WorkPackageCustomField.create!(name: name, field_format: field_format, is_required: false, multi_value: false)
    end
    abort "custom field #{name} format mismatch" unless field.field_format == field_format && !field.multi_value?
    [name, field]
  end
  project.types = (project.types.to_a + types.values).uniq
  project.work_package_custom_fields = (project.work_package_custom_fields.to_a + fields.values).uniq
  project.save!
  types.each_value do |type|
    type.custom_fields = (type.custom_fields.to_a + fields.values).uniq
    type.save!
  end

  role = one!(ProjectRole.where("LOWER(name) = ?", "planning platform publisher"), "ProjectRole") do
    ProjectRole.create!(name: "Planning Platform Publisher")
  end
  required_permissions = %i[view_work_packages add_work_packages edit_work_packages manage_work_package_relations manage_subtasks add_work_package_comments]
  role.permissions = required_permissions
  role.save!
  member = Member.find_or_initialize_by(project: project, principal: service_user)
  member.roles = [role]
  member.save!

  alert_role = one!(
    ProjectRole.where("LOWER(name) = ?", "planning platform alert assignee"),
    "alert assignee ProjectRole"
  ) { ProjectRole.create!(name: "Planning Platform Alert Assignee") }
  alert_role.permissions = %i[view_work_packages work_package_assigned]
  alert_role.save!
  Member
    .where(project: project)
    .joins(:member_roles)
    .where(member_roles: { role_id: alert_role.id })
    .where.not(user_id: alert_assignee.id)
    .distinct
    .find_each do |stale_member|
      stale_member.member_roles.where(role: alert_role).destroy_all
      stale_member.destroy! unless stale_member.member_roles.reload.exists?
    end
  alert_member = Member.find_or_initialize_by(project: project, principal: alert_assignee)
  if alert_member.new_record?
    alert_member.roles = [alert_role]
    alert_member.save!
  else
    alert_member.member_roles.only_inherited.where(role: alert_role).destroy_all
    direct_alert_roles = alert_member.member_roles.only_non_inherited.where(role: alert_role)
    abort "duplicate direct alert assignee role" if direct_alert_roles.count > 1
    alert_member.member_roles.create!(role: alert_role) unless direct_alert_roles.exists?
  end
  abort "alert assignee role mismatch" unless \
    alert_member.member_roles.only_non_inherited.where(role: alert_role).count == 1 &&
    alert_member.member_roles.only_inherited.where(role: alert_role).none?

  # Exhaustive status matrix for every publisher type/required status edge.
  types.each_value do |type|
    statuses.each_value do |status|
      statuses.each_value do |new_status|
        workflow = Workflow.find_or_initialize_by(role: role, type: type, old_status: status, new_status: new_status)
        workflow.author = false
        workflow.assignee = false
        workflow.save!
      end
    end
  end
  abort "workflow matrix mismatch" unless Workflow.where(role: role, type: types.values).count == types.length * statuses.length * statuses.length

  template = one!(Project.where("LOWER(identifier) = ?", "#{project.identifier}-idea-template".downcase), "Idea template project") do
    Project.create!(
      name: "#{project.name} Idea template",
      identifier: "#{project.identifier}-idea-template",
      workspace_type: "project",
      active: false,
      templated: true
    )
  end
  abort "Idea template project mismatch" unless \
    template.workspace_type == "project" && template.templated?
  template.enabled_module_names = template.enabled_module_names | ["work_package_tracking"]
  abort "Idea template work package module is disabled" unless \
    template.enabled_module_names.include?("work_package_tracking")
  template.types = (template.types.to_a + types.values).uniq
  template.work_package_custom_fields = (template.work_package_custom_fields.to_a + fields.values).uniq
  template.save!
  description = IDEA_HEADINGS.map { |heading| "## #{heading}\n" }.join("\n")
  idea_priority = one!(IssuePriority.where("LOWER(name) = ?", "normal"), "Priority Normal") { abort "a Normal priority is required" }
  idea = one!(WorkPackage.where(project: template, type: types.fetch("Idea"), subject: "Idea intake"), "Idea intake work package") do
    WorkPackage.create!(project: template, type: types.fetch("Idea"), status: statuses.fetch("Draft"), priority: idea_priority, author: service_user, subject: "Idea intake", description: description)
  end
  abort "Idea template mismatch" unless IDEA_HEADINGS.all? { |heading| idea.description.to_s.include?("## #{heading}") } && idea.author == service_user && idea.priority == idea_priority

  webhook = one!(Webhooks::Webhook.where("LOWER(name) = ?", "planning-platform-openproject-events"), "webhook") do
    Webhooks::Webhook.new(name: "planning-platform-openproject-events")
  end
  webhook.url = ENV.fetch("PLANNING_PLATFORM_WEBHOOK_URL")
  webhook.description = WEBHOOK_DESCRIPTION
  webhook.secret = webhook_secret
  webhook.enabled = true
  webhook.all_projects = false
  webhook.projects = [project]
  webhook.event_names = %w[work_package:created work_package:updated work_package_comment:comment work_package_comment:internal_comment]
  webhook.save!
  abort "webhook configuration mismatch" unless \
    webhook.description == WEBHOOK_DESCRIPTION && webhook.enabled? && webhook.projects == [project] && \
    webhook.event_names.sort == %w[work_package:created work_package:updated work_package_comment:comment work_package_comment:internal_comment]
end

puts "planning-platform OpenProject bootstrap verified"
