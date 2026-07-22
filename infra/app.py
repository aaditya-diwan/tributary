"""Tributary infrastructure as code (AWS CDK, Python).

One command deploys everything AWS-side: the Gardener Lambda (container
image) on an EventBridge schedule, and the dashboard on App Runner — with
both Docker images built and pushed automatically by CDK asset bundling.

    cd infra
    pip install -r requirements.txt
    cdk bootstrap                        # first time only, per account/region
    $env:DATABASE_URL = "<crdb-url>"     # PowerShell (export ... on bash)
    cdk deploy

Not automatable here: the CockroachDB cluster itself (ccloud CLI — see
docs/DEPLOY.md).

Note: DATABASE_URL lands as a plain environment variable on both services,
which is fine for a hackathon; the production path is Secrets Manager.
"""

import os

from aws_cdk import (
    App,
    CfnOutput,
    Duration,
    Stack,
    aws_apprunner as apprunner,
    aws_ecr_assets as assets,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
)
from constructs import Construct

REPO_ROOT = ".."  # infra/ lives inside the repo; assets build from the root


class TributaryStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            raise ValueError(
                "Set DATABASE_URL before deploying (ccloud cluster sql "
                "tributary --connection-url); see docs/DEPLOY.md"
            )

        # --- Gardener: Lambda container image + EventBridge schedule ---
        gardener = lambda_.DockerImageFunction(
            self,
            "Gardener",
            code=lambda_.DockerImageCode.from_image_asset(
                directory=REPO_ROOT, file="gardener/Dockerfile"
            ),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={"DATABASE_URL": database_url},
            description="Tends the tribe's memory: decays and retires stale lessons",
        )
        events.Rule(
            self,
            "GardenerTick",
            schedule=events.Schedule.rate(Duration.minutes(10)),
            targets=[targets.LambdaFunction(gardener)],
        )

        # --- Dashboard: image asset + App Runner service ---
        dashboard_image = assets.DockerImageAsset(
            self, "DashboardImage", directory=REPO_ROOT, file="dashboard/Dockerfile"
        )
        ecr_access = iam.Role(
            self,
            "AppRunnerEcrAccess",
            assumed_by=iam.ServicePrincipal("build.apprunner.amazonaws.com"),
        )
        dashboard_image.repository.grant_pull(ecr_access)

        dashboard = apprunner.CfnService(
            self,
            "Dashboard",
            source_configuration=apprunner.CfnService.SourceConfigurationProperty(
                authentication_configuration=(
                    apprunner.CfnService.AuthenticationConfigurationProperty(
                        access_role_arn=ecr_access.role_arn
                    )
                ),
                auto_deployments_enabled=False,
                image_repository=apprunner.CfnService.ImageRepositoryProperty(
                    image_identifier=dashboard_image.image_uri,
                    image_repository_type="ECR",
                    image_configuration=(
                        apprunner.CfnService.ImageConfigurationProperty(
                            port="8080",
                            runtime_environment_variables=[
                                apprunner.CfnService.KeyValuePairProperty(
                                    name="DATABASE_URL", value=database_url
                                )
                            ],
                        )
                    ),
                ),
            ),
            instance_configuration=apprunner.CfnService.InstanceConfigurationProperty(
                cpu="1024", memory="2048"
            ),
        )

        CfnOutput(self, "DashboardUrl", value=f"https://{dashboard.attr_service_url}")
        CfnOutput(self, "GardenerFunction", value=gardener.function_name)


app = App()
TributaryStack(app, "Tributary")
app.synth()
