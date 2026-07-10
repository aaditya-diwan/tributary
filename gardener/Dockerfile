# Gardener as a Lambda container image (avoids zip-packaging psycopg's
# native wheel for Amazon Linux). Build from the repo root:
#   docker build -f gardener/Dockerfile -t tributary-gardener .
FROM public.ecr.aws/lambda/python:3.12
RUN pip install --no-cache-dir "psycopg[binary]>=3.1" "python-dotenv>=1.0"
COPY tributary/ ${LAMBDA_TASK_ROOT}/tributary/
COPY gardener/ ${LAMBDA_TASK_ROOT}/gardener/
CMD ["gardener.handler.lambda_handler"]
