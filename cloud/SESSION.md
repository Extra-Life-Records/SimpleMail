# Saved mailbox sign-in

SimpleMail saves its Cognito refresh session protected by Windows DPAPI and
automatically renews its five-minute access token. The original pilot configured
that saved session to expire after one day, forcing an unnecessary daily login.

The client now allows a saved sign-in for up to one year, unless revoked.
Cognito uses an absolute expiry: refreshing or rotating tokens does not extend
that original lifetime. Sign-out revokes the saved refresh session.

Deploy the Client refresh lifetime change in `template.yaml`; an app update alone
cannot change the AWS policy or restore an expired session. Review a CloudFormation
change set that modifies only `Client`, with no replacement or other resource
changes, while preserving all stack parameters. After deployment, reconnect once
to obtain a fresh session with the new lifetime.

Reference: https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-the-refresh-token.html
