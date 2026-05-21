import json

# In-memory store (resets on each cold start, fine for testing)
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
]


def lambda_handler(event, context):
    """
    POST handler - accepts an action and processes in-memory data.
    Actions: create, start, stop, calculate_cost
    """
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return build_response(400, {"error": "Invalid JSON"})

    action = body.get("action", "")

    if action == "create":
        return handle_create(body)
    elif action == "start":
        return handle_start(body)
    elif action == "stop":
        return handle_stop(body)
    elif action == "calculate_cost":
        return handle_cost_calculation(body)
    else:
        return build_response(400, {
            "error": "Invalid action",
            "valid_actions": ["create", "start", "stop", "calculate_cost"],
        })


def handle_create(body):
    """Simulate creating a new instance."""
    name = body.get("name")
    cloud = body.get("cloud", "aws")
    instance_type = body.get("type", "t3.micro")

    if not name:
        return build_response(400, {"error": "Field 'name' is required"})

    new_instance = {
        "id": f"i-{len(INSTANCES) + 1:03d}",
        "name": name,
        "cloud": cloud,
        "type": instance_type,
        "region": body.get("region", "us-east-1"),
        "status": "pending",
        "cost_per_hour": 0.0104,
    }
    INSTANCES.append(new_instance)

    return build_response(201, {"message": "Instance created", "instance": new_instance})


def handle_start(body):
    """Simulate starting an instance."""
    instance_id = body.get("id")
    if not instance_id:
        return build_response(400, {"error": "Field 'id' is required"})

    for inst in INSTANCES:
        if inst["id"] == instance_id:
            inst["status"] = "running"
            return build_response(200, {"message": "Instance started", "instance": inst})

    return build_response(404, {"error": f"Instance {instance_id} not found"})


def handle_stop(body):
    """Simulate stopping an instance."""
    instance_id = body.get("id")
    if not instance_id:
        return build_response(400, {"error": "Field 'id' is required"})

    for inst in INSTANCES:
        if inst["id"] == instance_id:
            inst["status"] = "stopped"
            return build_response(200, {"message": "Instance stopped", "instance": inst})

    return build_response(404, {"error": f"Instance {instance_id} not found"})


def handle_cost_calculation(body):
    """Calculate estimated monthly cost for given hours."""
    hours = body.get("hours_per_month", 730)  # default full month

    total_cost = 0
    breakdown = []
    for inst in INSTANCES:
        if inst["status"] == "running":
            monthly = inst["cost_per_hour"] * hours
            total_cost += monthly
            breakdown.append({"name": inst["name"], "monthly_cost": round(monthly, 2)})

    return build_response(200, {
        "total_monthly_cost": round(total_cost, 2),
        "hours_per_month": hours,
        "running_instances": len(breakdown),
        "breakdown": breakdown,
    })


def build_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }
