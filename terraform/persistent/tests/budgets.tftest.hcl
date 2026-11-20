# Budgets: the imported zero-spend alert, the monthly cap with alerts at 1/5/10 USD, and the automatic
# deny action at the cap.

mock_provider "aws" {
  source = "./tests/mocks"
}

variables {
  alert_email = "alerts@example.com"
}

run "zero_spend_budget_alerts_on_the_first_cent" {
  command = apply

  assert {
    condition = (
      aws_budgets_budget.zero_spend.name == "zero-spend" &&
      aws_budgets_budget.zero_spend.budget_type == "COST" &&
      aws_budgets_budget.zero_spend.time_unit == "MONTHLY" &&
      tonumber(aws_budgets_budget.zero_spend.limit_amount) == 1
    )
    error_message = "zero-spend must stay a monthly USD 1 cost budget, matching the one created in the console."
  }

  assert {
    condition = alltrue([
      for n in aws_budgets_budget.zero_spend.notification :
      n.notification_type == "ACTUAL" && n.comparison_operator == "GREATER_THAN" &&
      n.threshold_type == "ABSOLUTE_VALUE" && n.threshold == 0.01 &&
      toset(n.subscriber_email_addresses) == toset(["alerts@example.com"])
    ]) && length(aws_budgets_budget.zero_spend.notification) == 1
    error_message = "zero-spend must send one email when actual spend passes USD 0.01."
  }
}

run "monthly_cap_alerts_at_1_5_and_10_usd" {
  command = apply

  assert {
    condition = (
      aws_budgets_budget.monthly_cap.budget_type == "COST" &&
      aws_budgets_budget.monthly_cap.time_unit == "MONTHLY" &&
      tonumber(aws_budgets_budget.monthly_cap.limit_amount) == 15 &&
      aws_budgets_budget.monthly_cap.limit_unit == "USD"
    )
    error_message = "monthly-cap must be a monthly USD 15 cost budget."
  }

  assert {
    condition     = toset([for n in aws_budgets_budget.monthly_cap.notification : n.threshold]) == toset([1, 5, 10])
    error_message = "monthly-cap must alert at USD 1, 5 and 10."
  }

  assert {
    condition = alltrue([
      for n in aws_budgets_budget.monthly_cap.notification :
      n.notification_type == "ACTUAL" && n.comparison_operator == "GREATER_THAN" &&
      n.threshold_type == "ABSOLUTE_VALUE" &&
      toset(n.subscriber_email_addresses) == toset(["alerts@example.com"])
    ])
    error_message = "Every monthly-cap alert must be on actual spend, in USD, emailed to the alert address."
  }
}

run "budgets_ignore_credits" {
  command = apply

  assert {
    condition = (
      aws_budgets_budget.zero_spend.cost_types[0].include_credit == false &&
      aws_budgets_budget.monthly_cap.cost_types[0].include_credit == false
    )
    error_message = "Budgets must exclude credits so a credit can never hide real spend."
  }
}

run "budget_action_attaches_the_deny_policy_at_the_cap" {
  command = apply

  assert {
    condition = (
      aws_budgets_budget_action.deny_new_spend.budget_name == aws_budgets_budget.monthly_cap.name &&
      aws_budgets_budget_action.deny_new_spend.action_type == "APPLY_IAM_POLICY" &&
      aws_budgets_budget_action.deny_new_spend.approval_model == "AUTOMATIC" &&
      aws_budgets_budget_action.deny_new_spend.notification_type == "ACTUAL"
    )
    error_message = "The action must apply an IAM policy automatically when actual spend reaches the cap."
  }

  assert {
    condition = (
      aws_budgets_budget_action.deny_new_spend.action_threshold[0].action_threshold_type == "ABSOLUTE_VALUE" &&
      aws_budgets_budget_action.deny_new_spend.action_threshold[0].action_threshold_value == 15
    )
    error_message = "The action must fire at USD 15."
  }

  assert {
    condition = (
      one(aws_budgets_budget_action.deny_new_spend.definition[0].iam_action_definition).policy_arn == aws_iam_policy.deny_new_spend.arn &&
      toset(one(aws_budgets_budget_action.deny_new_spend.definition[0].iam_action_definition).users) == toset(["toolikheng-admin"]) &&
      aws_budgets_budget_action.deny_new_spend.execution_role_arn == aws_iam_role.budget_action.arn
    )
    error_message = "The action must attach the deny policy to toolikheng-admin, using the budget-action role."
  }
}
