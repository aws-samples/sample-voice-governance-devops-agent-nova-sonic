# Requirements Document

## Introduction

The Nova Sonic Support Portal is a voice-driven support portal for DevOps engineers. Engineers speak to a browser application; Amazon Nova 2 Sonic (a Bedrock speech-to-speech model in us-east-1) transcribes speech, converses, and forwards engineer requests as text to the AWS DevOps Agent service through a Nova Sonic tool named `ask_devops_agent`. The DevOps Agent's streamed answer is spoken back to the engineer. The portal also pushes incident notifications from the backend to engineers as in-app popups with an audio chime, and as web push notifications when the browser is closed.

The system consists of two planes plus shared services:

- **Voice plane**: Browser (microphone, PCM audio) ↔ WebSocket ↔ ALB ↔ ECS Fargate service (Python FastAPI + websockets). Each ECS task holds Bedrock `InvokeModelWithBidirectionalStream` sessions to Nova 2 Sonic, handles the 8-minute Bedrock stream rollover via session segmentation with context replay, and exposes the `ask_devops_agent` tool.
- **Notification plane**: EventBridge (sources: CloudWatch Alarms, Incident Manager, DevOps Agent findings) → notifier Lambda → AppSync Events broadcast channel (in-app popup + audio chime) and the Web Push API via a service worker (browser closed), with optional SNS escalation.
- **Shared services**: Amazon Cognito for mandatory authentication, DynamoDB for session state and mappings, CloudFront + S3 for the frontend.

The entire deployment is provisioned with Terraform in two layers (bootstrap and application), released through three CI/CD pipelines (frontend, backend, IaC), and governed by Amazon Bedrock Guardrails with Automated Reasoning checks that restrict the portal to read-only diagnostic operations.

## Glossary

- **Portal**: The complete Nova Sonic Support Portal system, comprising the Frontend, Voice_Service, Notifier, and supporting AWS infrastructure.
- **Frontend**: The browser-based single-page application served from S3 via CloudFront, including its service worker.
- **Voice_Service**: The Python FastAPI application running on ECS Fargate that terminates engineer WebSocket connections and manages Nova Sonic streams.
- **Nova_Sonic**: The Amazon Nova 2 Sonic speech-to-speech foundation model, invoked via the Bedrock `InvokeModelWithBidirectionalStream` API (HTTP/2) in us-east-1.
- **Bedrock_Stream**: A single bidirectional stream to Nova_Sonic, limited by Bedrock to a maximum duration of 8 minutes.
- **Voice_Session**: A logical conversation between one engineer and the Portal, which may span multiple consecutive Bedrock_Streams.
- **Session_Segmentation**: The process of ending an expiring Bedrock_Stream and opening a new one while replaying conversation context so the Voice_Session continues without loss of context.
- **DevOps_Agent**: The AWS DevOps Agent service, invoked via the `aidevops:CreateChat` and `aidevops:SendMessage` APIs with streaming responses.
- **Ask_DevOps_Agent_Tool**: The Nova Sonic tool named `ask_devops_agent`, exposed by the Voice_Service, which forwards an engineer request as text to the DevOps_Agent and returns the response.
- **Guardrail**: An Amazon Bedrock Guardrails configuration with Automated Reasoning checks that blocks requests for destructive or environment-impacting operations.
- **Notifier**: The Lambda function that receives incident events from EventBridge and fans them out to notification channels.
- **Events_Channel**: The AppSync Events broadcast channel that delivers in-app notifications to connected browsers.
- **Web_Push_Subscription**: A browser push subscription registered by the Frontend service worker for delivery when the browser is closed.
- **Incident_Notification**: A notification payload describing an incident, carrying the DevOps_Agent executionId when available.
- **Auth_Service**: The Amazon Cognito user pool and associated clients that authenticate engineers.
- **Session_Store**: The DynamoDB tables persisting Voice_Session state, chat/execution mappings, Web_Push_Subscriptions, and transcripts.
- **Bootstrap_Layer**: The first Terraform layer, which creates source repositories, CodePipeline pipelines, CodeBuild projects, and supporting CI/CD resources.
- **Application_Layer**: The second Terraform layer, deployed by the pipelines the Bootstrap_Layer creates, which provisions the ECS service, IAM roles, AppSync Events, Nova Sonic access, and operations resources (CloudWatch alarms, SNS topics).
- **Frontend_Pipeline**: The CodePipeline that builds and deploys the Frontend.
- **Backend_Pipeline**: The CodePipeline that builds and deploys the Voice_Service container.
- **IaC_Pipeline**: The CodePipeline that plans and applies the Application_Layer Terraform.
- **Destructive_Operation**: Any request that mutates AWS environments, including but not limited to terminating instances, deleting or purging queues, deleting Lambda functions, creating IAM roles, and creating instances.
- **OAC**: CloudFront Origin Access Control, restricting S3 origin access to CloudFront.

