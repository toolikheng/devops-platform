variable "alert_email" {
  description = "Email address that receives budget alerts and budget action notices."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be an email address."
  }
}

variable "admin_user_name" {
  description = "IAM user the budget action restricts when the monthly cap is reached."
  type        = string
  default     = "toolikheng-admin"
}

variable "monthly_budget_usd" {
  description = "Monthly cost cap in USD. At this amount the budget action blocks new spending."
  type        = number
  default     = 15

  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "monthly_budget_usd must be more than 0."
  }
}

variable "alert_thresholds_usd" {
  description = "Actual-spend amounts in USD that send an email alert each month."
  type        = list(number)
  default     = [1, 5, 10]

  validation {
    # A budget can have at most 5 notifications.
    condition = (
      length(var.alert_thresholds_usd) >= 1 && length(var.alert_thresholds_usd) <= 5 &&
      alltrue([for t in var.alert_thresholds_usd : t > 0 && t < var.monthly_budget_usd])
    )
    error_message = "alert_thresholds_usd needs 1 to 5 amounts, each above 0 and below monthly_budget_usd."
  }
}

variable "db_password_version" {
  description = "Version of PolicyPal's database password in SSM. Raise it by 1 to write a new random value (runbook: change it in Postgres first)."
  type        = number
  default     = 1

  validation {
    condition     = var.db_password_version >= 1 && floor(var.db_password_version) == var.db_password_version
    error_message = "db_password_version must be a whole number from 1."
  }
}

variable "admin_token_version" {
  description = "Version of PolicyPal's admin token in SSM. Raise it by 1 to write a new random value (rotation)."
  type        = number
  default     = 1

  validation {
    condition     = var.admin_token_version >= 1 && floor(var.admin_token_version) == var.admin_token_version
    error_message = "admin_token_version must be a whole number from 1."
  }
}
