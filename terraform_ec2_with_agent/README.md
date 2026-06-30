# Terraform EC2 with CMP Agent

This Terraform template provisions an AWS EC2 instance with automatic CMP monitoring agent installation.

## How It Works

When executed through CMP, the platform automatically injects the agent variables:

| Variable | Source | Description |
|----------|--------|-------------|
| `cmp_agent_install_url` | `cmp["agent"]["install_url"]` | URL to agent install script |
| `cmp_agent_endpoint` | `cmp["agent"]["endpoint"]` | CMP agent API endpoint |
| `cmp_agent_token` | `cmp["agent"]["token"]` | One-time registration token |
| `cmp_agent_tenant_id` | `cmp["execution"]["tenant_id"]` | Tenant scope |

These are mapped via **Terraform Variable Mappings** in the CMP catalog/resource-action definition.

## CMP Variable Mapping Configuration

In your CMP catalog or resource action definition, set up terraform_variable_mappings:

```json
[
  {"terraform_var": "cmp_agent_install_url", "source": "agent.install_url"},
  {"terraform_var": "cmp_agent_endpoint", "source": "agent.endpoint"},
  {"terraform_var": "cmp_agent_token", "source": "agent.token"},
  {"terraform_var": "cmp_agent_tenant_id", "source": "execution.tenant_id"}
]
```

## Agent Installation Flow

1. Terraform creates the EC2 instance with `user_data` containing the agent install script
2. On first boot, the instance resolves its own instance ID via IMDSv2 metadata
3. The install script downloads and runs the agent installer from CMP
4. Agent registers with CMP using the one-time token
5. Agent starts reporting metrics every 60 seconds
6. Metrics appear on the Resource Detail → System Metrics tab

## Without Agent

If `cmp_agent_token` is empty (or not mapped), the instance is created without the agent — `user_data` is null and no monitoring is installed. The `CMPAgent` tag will be set to `"false"`.