## Requirements

### Requirement 1: Voice Conversation

**User Story:** As a DevOps engineer, I want to speak to the portal and hear spoken responses, so that I can get support hands-free while working on incidents.

#### Acceptance Criteria

1. WHEN an authenticated engineer starts a Voice_Session, THE Frontend SHALL capture microphone audio and stream it as 16kHz PCM audio over a WebSocket connection to the Voice_Service.
2. WHEN the Voice_Service receives engineer audio, THE Voice_Service SHALL forward the audio to Nova_Sonic over a Bedrock_Stream within 500 milliseconds of receipt.
3. WHEN Nova_Sonic produces response audio, THE Voice_Service SHALL stream the response audio to the Frontend over the same WebSocket connection used for the Voice_Session.
4. WHEN the Frontend receives a transcript event over the WebSocket connection, THE Frontend SHALL display the transcript text of the engineer utterance or the Nova_Sonic response within 2 seconds of receipt, visually distinguishing engineer utterances from Nova_Sonic responses.
5. IF the WebSocket connection is interrupted, THEN THE Frontend SHALL display a notification to the engineer within 5 seconds of detecting the interruption and SHALL present a reconnect option that, when selected, attempts to re-establish the WebSocket connection to the Voice_Session.
6. IF the Voice_Service cannot open a Bedrock_Stream, THEN THE Voice_Service SHALL send a structured error message to the Frontend indicating the failure category, close the WebSocket connection, and end the Voice_Session without processing further engineer audio.
7. WHEN the Frontend receives response audio from the Voice_Service, THE Frontend SHALL begin playing the response audio to the engineer within 1 second of receiving the first audio chunk.
8. IF the engineer denies microphone permission, THEN THE Frontend SHALL display an error message indicating that microphone access is required and SHALL NOT open a WebSocket connection to the Voice_Service.

### Requirement 2: Bedrock Stream Lifecycle Management

**User Story:** As a DevOps engineer, I want conversations to continue past 8 minutes without losing context, so that long troubleshooting sessions are not cut off.

#### Acceptance Criteria

1. THE Voice_Service SHALL invoke Nova_Sonic exclusively through the Bedrock `InvokeModelWithBidirectionalStream` API over HTTP/2 in us-east-1.
2. WHEN a Bedrock_Stream reaches 7 minutes 30 seconds of elapsed stream time, THE Voice_Service SHALL perform Session_Segmentation by closing the expiring Bedrock_Stream and opening a new Bedrock_Stream before the 8-minute Bedrock limit is reached.
3. WHEN performing Session_Segmentation, THE Voice_Service SHALL replay the conversation context (system prompt, tool configuration, and conversation history in original chronological order) into the new Bedrock_Stream before delivering any buffered audio, so the Voice_Session continues without loss of context.
4. WHILE Session_Segmentation is in progress, THE Voice_Service SHALL buffer up to 30 seconds of incoming engineer audio and deliver the buffered audio to the new Bedrock_Stream in the order it was received once the new Bedrock_Stream is ready.
5. WHEN a Voice_Session ends, THE Voice_Service SHALL close the associated Bedrock_Stream and persist the final transcript to the Session_Store.
6. IF Session_Segmentation fails or does not complete within 10 seconds of being initiated, THEN THE Voice_Service SHALL persist the transcript accumulated up to the failure point to the Session_Store, send a structured error message indicating segmentation failure to the Frontend, and record the failure with the Voice_Session identifier in application logs.
7. IF persisting a transcript to the Session_Store fails, THEN THE Voice_Service SHALL retry the persistence operation up to 3 times and, if all retries fail, record the failure with the Voice_Session identifier in application logs.

### Requirement 3: DevOps Agent Integration

**User Story:** As a DevOps engineer, I want my spoken questions forwarded to the AWS DevOps Agent, so that I receive expert diagnostic answers spoken back to me.

