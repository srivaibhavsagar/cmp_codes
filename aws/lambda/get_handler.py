import json

# In-memory sample data for CMP testing
INSTANCES = [
    {
        "id": "i-001",
        "name": "web-server-01",
        "cloud": "aws",
        "type": "t3.medium",
        "region": "us-east-1",
        "status": "running",
        "cost_per_hour": 0.0416,
    },
    {
        "id": "i-002",
        "name": "db-server-01",
        "cloud": "aws",
        "type": "r5.large",
        "region": "us-west-2",
        "status": "running",
        "cost_per_hour": 0.126,
    },
    {
        "id": "i-003",
        "name": "dev-box-01",
        "cloud": "azure",
        "type": "Standard_B2s",
        "region": "eastus",
        "status": "stopped",
        "cost_per_hour": 0.0416,
    },
    {
        "id": "i-004",
        "name": "staging-app-01",
        "cloud": "aws",
        "type": "t3.small",
        "region": "eu-west-1",
        "status": "running",
        "cost_per_hour": 0.0208,
    },
]


def lambda_handler(event, context):
    """
    GET handler - returns instances from in-memory data.
    Supports filtering by query params: cloud, status, region
    """
    params = event.get("queryStringParameters") or {}

    # Filter by query params if provided
    results = INSTANCES
    if params.get("cloud"):
        results = [i for i in results if i["cloud"] == params["cloud"]]
    if params.get("status"):
        results = [i for i in results if i["status"] == params["status"]]
    if params.get("region"):
        results = [i for i in results if i["region"] == params["region"]]
    if params.get("id"):
        results = [i for i in results if i["id"] == params["id"]]

    return build_response(200, {"instances": results, "count": len(results)})


def build_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }
