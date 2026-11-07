# TripMate AI: AWS Deployment Guide

Step-by-step guide to host TripMate AI on AWS with Docker, EC2, an Application Load Balancer, Route 53, ACM (HTTPS), RDS PostgreSQL, Secrets Manager and IAM. CloudFront (CDN) is covered as an optional step with an important caveat.

## 1. Target architecture

```text
                    Route 53  (tripmate.example.com  ->  ALIAS)
                        |
                  [CloudFront]  optional, see step 13
                        |
        +---------------+----------------+
        |  Application Load Balancer     |   public subnets (2 AZs)
        |  HTTPS :443 (ACM cert)         |   HTTP :80 -> redirect to 443
        +---------------+----------------+
                        | HTTP :8000
                 EC2 (Docker container)       SG: only from ALB
                 uvicorn app:app              IAM role: ECR pull + Secrets read
                        |
       +----------------+------------------+--------------------+
       |                |                  |                    |
  RDS PostgreSQL   Groq API (internet)  Tavily MCP (internet)  uvx aviationstack-mcp
  (private subnet) LangGraph LLM calls  remote HTTP MCP        + local weather MCP
                                                               (spawned inside the container)
```

Why each piece:

| Service | Purpose in this app |
|---|---|
| ECR | Stores the Docker image |
| EC2 | Runs the container. The app spawns MCP servers as subprocesses (`uvx`, a Python weather server), so it needs a normal long-lived host. Lambda is a poor fit. |
| ALB | HTTPS termination, health checks, a stable entry point |
| ACM | Free TLS certificate for the ALB |
| Route 53 | DNS name pointing at the ALB |
| RDS PostgreSQL | LangGraph `PostgresSaver` checkpointer (`DATABASE_URL`) |
| Secrets Manager | API keys and the DB URL, not baked into the image |
| IAM | Instance role so EC2 can pull from ECR and read secrets with no stored AWS keys |

## 2. Things in this repo to fix before deploying

These come from the current code and will bite you on AWS.

1. **`.dockerignore` does not exclude `.env` or `.git`.** `COPY . .` in the [Dockerfile](Dockerfile) would bake your real API keys into the image. Add to `.dockerignore`:
   ```text
   .env
   .git
   __pycache__
   excalidraw
   *.excalidraw
   ```
2. **The Dockerfile has no `uv`.** `mcp_client.py` launches the Aviationstack server with `uvx aviationstack-mcp`, which would fail in the container. Add after the `pip install -r requirements.txt` line:
   ```dockerfile
   RUN pip install --no-cache-dir uv
   ```
3. **Apple Silicon builds ARM images.** Your Mac builds `arm64` by default. Either build with `--platform linux/amd64` (used below) or run Graviton (`t4g`) instances.
4. **Requests are slow.** One plan runs several LLM and MCP calls and can take 30 to 90 seconds. The ALB idle timeout (default 60 s) must be raised. See step 10.
5. **Run a single uvicorn worker.** `backend.py` opens one module-level Postgres connection at import time. The Dockerfile `CMD` already does this, so don't add `--workers`.
6. **Groq limits.** The on-demand tier allows 8,000 tokens per minute, so concurrent users will get 429 errors. That is a Groq plan limit, not an AWS one.

## 3. Prerequisites

- An AWS account with admin access for the setup, and the AWS CLI configured (`aws configure`)
- Docker Desktop
- A domain name. It can be registered in Route 53 or elsewhere; both work.
- Your API keys: Groq, Tavily, AviationStack, OpenWeather

Set these shell variables once and reuse them in the commands below:

```bash
export AWS_REGION=us-east-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export DOMAIN=tripmate.example.com      # your real hostname
```

## 4. Network and security groups

The default VPC is fine to start with. It has public subnets in every AZ.

Create three security groups (EC2 console → Security Groups, or CLI):

| SG | Inbound rule |
|---|---|
| `tripmate-alb-sg` | TCP 80 and 443 from `0.0.0.0/0` |
| `tripmate-ec2-sg` | TCP 8000 **from `tripmate-alb-sg` only** |
| `tripmate-rds-sg` | TCP 5432 **from `tripmate-ec2-sg` only** |

Do not open port 22. Use SSM Session Manager for shell access (step 8 adds the permission).

## 5. RDS PostgreSQL