#### Acceptance Criteria

1. THE Voice_Service SHALL register the Ask_DevOps_Agent_Tool in the Nova_Sonic tool configuration for every Voice_Session.
2. WHEN Nova_Sonic invokes the Ask_DevOps_Agent_Tool for the first time in a Voice_Session, THE Voice_Service SHALL create a chat with the DevOps_Agent using the `aidevops:CreateChat` API and persist the chat identifier in the Session_Store keyed by the Voice_Session.
3. WHEN Nova_Sonic invokes the Ask_DevOps_Agent_Tool, THE Voice_Service SHALL send the engineer request as text to the DevOps_Agent using the `aidevops:SendMessage` API and accumulate all streamed response chunks into a single complete response text before returning the tool result.
4. WHEN the DevOps_Agent returns a response, THE Voice_Service SHALL return the complete accumulated response text as the Ask_DevOps_Agent_Tool result so that Nova_Sonic speaks the answer to the engineer.
5. WHEN a Voice_Session is opened from an Incident_Notification carrying an executionId, THE Voice_Service SHALL scope the DevOps_Agent chat to that executionId.
6. WHEN a Voice_Session is opened without an executionId, THE Voice_Service SHALL create the DevOps_Agent chat without execution scoping.
7. IF a DevOps_Agent API call fails, THEN THE Voice_Service SHALL return a tool result to Nova_Sonic containing an error indication that the DevOps_Agent request could not be completed, so that Nova_Sonic informs the engineer verbally, and THE Voice_Service SHALL log the failure including the exception class name and the Voice_Session identifier.
8. IF the DevOps_Agent does not complete its streamed response within 60 seconds of the `aidevops:SendMessage` call, THEN THE Voice_Service SHALL stop consuming the stream and return a tool result to Nova_Sonic containing an error indication that the DevOps_Agent response timed out.
9. WHEN a Voice_Session sends a second or subsequent request to the DevOps_Agent, THE Voice_Service SHALL reuse the chat identifier persisted in the Session_Store for that Voice_Session so conversation continuity with the DevOps_Agent is preserved.
10. IF the chat identifier for a Voice_Session is not found in the Session_Store when a subsequent request is made, THEN THE Voice_Service SHALL create a new chat with the DevOps_Agent using the `aidevops:CreateChat` API and persist the new chat identifier in the Session_Store keyed by the Voice_Session.

### Requirement 4: Read-Only Guardrails

**User Story:** As a security administrator, I want the portal restricted to read-only diagnostic operations, so that no engineer can trigger destructive changes through the voice interface.

#### Acceptance Criteria

1. THE Portal SHALL apply a Guardrail with Amazon Bedrock Automated Reasoning checks to every engineer request in a Nova_Sonic interaction before that request is forwarded to the DevOps_Agent.
2. WHEN an engineer requests a Destructive_Operation (any operation that creates, modifies, deletes, or terminates an AWS resource or IAM entity, including terminating an instance, deleting or purging a queue, deleting a Lambda function, creating an IAM role, or creating an instance), THE Guardrail SHALL block the request before any part of the request reaches the DevOps_Agent.
3. WHEN the Guardrail blocks a request, THE Voice_Service SHALL cause Nova_Sonic to verbally inform the engineer, within the same Voice_Session, that the requested operation is not permitted and that the Portal supports only read and diagnostic operations.
4. THE Voice_Service SHALL forward to the DevOps_Agent only requests that the Guardrail has evaluated and classified as read or diagnostic support requests.
5. WHEN the Guardrail blocks a request, THE Voice_Service SHALL record the blocked request content, the Voice_Session identifier, the engineer identity, and a timestamp in application logs.
6. IF the Guardrail evaluation fails or the Guardrail is unavailable, THEN THE Voice_Service SHALL block the request without forwarding it to the DevOps_Agent and SHALL cause Nova_Sonic to verbally inform the engineer that the request cannot be processed.
7. IF the Guardrail cannot classify a request as a read or diagnostic operation, THEN THE Guardrail SHALL block the request.

### Requirement 5: In-App Incident Notifications

**User Story:** As a DevOps engineer, I want to see and hear incident notifications while using the portal, so that I can react to incidents immediately.

#### Acceptance Criteria

