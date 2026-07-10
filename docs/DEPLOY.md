# Deploying Tributary on AWS

Four pieces get deployed. The agents themselves run anywhere (your laptop for
the demo, ECS if you want them cloud-resident) — what lives on AWS is the
model layer (Bedrock), the Gardener (Lambda), and the dashboard (App Runner).

```
CockroachDB Cloud (on AWS us-east-1)  ← ccloud CLI
Bedrock: Claude + Titan embeddings    ← model access, IAM
Lambda + EventBridge: the Gardener    ← container image from gardener/Dockerfile
App Runner: the dashboard             ← container image from dashboard/Dockerfile
```

Prereqs: AWS CLI configured (`aws configure`), Docker Desktop running,
`ccloud` installed. Everything below assumes region `us-east-1` — keep the
cluster and Bedrock in the same region to minimize latency.

---

## 1. CockroachDB Cloud cluster (via ccloud CLI)

```powershell
ccloud auth login
ccloud cluster create tributary --cloud AWS --region us-east-1
ccloud cluster sql tributary --connection-url     # copy this into .env as DATABASE_URL
```

(Record this step — using the agent-ready ccloud CLI is one of your sponsor-tool
credits. The exact create flags vary by plan; `ccloud cluster create --help`
shows the current form, and the Cloud Console works too.)

Then create the schema and enable the MCP Server:

```powershell
python scripts/init_db.py
```

- If you get a TLS error, download the cluster CA cert per the Console's
  connection dialog and append `&sslrootcert=<path>` to `DATABASE_URL`.
- MCP Server: Cloud Console → your cluster → **MCP** → copy the config snippet
  into Claude Code (read-only mode is fine; it's for human curation).

## 2. Amazon Bedrock

1. Console → Bedrock → **Model access** → request: **Anthropic Claude Sonnet 4.5**
   and **Amazon Titan Text Embeddings V2** (usually instant).
2. Give your local credentials (and later the agents' execution role, if you
   move them to ECS) this policy:

```json
{ "Version": "2012-10-17",
  "Statement": [{ "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": "*" }] }
```

3. Smoke test: `python scripts/run_demo.py` locally — this exercises Bedrock,
   the vector index, and the whole learn/recall loop end to end.

## 3. One-time: ECR login + repos

```powershell
$ACCT = (aws sts get-caller-identity --query Account --output text)
$ECR  = "$ACCT.dkr.ecr.us-east-1.amazonaws.com"
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $ECR
aws ecr create-repository --repository-name tributary-dashboard --region us-east-1
aws ecr create-repository --repository-name tributary-gardener  --region us-east-1
```

## 4. Dashboard → App Runner (the public demo URL)

```powershell
docker build -f dashboard/Dockerfile -t tributary-dashboard .
docker tag tributary-dashboard "$ECR/tributary-dashboard:latest"
docker push "$ECR/tributary-dashboard:latest"
```

Then in the console (fastest path): **App Runner → Create service** →
source: the ECR image → port **8080** → add environment variable
`DATABASE_URL` → create. App Runner needs its default ECR access role —
accept the one it offers to create. Two minutes later you have a public
HTTPS URL; that's the "functional demo app" link for the submission.

Redeploying after changes: push the image again and hit **Deploy** on the
service (or enable automatic deployments).

## 5. Gardener → Lambda + EventBridge

```powershell
docker build -f gardener/Dockerfile -t tributary-gardener .
docker tag tributary-gardener "$ECR/tributary-gardener:latest"
docker push "$ECR/tributary-gardener:latest"

# Execution role (basic logging is all it needs)
aws iam create-role --role-name tributary-gardener-role `
  --assume-role-policy-document '{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"lambda.amazonaws.com\"},\"Action\":\"sts:AssumeRole\"}]}'
aws iam attach-role-policy --role-name tributary-gardener-role `
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

aws lambda create-function --function-name tributary-gardener `
  --package-type Image --code ImageUri="$ECR/tributary-gardener:latest" `
  --role "arn:aws:iam::${ACCT}:role/tributary-gardener-role" `
  --timeout 60 --memory-size 256 `
  --environment "Variables={DATABASE_URL=<your-crdb-url>}"

aws lambda invoke --function-name tributary-gardener out.json; cat out.json   # smoke test

# Schedule every 10 minutes
aws events put-rule --name tributary-gardener-tick --schedule-expression "rate(10 minutes)"
aws lambda add-permission --function-name tributary-gardener --statement-id eventbridge `
  --action lambda:InvokeFunction --principal events.amazonaws.com `
  --source-arn "arn:aws:events:us-east-1:${ACCT}:rule/tributary-gardener-tick"
aws events put-targets --rule tributary-gardener-tick `
  --targets "Id=1,Arn=arn:aws:lambda:us-east-1:${ACCT}:function:tributary-gardener"
```

(If the IAM role creation's escaped JSON fights PowerShell, create the role in
the console instead — it's two clicks with the Lambda service trust.)

## 6. (Optional) Agents on ECS Fargate

For the demo, running agents from your terminal is *better* — judges see them
live, and the point is that unrelated processes share memory. If you want
them cloud-resident anyway: build an image from the repo root that runs
`python -m agents.runner --agent agent-a`, push to ECR, and run it as a
Fargate task with `DATABASE_URL` in the environment and a task role that has
the Bedrock invoke policy from step 2. Two task definitions (agent-a /
agent-b) launched a minute apart make the same A-then-B demo, in the cloud.

## Checklist before submitting

- [ ] App Runner URL loads the dashboard and the live feed updates during a run
- [ ] `aws lambda invoke` on the Gardener returns `{"decayed": N, "retired": M}`
- [ ] MCP Server connected in Claude Code (both the managed one and `mcp_server/`)
- [ ] `.env` is NOT committed (it's gitignored — keep it that way)
- [ ] Architecture diagram includes: Bedrock, Lambda+EventBridge, App Runner,
      CockroachDB Cloud, MCP, and the agents
