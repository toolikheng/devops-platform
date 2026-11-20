# Shared mock data for the persistent stack's tests (no AWS calls are made).

override_data {
  target = data.aws_caller_identity.current
  values = {
    account_id = "111122223333"
  }
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