1. WHEN an incident event from CloudWatch Alarms, Incident Manager, or DevOps_Agent findings arrives on EventBridge, THE Notifier SHALL publish an Incident_Notification to the Events_Channel within 5 seconds of receiving the event.
2. WHEN the Frontend receives an Incident_Notification on the Events_Channel, THE Frontend SHALL display an in-app popup containing the incident summary, severity, and timestamp from the notification payload within 2 seconds of receipt.
3. WHEN the Frontend receives an Incident_Notification on the Events_Channel, THE Frontend SHALL play an audio chime.
4. IF the browser blocks or audio playback fails, THEN THE Frontend SHALL display the in-app popup without the audio chime and SHALL NOT suppress the notification.
5. THE Incident_Notification payload SHALL include the incident summary, severity, and timestamp.
6. IF the source event provides a DevOps_Agent executionId, THEN THE Notifier SHALL include the executionId in the Incident_Notification payload.
7. WHEN an engineer clicks an Incident_Notification that includes an executionId, THE Frontend SHALL open a Voice_Session scoped to the incident using that executionId.
8. IF an engineer clicks an Incident_Notification that does not include an executionId, THEN THE Frontend SHALL open a Voice_Session with the incident summary and severity from the notification payload as the session context.
9. IF publishing to the Events_Channel fails, THEN THE Notifier SHALL log the failure with a specific exception class and retry the publish up to 3 additional attempts.
10. IF all retry attempts to publish to the Events_Channel fail, THEN THE Notifier SHALL log the final failure with a specific exception class indicating retry exhaustion.
11. WHERE SNS escalation is configured, THE Notifier SHALL additionally publish the Incident_Notification to the configured SNS topic.

### Requirement 6: Web Push Notifications

**User Story:** As a DevOps engineer, I want incident notifications delivered even when my browser is closed, so that I never miss a critical incident.

#### Acceptance Criteria

1. WHEN an authenticated engineer grants push permission, THE Frontend SHALL register a Web_Push_Subscription through its service worker and persist the subscription in the Session_Store within 10 seconds of the permission grant.
2. WHEN the Notifier processes an incident event, THE Notifier SHALL deliver an Incident_Notification containing the incident summary and the executionId to each registered Web_Push_Subscription via the Web Push API within 30 seconds of receiving the event.
3. WHILE the browser is closed, WHEN a web push message arrives, THE Frontend service worker SHALL display a system notification containing the incident summary from the Incident_Notification payload.
4. WHEN an engineer clicks a system notification, THE Frontend SHALL open the Portal, verify the engineer holds a valid authenticated session (prompting for authentication if none exists), and start a Voice_Session scoped to the incident using the executionId from the Incident_Notification payload.
5. IF a Web_Push_Subscription is rejected by the push service as expired or invalid, THEN THE Notifier SHALL remove the subscription from the Session_Store and SHALL NOT attempt further deliveries to that subscription.
6. IF an engineer denies push permission, THEN THE Frontend SHALL NOT register a Web_Push_Subscription and SHALL display an indication that push notifications are disabled.
7. IF persisting a Web_Push_Subscription to the Session_Store fails, THEN THE Frontend SHALL display an error message indicating that push notification registration failed and SHALL allow the engineer to retry registration.
8. IF delivery of an Incident_Notification to a Web_Push_Subscription fails for a reason other than an expired or invalid subscription, THEN THE Notifier SHALL retry delivery up to 3 times before discarding the notification for that subscription.

### Requirement 7: Authentication and Authorization

**User Story:** As a security administrator, I want every connection authenticated, so that only authorized engineers can use the portal.

#### Acceptance Criteria

1. THE Auth_Service SHALL authenticate engineers using Amazon Cognito before the Frontend grants access to any Portal feature.
2. WHEN the Frontend initiates a WebSocket handshake with the Voice_Service, THE Voice_Service SHALL validate that the handshake carries a Cognito-issued JWT with a valid signature and an unexpired lifetime before accepting the connection.
3. IF the WebSocket handshake presents a missing, expired, or invalid token, THEN THE Voice_Service SHALL reject the connection with an authentication error and SHALL NOT create a Voice_Session.
4. WHEN the Frontend connects to the Events_Channel, THE Events_Channel SHALL require a valid Cognito-issued authorization.
5. THE Voice_Service SHALL require Cognito authentication regardless of the transport protocol used between CloudFront, the ALB, and the Voice_Service.
6. IF a Cognito token expires during an active Voice_Session, THEN THE Voice_Service SHALL send an authentication-expired error message to the Frontend, SHALL close the Voice_Session, and SHALL NOT process audio received after the expiration is detected.
7. IF an unauthenticated engineer attempts to access a Portal feature, THEN THE Frontend SHALL deny access to the requested feature and redirect the engineer to the Cognito sign-in flow.
8. IF a connection to the Events_Channel presents a missing, expired, or invalid Cognito authorization, THEN THE Events_Channel SHALL reject the connection and SHALL NOT deliver any events to that connection.

