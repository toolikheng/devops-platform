# AWS test log

Measured results of live runs against the AWS account (ap-southeast-1). Offline checks (`make tf-check`, `make test-infra`) run on every push; this file records what only a real apply can show. Every run ends with a destroy and a clean `make cost-check`.

Costs are estimates from the published hourly rates, before tax. The Billing console is the source of truth.

## Ephemeral stack: network and compute (Phase C2)

### Steps

```bash
aws login --profile devops
make cost-check                                   # clean before starting
cd terraform/persistent && terraform plan -out=tfplan && terraform apply tfplan && cd -   # node role, profile, owner-cidr parameter
make owner-ip                                     # store this machine's IP as a /32
make cloud-up                                     # on-demand; Spot: make cloud-up CLOUD_ARGS='-var use_spot=true'
aws ssm start-session --target "$(cd terraform/envs/aws && terraform output -raw instance_id)"
```

Inside the Session Manager shell:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://169.254.169.254/latest/meta-data/instance-id     # 401: IMDSv1 refused
TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id   # the instance ID
getent hosts ssm.ap-southeast-1.amazonaws.com ghcr.io github.com                                     # DNS works
```

Then, from the laptop: a second `terraform plan` (must show no changes), `aws ec2 describe-instance-credit-specifications`, port probes against the public IP, and finally `make cloud-down`.

### Expected results

| Check | Expected |
|---|---|
| Apply | 9 resources added (VPC, subnet, internet gateway, route table, route, association, security group, key pair, instance) |
| SSM agent | `Online` in `aws ssm describe-instance-information`, with only the node role's 11 SSM-agent actions |
| IMDS | v1 (no token) returns 401; v2 returns the instance ID |
| DNS and outbound | names resolve through the VPC resolver; HTTPS and HTTP out work; other ports out are blocked |
| Inbound from the owner's IP | port 80 allowed; 22 and 6443 dropped |
| Credits | `standard` |
| Root volume | gp3, 20 GB, encrypted, deleted on termination |
| Second plan | no changes |
| Destroy | 9 destroyed; `make cost-check` exit 0 |

### Result log

| Date (MYT) | Run | Result | Notes |
|---|---|---|---|
| 2026-11-20 23:35 | Persistent stack additions | Pass | 4 added (node role, inline `ssm-agent` policy, instance profile, `/devops-platform/owner-cidr`). Second plan: no changes. The role has no managed policies attached. `make owner-ip` then set the parameter to the owner's /32 (version 2). |
| 2026-11-20 23:40–23:59 | t3.micro on-demand | Pass | 9 added in 23 s. SSM agent `Online` about a minute after launch with the minimal policy, so the managed policy was not needed. IMDSv1 401, IMDSv2 returned the instance ID, the role name was visible through IMDS. DNS through the VPC resolver resolved SSM, ghcr.io, github.com and archive.ubuntu.com. Outbound: ghcr.io:443 answered (401, as expected without a login), archive.ubuntu.com:80 returned 200, github.com:22 was blocked. From the owner's IP: port 80 "connection refused" (allowed, nothing listening yet), ports 22 and 6443 timed out although sshd was listening. Credits `standard`; root volume gp3 20 GB, encrypted with the AWS-managed key, deleted on termination; shutdown behaviour `terminate`; tags on the instance and the volume. Second plan: no changes. Destroy: 9 destroyed, cost-check clean. About USD 0.007. |
| 2026-11-21 00:03–00:11 | t3.micro Spot | **Fail** | The apply stopped after launching the instance: `UnsupportedOperation: Modifying 'instanceInitiatedShutdownBehavior' is not supported for spot instances`. The AWS provider sets that attribute with a second API call after launch, and AWS refuses it for Spot. The instance was in Terraform state (marked tainted), so `terraform destroy` removed everything; cost-check clean. AWS already reported `terminate` for the one-time Spot instance. Fixed by setting the attribute for on-demand nodes only (`2ee3cee`), with a test that launches a fresh Spot instance and checks it is left alone. About USD 0.002. |
| 2026-11-21 00:27–00:36 | t3.micro Spot, after the fix | Pass | 9 added. `InstanceLifecycle` spot; one-time request with interruption behaviour `terminate` and a price cap of USD 0.0132 (the on-demand price, because no max price is set). Shutdown behaviour `terminate`, set by AWS. SSM, IMDS, DNS and outbound results as in the on-demand run. Second plan: no changes. Destroy: 9 destroyed, Spot request cancelled, cost-check clean. About USD 0.002. |

Total for these runs: about USD 0.011 before tax.

Record each run here, including failures and what was changed.
