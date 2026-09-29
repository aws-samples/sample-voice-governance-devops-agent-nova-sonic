output "api_id" {
  description = "ID of the AppSync Events API."
  value       = aws_appsync_api.this.api_id
}

output "api_arn" {
  description = "ARN of the AppSync Events API, for scoping the Notifier Lambda's appsync:EventPublish permission."
  value       = aws_appsync_api.this.api_arn
}

output "channel_namespace_arn" {
  description = "ARN of the incidents channel namespace."
  value       = aws_appsync_channel_namespace.incidents.channel_namespace_arn
}

output "http_endpoint" {
  description = "HTTP publish endpoint of the Events API (Notifier APPSYNC_EVENTS_HTTP_ENDPOINT and frontend config.json events.httpEndpoint)."
  value       = "https://${aws_appsync_api.this.dns["HTTP"]}/event"
}

output "realtime_endpoint" {
  description = "Realtime WebSocket endpoint of the Events API (frontend config.json events.realtimeEndpoint and Voice_Service APPSYNC_EVENTS_REALTIME_ENDPOINT)."
  value       = "wss://${aws_appsync_api.this.dns["REALTIME"]}/event/realtime"
}