### Requirement 8: Session State Persistence

**User Story:** As a DevOps engineer, I want my sessions, transcripts, and subscriptions stored reliably, so that context survives reconnects and incidents can be traced.

#### Acceptance Criteria

1. THE Session_Store SHALL persist Voice_Session state, DevOps_Agent chat and execution mappings, Web_Push_Subscriptions, and conversation transcripts in DynamoDB.
2. WHEN a Voice_Session state changes (created, segmented, ended), THE Voice_Service SHALL update the corresponding Session_Store record before reporting the state change as complete.
3. WHEN an engineer reconnects to an existing Voice_Session, THE Voice_Service SHALL restore conversation context, consisting of the persisted Voice_Session state and all persisted transcript entries for that Voice_Session, from the Session_Store.
4. THE Session_Store SHALL apply a time-to-live attribute to Voice_Session and transcript records equal to the record's last update time plus a configurable retention period defaulting to 30 days, so that expired records are removed automatically.
5. IF a Session_Store read or write fails, THEN THE Voice_Service SHALL return an error indication to the caller, raise a specific exception class, log the failure with the affected Voice_Session identifier, and leave no partially updated record in the Session_Store.
6. IF an engineer attempts to reconnect to a Voice_Session that does not exist in the Session_Store or whose records have expired, THEN THE Voice_Service SHALL reject the reconnection and return an error indication that the Voice_Session is not available.
7. WHEN a Web_Push_Subscription is registered or removed, THE Session_Store SHALL persist the corresponding record change before the registration or removal is confirmed to the caller.

### Requirement 9: Responsive Frontend

**User Story:** As a DevOps engineer, I want a responsive web UI that works on my phone, so that I can use the portal on call from any device.

#### Acceptance Criteria

1. THE Frontend SHALL be deployed as static assets to an S3 bucket and served through CloudFront.
2. THE Frontend SHALL render a responsive layout using Bootstrap or an equivalent responsive CSS framework such that all Portal features are operable without horizontal scrolling at viewport widths from 320 pixels to 1920 pixels.
3. WHEN an engineer accesses the Portal from a mobile browser, THE Frontend SHALL provide microphone capture, audio playback, transcripts, and notifications with the same functionality as on desktop browsers.
4. WHILE a Voice_Session is active, THE Frontend SHALL display the current Voice_Session status as one of: connecting, live, segmenting, ended, or error.
5. WHEN the Voice_Session status changes, THE Frontend SHALL update the displayed status within 1 second of the change.
6. IF the browser denies microphone access permission, THEN THE Frontend SHALL display an error message indicating that microphone permission is required to start a Voice_Session.
7. IF the browser does not support the audio capture or audio playback capabilities required for a Voice_Session, THEN THE Frontend SHALL display an error message indicating that the browser is unsupported.

### Requirement 10: Scalability and Availability

**User Story:** As an operations owner, I want the voice service to scale with demand without dropping live calls, so that the portal stays available during incident storms.

#### Acceptance Criteria

1. THE Voice_Service SHALL run as an ECS Fargate service behind an Application Load Balancer, with tasks distributed across at least 2 Availability Zones and a minimum of 2 running tasks at all times.
2. WHEN ALB active connection count exceeds the configured scale-out threshold, THE Voice_Service SHALL increase the ECS task count within 5 minutes, up to the configured maximum task count.
3. WHILE an ECS task hosts at least one live Voice_Session, THE Voice_Service SHALL keep ECS task scale-in protection enabled for that task.
4. WHEN the last Voice_Session on an ECS task ends, THE Voice_Service SHALL release the scale-in protection for that task within 60 seconds.
5. WHEN an ECS task receives a termination signal, THE Voice_Service SHALL stop accepting new WebSocket connections on that task and continue serving existing Voice_Sessions for the configured drain period of at most 120 seconds.
6. WHEN ALB active connection count falls below the configured scale-in threshold, THE Voice_Service SHALL decrease the ECS task count without terminating tasks that have scale-in protection enabled and without going below the minimum task count of 2.
7. IF enabling scale-in protection for an ECS task fails, THEN THE Voice_Service SHALL retry the protection request up to 3 times and SHALL stop routing new Voice_Sessions to that task until protection is confirmed.
8. IF a Voice_Session is still active when the drain period expires, THEN THE Voice_Service SHALL send a session-termination notification to the connected client before closing that WebSocket connection.

