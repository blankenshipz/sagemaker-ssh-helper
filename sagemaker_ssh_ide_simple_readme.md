###

1. Close this repo to your notebook and run the following script to setup the notebook node. You will be prompted to set a password for your user, this is the password that you'll use for SSH Connections.

```
./sagemaker_ssh_ide.sh
```

Capture the output for the "Managed Instance ID - you should see this logline

> Please Note that the Managed Instance ID is "mi-xxxxxxxxxxxxxxx"

Keep the command running to run the SSH server and SSM Agent. 

2. Setup your local SSH Config with an entry for localhost - we'll forward this port to the notebook in the next step.

Edit `~/.ssh/config` include an entry like the following:

```
Host sagemaker-notebook
    HostName localhost
    Port 2222
    User sagemaker-user
```

3. Install the SSM (Session Manager) Plugin for AWS (Local Machine)

https://docs.aws.amazon.com/systems-manager/latest/userguide/install-plugin-macos-overview.html

4. In a terminal run the following command (keep it running) to forward traffic from your local instance to the notebook SSH server over SSM. Make sure to update *$YOUR_MANAGED_INSTANCE_ID*

```
aws ssm start-session \
    --target $YOUR_MANAGED_INSTANCE_ID  \
    --document-name AWS-StartPortForwardingSession \
    --parameters '{"portNumber":["22"],"localPortNumber":["2222"]}' \
    --region us-east-1
```

5. Setup your editor to work with the remote SSH Session. In VSCode there's an extension called "Remote - SSH". *Optionally* setup a custom shell command to make starting the `aws ssm start-session` locally easier.
