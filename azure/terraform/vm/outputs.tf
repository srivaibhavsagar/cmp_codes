output "vm_id" {
  value = azurerm_linux_virtual_machine.main.id
}

output "public_ip" {
  value = azurerm_public_ip.main.ip_address
}

output "resource_group" {
  value = azurerm_resource_group.main.name
}