### Requirement 11: Web Application Firewall

**User Story:** As a security administrator, I want a WAF in front of the portal, so that common web attacks are blocked before reaching the application.

#### Acceptance Criteria

1. THE Portal SHALL attach an AWS WAF web ACL to every internet-facing entry point of the Portal, including the CloudFront distribution and any internet-facing ALB.
2. THE WAF web ACLs SHALL include, at minimum, the AWS managed core rule set (AWSManagedRulesCommonRuleSet) and the known bad inputs rule set (AWSManagedRulesKnownBadInputsRuleSet).
3. THE WAF web ACLs SHALL configure the included managed rule sets with blocking enabled, such that a request matching a rule is blocked rather than only counted.
4. WHEN the WAF blocks a request, THE Portal SHALL return an error response to the requester and SHALL NOT forward the blocked request to the application.
5. WHEN the WAF blocks a request, THE Portal SHALL record the blocked request in WAF logs, including the timestamp, source IP address, requested URI, and the identifier of the rule that triggered the block.
6. THE Portal SHALL enable WAF logging for each attached web ACL so that all blocked requests are captured in a persistent log destination.

### Requirement 12: Encryption in Transit and at Rest

**User Story:** As a security administrator, I want all data encrypted in transit and at rest, so that engineer conversations and incident data are protected.

#### Acceptance Criteria

1. THE Frontend SHALL be served over HTTPS using the CloudFront default domain certificate.
2. WHEN CloudFront receives a Frontend request over HTTP, THE Frontend SHALL redirect the request to HTTPS.
3. THE Portal SHALL encrypt data at rest in every persistent data store, including S3 buckets, DynamoDB tables, CloudWatch log groups, and container image repositories.
4. WHERE no custom domain TLS certificate is available for the ALB, THE Portal SHALL permit an HTTP listener on the ALB for CloudFront-to-ALB or direct traffic.
5. THE Voice_Service SHALL require Cognito authentication on every connection, including connections received through the ALB HTTP listener.
6. IF a connection to the Voice_Service does not present valid Cognito authentication, THEN THE Voice_Service SHALL reject the connection before processing any audio or incident data.
7. THE Voice_Service SHALL use TLS for all connections to AWS services (Bedrock, DevOps_Agent, DynamoDB, AppSync).
8. IF a TLS connection between the Voice_Service and an AWS service cannot be established, THEN THE Voice_Service SHALL fail the operation and SHALL NOT transmit data over an unencrypted connection.

### Requirement 13: S3 and ALB Hardening

**User Story:** As a security administrator, I want storage and load balancer hardening controls enforced, so that infrastructure cannot be accidentally deleted or accessed insecurely.

#### Acceptance Criteria

1. THE Application_Layer SHALL set the deletion protection attribute on the ALB to enabled.
2. THE Application_Layer SHALL apply a bucket policy to every S3 bucket created by the Application_Layer that denies all requests for which `aws:SecureTransport` is false.
3. THE Application_Layer SHALL apply a bucket policy to every S3 bucket created by the Application_Layer that denies all requests made with a TLS version lower than 1.2.
4. THE Application_Layer SHALL accept the access-logging bucket name as a Terraform input variable.
5. WHEN configuring access logging, THE Application_Layer SHALL reference the existing bucket identified by the access-logging bucket name input variable without creating any S3 bucket for logging.
6. IF the access-logging bucket name input variable is empty or not provided, THEN THE Application_Layer SHALL fail Terraform validation with an error message indicating that the access-logging bucket name is required.
7. THE Application_Layer SHALL apply a bucket policy to the Frontend S3 bucket that allows read access only to the CloudFront distribution via OAC and denies read requests from all other principals.

### Requirement 14: Secrets and Configuration Management

**User Story:** As an operations owner, I want configuration externalized and secrets managed centrally, so that the same codebase deploys across dev and prod without code changes.

