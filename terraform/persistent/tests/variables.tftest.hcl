# Input rules and tags for the persistent stack. The mock provider means no AWS calls are made.

mock_provider "aws" {
  source = "./tests/mocks"
}

variables {
  alert_email = "alerts@example.com"
}

run "alert_email_must_look_like_an_email_address" {
  command = plan
  variables {
    alert_email = "not-an-email"
  }
  expect_failures = [var.alert_email]
}

run "alert_thresholds_must_be_below_the_monthly_budget" {
  command = plan
  variables {
    alert_thresholds_usd = [1, 5, 20]
  }
  expect_failures = [var.alert_thresholds_usd]
}

run "every_resource_gets_the_project_tags" {
  command = plan
  assert {
    condition = (
      length(output.default_tags) == 4 &&
      output.default_tags["Project"] == "devops-platform" &&
      output.default_tags["Owner"] == "toolikheng" &&
      output.default_tags["ManagedBy"] == "terraform" &&
      output.default_tags["Stack"] == "persistent"
    )
    error_message = "Default tags must be exactly Project, Owner, ManagedBy and Stack=persistent."
  }
}
