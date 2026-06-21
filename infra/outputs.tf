output "vm_ip" {
  description = "External IP address of the VM"
  value       = google_compute_address.vm_ip.address
}

output "vm_name" {
  description = "Name of the VM instance"
  value       = google_compute_instance.vm.name
}

output "ssh_command" {
  description = "SSH command to connect"
  value       = "ssh -i ~/.ssh/lidl_bot debian@${google_compute_address.vm_ip.address}"
}
