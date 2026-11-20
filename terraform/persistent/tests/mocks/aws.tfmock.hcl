# Shared mock data for the persistent stack's tests (no AWS calls are made).

override_data {
  target = data.aws_caller_identity.current
  values = {
    account_id = "111122223333"
  }
}

# Mock providers can't import. This stands in for the console-made zero-spend budget that imports.tf
# adopts; remove it together with the import block.
override_resource {
  target = aws_budgets_budget.zero_spend
}

# The budget action validates the ARNs it is given, so mocks need realistic ones.
mock_resource "aws_iam_policy" {
  defaults = {
    arn = "arn:aws:iam::111122223333:policy/devops-platform-deny-new-spend"
  }
}

mock_resource "aws_iam_role" {
  defaults = {
    arn = "arn:aws:iam::111122223333:role/devops-platform-budget-action"
  }
}
