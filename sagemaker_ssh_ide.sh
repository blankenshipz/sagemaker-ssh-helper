check_password_set() {
  local user=${1:-$(whoami)}
  passwd -S "$user" 2>/dev/null | grep -q '^[^L]*P'
  return $?
}

current_user=$(whoami)

if ! check_password_set "$current_user"; then
  echo "No password set for $current_user"
  # Prompt for new password securely (hidden input)
  read -s -p "Enter new password: " password
  echo
  read -s -p "Confirm password: " password2
  echo

  if [ "$password" = "$password2" ]; then
    # Use chpasswd to set password non-interactively
    echo "$current_user:$password" | sudo chpasswd
    if [ $? -eq 0 ]; then
      echo "Password set successfully"
    else
      echo "Failed to set password"
      exit 1
    fi
  else
    echo "Passwords do not match"
    exit 1
  fi
else
  echo "Password is already set for $current_user"
fi

# Configure sm-ssh-ide
sudo ./sagemaker_ssh_helper/sm-ssh-ide configure --ssh-only

# Set a Fake User ID for the SSH Helper Scripts
sudo ./sagemaker_ssh_helper/sm-ssh-ide set-local-user-id "no-user-needed"

# Init SSM
sudo \
  AWS_CONTAINER_CREDENTIALS_RELATIVE_URI="$AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" \
  AWS_DEFAULT_REGION="$AWS_DEFAULT_REGION" \
  AWS_REGION="$AWS_REGION" \
  AWS_INTERNAL_IMAGE_OWNER="$AWS_INTERNAL_IMAGE_OWNER" \
  AWS_ACCOUNT_ID="$AWS_ACCOUNT_ID" \
  ./sagemaker_ssh_helper/sm-ssh-ide init-ssm

# Update the SSHD Config to Allow Password Authentication
sed -i 's/#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config

# Clear Services (Should Start SSHD in the background)
sudo ./sagemaker_ssh_helper/sm-ssh-ide stop
sudo ./sagemaker_ssh_helper/sm-ssh-ide start

# Output the Managed Instance ID to the Log
MANAGED_INSTANCE_ID=$(sudo cat /var/lib/amazon/ssm/registration | jq .ManagedInstanceID)
echo "!!!!!!!!! Please Note that the Managed Instance ID is ${MANAGED_INSTANCE_ID} !!!!!!!!!!!"

# Start the Agent
./sagemaker_ssh_helper/sm-ssh-ide ssm-agent