#### Acceptance Criteria

1. THE Portal SHALL store all secrets and sensitive configuration (including credentials, API keys, authentication tokens, and database or service connection strings) in AWS Secrets Manager or SSM Parameter Store, and SHALL store all non-sensitive configuration in environment variables.
2. THE Portal codebase SHALL contain no hardcoded secrets, credentials, account identifiers, or environment-specific endpoints, such that a static scan of the source repository finds zero occurrences of these values.
3. WHEN the Portal is deployed to a different environment (dev or prod), THE Portal SHALL derive all environment-specific configuration from Terraform input variables and runtime configuration sources without source code changes, such that the deployed artifact is identical across environments.
4. WHEN the Voice_Service starts, THE Voice_Service SHALL load configuration from its configured sources, and IF a required configuration value is missing, THEN THE Voice_Service SHALL terminate startup before accepting any requests and SHALL raise a specific exception class whose error output identifies the missing configuration key by name.
5. IF retrieval of a secret from AWS Secrets Manager or SSM Parameter Store fails during startup, THEN THE Portal SHALL terminate startup before accepting any requests and SHALL emit an error indicating which secret could not be retrieved.
6. THE Portal SHALL exclude secret values from all log output and error messages, referencing configuration entries by key name only.

### Requirement 15: Infrastructure as Code with Terraform

**User Story:** As an operations owner, I want all infrastructure defined in Terraform across two layers, so that deployments are reproducible and CI/CD is itself provisioned as code.

#### Acceptance Criteria

1. THE Portal SHALL define all AWS infrastructure using Terraform, organized into exactly two layers: the Bootstrap_Layer and the Application_Layer.
2. THE Bootstrap_Layer SHALL create the source repositories, CodePipeline pipelines, CodeBuild projects, artifact stores, and the IAM roles used by CodePipeline and CodeBuild, such that the Application_Layer can be deployed through the created pipelines without manually created AWS resources.
3. THE Application_Layer SHALL provision the ECS service, task and execution IAM roles, ALB, AppSync Events resources, Cognito resources, DynamoDB tables, Nova_Sonic access policies, and Guardrail resources.
4. THE Application_Layer SHALL provision CloudWatch alarms and the SNS topics that receive notifications from those alarms.
5. THE Terraform code SHALL accept environment-specific values (environment name, account-specific inputs, access-logging bucket name) as input variables and SHALL NOT hardcode these values in resource definitions.
6. THE Terraform code SHALL maintain separate Terraform state for the Bootstrap_Layer and the Application_Layer, such that each layer can be planned and applied independently of the other.
7. IF a required input variable is not provided, THEN THE Terraform code SHALL fail the plan operation with an error indicating the missing variable, without creating or modifying any AWS resources.

### Requirement 16: CI/CD Pipelines

**User Story:** As an operations owner, I want three gated pipelines with security and test stages, so that only verified changes reach the environment.

#### Acceptance Criteria

1. THE Bootstrap_Layer SHALL create three separate pipelines: the Frontend_Pipeline, the Backend_Pipeline, and the IaC_Pipeline, each connected to its own source repository.
2. WHEN a commit is pushed to the monitored branch of a pipeline's source repository, THE corresponding pipeline SHALL start a new execution automatically without manual intervention.
3. WHEN a pipeline execution starts, THE pipeline SHALL run three separate CodeBuild stages in order — a security scan stage, a unit test stage, and a build-and-plan stage — starting each stage only after the preceding stage completes successfully.
4. IF the security scan stage detects one or more findings of high or critical severity, THEN THE security scan stage SHALL fail.
5. IF any CodeBuild stage fails, THEN THE pipeline SHALL stop the execution, mark the execution as failed, and SHALL NOT execute any subsequent stage, including the deployment stage.
6. WHEN all three CodeBuild stages succeed, THE pipeline SHALL pause at a manual approval stage before the deployment stage.
7. WHEN the manual approval is granted, THE pipeline SHALL execute the deployment stage.
8. IF the manual approval is rejected, or is not granted within 7 days of reaching the approval stage, THEN THE pipeline SHALL stop the execution without executing the deployment stage and SHALL mark the execution as failed.
9. WHEN the Frontend_Pipeline deployment stage executes, THE Frontend_Pipeline SHALL deploy Frontend assets using `aws s3 sync` executed within a CodeBuild stage, and after the sync completes successfully, SHALL create a CloudFront cache invalidation covering the updated assets.
10. THE Frontend_Pipeline SHALL exclude the CodePipeline S3 deploy action from its pipeline definition.

