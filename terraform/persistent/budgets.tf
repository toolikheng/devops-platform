# Cost budgets email when actual spend passes a threshold. Both exclude credits, so a credit can never
# hide real spend. Budgets are free (and so are the first two with an action).

resource "aws_budgets_budget" "zero_spend" {
  name         = "zero-spend"
  budget_type  = "COST"
  limit_amount = "1.0"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_types {
    include_credit = false
  }

  # The first cent of the month: "something started billing".
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 0.01
    threshold_type             = "ABSOLUTE_VALUE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }
}

resource "aws_budgets_budget" "monthly_cap" {
  name        = "monthly-cap"
  budget_type = "COST"
  # AWS stores amounts with one decimal ("15.0"); matching it avoids a permanent diff.
  limit_amount = format("%.1f", var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_types {
    include_credit = false
  }

  dynamic "notification" {
    for_each = var.alert_thresholds_usd

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "ABSOLUTE_VALUE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alert_email]
    }
  }
}
