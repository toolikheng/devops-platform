# The zero-spend budget was created in the console before this project used Terraform. This block adopts
# it into state instead of deleting and recreating it, so the first-cent alert never has a gap. An import
# is a one-time move: remove this block after the apply that imports it.
import {
  to = aws_budgets_budget.zero_spend
  id = "${data.aws_caller_identity.current.account_id}:zero-spend"
}