### Requirement 17: Code Quality Standards

**User Story:** As a developer, I want enforced code quality standards, so that the codebase remains maintainable and reviewable.

#### Acceptance Criteria

1. THE Portal codebase SHALL include a docstring or documentation comment for every function, class, method, and module-level definition that describes the definition's purpose, each parameter by name, the return value where a value is returned, and each exception type the definition raises.
2. THE Voice_Service and Notifier SHALL use asynchronous code (async/await) for I/O-bound operations, including WebSocket handling, Bedrock streaming, DevOps_Agent calls, and DynamoDB access.
3. THE Voice_Service and Notifier SHALL NOT invoke blocking synchronous I/O operations within asynchronous execution paths.
4. WHEN Portal code raises an exception, THE Portal code SHALL raise a specific exception class rather than a bare or generic Exception.
5. WHEN Portal code catches an exception, THE Portal code SHALL catch a specific exception class rather than a bare or generic Exception, except in top-level boundary handlers (such as global handlers that convert unhandled errors into error responses) where catching generic exceptions is permitted.
6. THE Portal codebase SHALL structure each module around a single responsibility, and SHALL access external dependencies (Bedrock, DevOps_Agent, DynamoDB) only through dedicated interface modules.
7. IF Portal code violates any of the standards in criteria 1 through 6, THEN THE Portal build process SHALL fail an automated quality check before the change is merged.

### Requirement 18: Project Structure and Documentation

**User Story:** As a developer, I want a clear repository layout and deployment documentation, so that I can navigate and deploy the project without tribal knowledge.

#### Acceptance Criteria

1. THE Portal repository SHALL organize code into three separate top-level folders named `infrastructure/`, `frontend/`, and `backend/`, where infrastructure code resides only under `infrastructure/`, frontend application code resides only under `frontend/`, and backend application code resides only under `backend/`.
2. THE Portal repository SHALL include a README.md at the repository root containing the following sections: an architecture overview, a prerequisites list identifying every tool and access requirement needed for deployment, and step-by-step deployment instructions covering the Bootstrap_Layer, the Application_Layer, and each pipeline in the order they must be deployed.
3. THE Portal repository SHALL include at least one Kiro steering file under `.kiro/steering/` documenting project conventions and coding standards, covering each of the `infrastructure/`, `frontend/`, and `backend/` code areas.
4. THE Portal repository SHALL include Kiro hook definitions under `.kiro/hooks/` that trigger lint execution on file save and test execution on file save.
5. THE README.md deployment instructions SHALL specify, for each deployment step, the command to execute and the observable outcome that indicates the step completed successfully.

### Requirement 19: Well-Architected Alignment and Observability

**User Story:** As an operations owner, I want the design aligned to the AWS Well-Architected Framework with actionable monitoring, so that the portal is reliable, secure, and cost-aware in operation.

#### Acceptance Criteria

1. THE Portal design document SHALL record, for each of the AWS Well-Architected Framework pillars of operational excellence, security, reliability, high availability, and cost optimization, at least one design decision that addresses that pillar and the rationale for it.
2. THE Application_Layer SHALL provision CloudWatch alarms for Voice_Service running task count, ALB unhealthy target count, Voice_Service error rate, and Notifier delivery failures, with each alarm defining a monitored metric, a threshold value, and an evaluation period.
3. WHEN a provisioned CloudWatch alarm enters the ALARM state, THE Portal SHALL publish a message identifying the alarm name and the new alarm state to the operations SNS topic within 60 seconds of the state change.
4. THE Voice_Service and Notifier SHALL emit application logs to CloudWatch Logs in which each log entry includes a timestamp and a severity level, and each log entry produced while handling a Voice_Session includes the Voice_Session identifier.
5. WHEN the Voice_Service scaling metric remains below the configured scale-in threshold for the configured evaluation period, THE Portal SHALL reduce the ECS task count.
6. WHILE an ECS task hosts at least one live Voice_Session, THE Portal SHALL NOT terminate that task through a scale-in action.
7. WHEN the last live Voice_Session on a scale-in-protected ECS task ends, THE Portal SHALL make that task eligible for termination by subsequent scale-in actions.
