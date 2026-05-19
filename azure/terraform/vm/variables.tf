variable "subscription_id" {
  type = string
}

variable "location" {
  type = string
  default = "northeurope"
}

variable "vm_name" {
  type = string
}

variable "vm_size" {
  type = string
  default = "Standard_B1s"
}

variable "admin_username" {
  type = string
}

variable "admin_password" {
  type      = string
  sensitive = true
}

variable "disk_size" {
  type    = number
  default = 30
}

variable "environment" {
  type    = string
  default = "dev"
}