1. RDS → Create database → **PostgreSQL**, Free tier or Dev/Test template.
2. Instance class `db.t4g.micro`. Storage 20 GB gp3.
3. Set the master username and password, and the DB name, e.g. `travel_db`.
4. Connectivity: default VPC, **Public access: No**, security group `tripmate-rds-sg`.
5. Create it, then note the **endpoint**.

The connection string is:

```text
postgresql://USER:PASSWORD@<rds-endpoint>:5432/travel_db
```

`backend.py` appends `sslmode=require` automatically, which RDS supports. If the password has special characters, URL-encode them.

`PostgresSaver.setup()` creates its tables on first start, so no manual schema is needed.

## 6. Secrets Manager

Store all config as one JSON secret:

```bash
aws secretsmanager create-secret --name tripmate/prod --region $AWS_REGION \
  --secret-string '{
    "DATABASE_URL": "postgresql://USER:PASSWORD@<rds-endpoint>:5432/travel_db",
    "GROQ_API_KEY": "...",
    "TAVILY_API_KEY": "...",
    "AVIATIONSTACK_API_KEY": "...",
    "OPENWEATHER_API_KEY": "...",
    "DEFAULT_ORIGIN_IATA": "DAC"
  }'
```

## 7. Build and push the image to ECR

```bash
aws ecr create-repository --repository-name tripmate-ai --region $AWS_REGION

aws ecr get-login-password --region $AWS_REGION | \
  docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com

docker build --platform linux/amd64 -t tripmate-ai .
docker tag tripmate-ai:latest $ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/tripmate-ai:latest
docker push $ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/tripmate-ai:latest
```

Optional local check before pushing:

```bash
docker run --rm -p 8000:8000 --env-file .env tripmate-ai
curl http://localhost:8000/health
```

## 8. IAM role for the EC2 instance

1. IAM → Roles → Create role → trusted entity **EC2**. Name it `tripmate-ec2-role`.
2. Attach managed policies:
   - `AmazonEC2ContainerRegistryReadOnly` (pull the image)
   - `AmazonSSMManagedInstanceCore` (shell access without SSH)
3. Add an inline policy scoped to the one secret:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": "secretsmanager:GetSecretValue",
       "Resource": "arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:tripmate/prod-*"
     }]
   }
   ```
4. IAM creates the instance profile automatically when you create the role in the console.

## 9. Launch the EC2 instance

1. EC2 → Launch instance.
   - AMI: **Amazon Linux 2023** (x86_64, to match the image built above)
   - Type: `t3.small` (2 GB RAM). `t3.micro` can run out of memory with the MCP subprocesses.
   - No key pair is needed (use SSM).
   - Network: default VPC, **a public subnet**, auto-assign public IP **on**. The instance needs outbound internet for Groq, Tavily and the `uvx` download.
   - Security group: `tripmate-ec2-sg`
   - Advanced → IAM instance profile: `tripmate-ec2-role`
2. Paste this into **User data** (replace `ACCOUNT_ID` and the region if different):

```bash
#!/bin/bash
set -e
REGION=us-east-1
ACCOUNT_ID=<ACCOUNT_ID>
IMAGE=$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/tripmate-ai:latest

dnf install -y docker jq
systemctl enable --now docker

aws ecr get-login-password --region $REGION | \
  docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com

aws secretsmanager get-secret-value --secret-id tripmate/prod --region $REGION \
  --query SecretString --output text | \
  jq -r 'to_entries[] | "\(.key)=\(.value)"' > /opt/tripmate.env
chmod 600 /opt/tripmate.env

docker pull $IMAGE
docker run -d --name tripmate --restart unless-stopped \
  --env-file /opt/tripmate.env -p 8000:8000 $IMAGE
```

3. Wait about 2 minutes, then check from the instance (EC2 → Connect → Session Manager):
   ```bash
   curl localhost:8000/health
   docker logs tripmate
   ```

## 10. Target group and Application Load Balancer

1. EC2 → Target Groups → Create:
   - Type: Instances, protocol HTTP, port **8000**
   - Health check path: `/health`
   - Register the instance
2. EC2 → Load Balancers → Create **Application Load Balancer**:
   - Internet-facing, default VPC, **at least 2 subnets in different AZs**
   - Security group: `tripmate-alb-sg`
   - Listener HTTP:80 → **redirect to HTTPS:443**
   - Listener HTTPS:443 → forward to the target group, with the ACM certificate from step 11
3. **Raise the idle timeout.** In the ALB's attributes, set **Idle timeout to 180 seconds**. The default of 60 s will cut off long plan generations with a 504.

## 11. Route 53 and the ACM certificate

Do this before attaching the HTTPS listener in step 10.

1. **Hosted zone:** Route 53 → Hosted zones → Create (e.g. `example.com`). If the domain is registered elsewhere, copy the 4 NS records into that registrar.
2. **Certificate:** ACM (in the **same region as the ALB**) → Request public certificate for `tripmate.example.com`. Choose DNS validation, then click **Create records in Route 53**. Wait until the status is *Issued*.
3. **DNS record:** Route 53 → your zone → Create record:
   - Name `tripmate`, type **A**, **Alias** on
   - Target: your ALB (or the CloudFront distribution in step 13)

## 12. Verify

```bash
curl https://$DOMAIN/health
```

Then open `https://tripmate.example.com/` and generate a plan. Check that the target group shows the instance as **healthy**.

## 13. Optional: CloudFront (CDN)

The main caveat is that **CloudFront's origin response timeout defaults to 30 seconds and is capped at 60 seconds** unless you request a quota increase. `/api/travel` can take longer than that, so a CDN in front of the API would return 504 errors.

Recommended setups, in order of simplicity:

1. **Skip CloudFront at first.** The ALB, ACM and Route 53 already give you HTTPS and a clean domain.
2. **CloudFront for static files only.** Create a distribution with the ALB as origin, a behavior for `/static/*` with caching enabled, and a default behavior with **caching disabled** (`CachingDisabled` policy) and **AllViewer** origin request policy. Raise the origin response timeout to 60 s, and request a higher quota if needed.

If you use it, also:

- Request the ACM certificate for CloudFront in **us-east-1**, whatever your main region is.
- Add `tripmate.example.com` as an alternate domain name on the distribution.
- Point the Route 53 alias at the CloudFront distribution instead of the ALB.
- Optionally restrict the ALB so it only accepts traffic from CloudFront.

## 14. Deploying updates

```bash
docker build --platform linux/amd64 -t tripmate-ai .
docker tag tripmate-ai:latest $ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/tripmate-ai:latest
docker push $ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/tripmate-ai:latest
```

Then on the instance (Session Manager):

```bash
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
docker pull <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/tripmate-ai:latest
docker rm -f tripmate
docker run -d --name tripmate --restart unless-stopped \
  --env-file /opt/tripmate.env -p 8000:8000 \
  <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/tripmate-ai:latest
```

If you change a secret, re-run the `aws secretsmanager ... > /opt/tripmate.env` line from the user data first.

## 15. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Target group shows **unhealthy** | `tripmate-ec2-sg` doesn't allow 8000 from the ALB SG, or the container crashed (`docker logs tripmate`) |
| `exec format error` in the container | Image built for arm64 but instance is x86. Rebuild with `--platform linux/amd64`. |
| Container exits at start with a Postgres error | `tripmate-rds-sg` doesn't allow the EC2 SG, or `DATABASE_URL` is wrong |
| 504 after about 60 seconds | ALB idle timeout not raised (step 10), or CloudFront in front of the API |
| `uvx: not found` in logs | `uv` not installed in the Dockerfile (section 2, item 2) |
| Aviationstack `function_access_restricted` | AviationStack plan limit, not an AWS issue |
| Groq 413 or 429 | Groq on-demand token limit, not an AWS issue |

## 16. Cost and hardening notes

- Rough monthly cost in us-east-1: `t3.small` about $15, `db.t4g.micro` about $13, ALB about $17 to $20, plus small charges for Route 53, Secrets Manager and data transfer. The ALB is often the biggest fixed cost.
- The EC2 instance is in a public subnet only to reach the internet cheaply. For a stricter design, put it in a private subnet with a NAT gateway (about $32/month extra).
- Turn on RDS automated backups, and consider deletion protection.
- Add a CloudWatch alarm on the target group's `UnHealthyHostCount`, and ship container logs with the `awslogs` Docker log driver.
- Later options: an Auto Scaling Group with a launch template (reuse the user data above) for self-healing, or ECS on EC2/Fargate to avoid managing the host. Fargate would still need `uvx` available in the image.
- Terminate the instance, then delete the ALB, RDS, secret and ECR repository when you no longer need them, to stop billing.
