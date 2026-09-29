output "voice_sessions_table_name" {
  description = "Name of the voice-sessions table (Voice_Service SESSIONS_TABLE_NAME)."
  value       = aws_dynamodb_table.voice_sessions.name
}

output "voice_sessions_table_arn" {
  description = "ARN of the voice-sessions table, for task-role policy scoping."
  value       = aws_dynamodb_table.voice_sessions.arn
}

output "voice_sessions_by_engineer_gsi_arn" {
  description = "ARN of the by-engineer global secondary index on the voice-sessions table, for task-role policy scoping."
  value       = "${aws_dynamodb_table.voice_sessions.arn}/index/${local.by_engineer_index_name}"
}

output "agent_chats_table_name" {
  description = "Name of the agent-chats table (Voice_Service CHATS_TABLE_NAME)."
  value       = aws_dynamodb_table.agent_chats.name
}

output "agent_chats_table_arn" {
  description = "ARN of the agent-chats table, for task-role policy scoping."
  value       = aws_dynamodb_table.agent_chats.arn
}

output "push_subscriptions_table_name" {
  description = "Name of the push-subscriptions table (Voice_Service SUBSCRIPTIONS_TABLE_NAME and Notifier SUBSCRIPTIONS_TABLE_NAME)."
  value       = aws_dynamodb_table.push_subscriptions.name
}

output "push_subscriptions_table_arn" {
  description = "ARN of the push-subscriptions table, for task-role and notifier-role policy scoping."
  value       = aws_dynamodb_table.push_subscriptions.arn
}

output "transcripts_table_name" {
  description = "Name of the transcripts table (Voice_Service TRANSCRIPTS_TABLE_NAME)."
  value       = aws_dynamodb_table.transcripts.name
}

output "transcripts_table_arn" {
  description = "ARN of the transcripts table, for task-role policy scoping."
  value       = aws_dynamodb_table.transcripts.arn
}
