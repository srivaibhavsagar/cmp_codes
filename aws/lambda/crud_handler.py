import json

# In-memory cloud resources for CMP testing
RESOURCES = [
    {"id": "r-001", "name": "web-server-01", "cloud": "aws", "type": "ec2", "status": "active", "tags": {"env": "prod", "team": "platform"}},
    {"id": "r-002", "name": "app-db-01", "cloud": "aws", "type": "rds", "status": "active", "tags": {"env": "prod", "team": "backend"}},
    {"id": "r-003", "name": "dev-vm-01", "cloud": "azure", "type": "vm", "status": "stopped", "tags": {"env": "dev", "team": "frontend"}},
    {"id": "r-004", "name": "cache-01", "cloud": "aws", "type": "elasticache", "status": "active", "tags": {"env": "staging", "team": "platform"}},
    {"id": "r-005", "name": "storage-bucket", "cloud": "gcp", "type": "gcs", "status": "active", "tags": {"env": "prod", "team": "data"}},
]


def lambda_handler(event, context):
    """
    Combined CRUD handler - routes by HTTP method.
    All data is in-memory, no external dependencies.
    """
    method = event.get("httpMethod", "GET")
    path_params = event.get("pathParameters") or {}

    if method == "GET":
        return handle_get(event, path_params)
    elif method == "POST":
        return handle_post(event)
    elif method == "PUT":
        return handle_put(event, path_params)
    elif method == "DELETE":
        return handle_delete(path_params)
    else:
        return build_response(405, {"error": f"Method {method} not allowed"})


def handle_get(event, path_params):
    """List all resources or get one by ID."""
    resource_id = path_params.get("id")

    if resource_id:
        resource = next((r for r in RESOURCES if r["id"] == resource_id), None)
        if not resource:
            return build_response(404, {"error": "Resource not found"})
        return build_response(200, resource)

    # Support filtering
    params = event.get("queryStringParameters") or {}
    results = RESOURCES

    if params.get("cloud"):
        results = [r for r in results if r["cloud"] == params["cloud"]]
    if params.get("type"):
        results = [r for r in results if r["type"] == params["type"]]
    if params.get("status"):
        results = [r for r in results if r["status"] == params["status"]]
    if params.get("env"):
        results = [r for r in results if r.get("tags", {}).get("env") == params["env"]]

    return build_response(200, {"resources": results, "count": len(results)})


def handle_post(event):
    """Add a new resource."""
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return build_response(400, {"error": "Invalid JSON"})

    if not body.get("name") or not body.get("cloud"):
        return build_response(400, {"error": "Fields 'name' and 'cloud' are required"})

    new_resource = {
        "id": f"r-{len(RESOURCES) + 1:03d}",
        "name": body["name"],
        "cloud": body["cloud"],
        "type": body.get("type", "unknown"),
        "status": "active",
        "tags": body.get("tags", {}),
    }
    RESOURCES.append(new_resource)

    return build_response(201, {"message": "Resource created", "resource": new_resource})


def handle_put(event, path_params):
    """Update an existing resource."""
    resource_id = path_params.get("id")
    if not resource_id:
        return build_response(400, {"error": "Resource ID required"})

    resource = next((r for r in RESOURCES if r["id"] == resource_id), None)
    if not resource:
        return build_response(404, {"error": "Resource not found"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return build_response(400, {"error": "Invalid JSON"})

    # Update allowed fields
    for field in ["name", "status", "type", "tags"]:
        if field in body:
            resource[field] = body[field]

    return build_response(200, {"message": "Resource updated", "resource": resource})


def handle_delete(path_params):
    """Delete a resource by ID."""
    resource_id = path_params.get("id")
    if not resource_id:
        return build_response(400, {"error": "Resource ID required"})

    global RESOURCES
    original_count = len(RESOURCES)
    RESOURCES = [r for r in RESOURCES if r["id"] != resource_id]

    if len(RESOURCES) == original_count:
        return build_response(404, {"error": "Resource not found"})

    return build_response(200, {"message": "Resource deleted", "id": resource_id})


def build_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body),
    }
