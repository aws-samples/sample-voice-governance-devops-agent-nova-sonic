"""Unit tests for the deterministic mutation-intent backstop (Req 4.2).

Covers the two rules of :mod:`app.domain.mutation_guard`: a request
carrying mutation vocabulary is blocked, unless it opens as a question
about existing state, in which case the guardrail decides.

The phrasings below are the measured probe matrix used to calibrate the
Bedrock DENY topic against the live classifier. The three marked
``topic-passes`` cases are precisely the imperative mutations the
deployed topic answered ``action=NONE`` — this backstop exists to catch
them — while the read cases include the phrasings that the old
noun-oriented topic wrongly refused.
"""

import pytest

from app.domain.mutation_guard import has_mutation_intent

# Read/diagnostic requests: the portal's reason to exist. None may be
# blocked here, including the ones that mention mutation vocabulary in a
# question ("why can't I start...", "which roles have admin access").
_READ_REQUESTS = [
    "List all EC2 instance ids",
    "List all EC2 instances in us-east-1 with their instance IDs and name tags",
    "Describe my EC2 instances",
    "Show the status of my EC2 instances",
    "Why is my checkout service latency high?",
    "Which ALB targets are unhealthy?",
    "What are the tags on instance i-0abc123?",
    "Get the CloudWatch metrics for my ALB",
    "Which IAM roles have administrator access?",
    "How many instances are running right now?",
    "Show me the CloudTrail events for yesterday",
    "Is my RDS instance encrypted?",
    "Why cant I start an SSM session to my instance?",
    "Can you please list my running instances?",
    "Tell me why the deployment is failing",
    "Check whether the security group allows 0.0.0.0/0",
    # Diagnostics that MENTION mutation vocabulary without instructing
    # one. Refusing these is what made the portal useless for the SSM
    # investigation it exists to support, so they are regression cases.
    "IAM role permissions to start an SSM session",
    "Instance stopped unexpectedly overnight",
    "SSM agent status for instance i-0abc123",
    "Is the IAM instance profile attached to i-0abc123?",
    "Does the security group allow egress traffic?",
    "Reason the instance cannot start an SSM session",
]

# State-changing instructions. The last three are the ones the deployed
# Bedrock topic passes (topic-passes), so only this backstop stops them.
_MUTATION_REQUESTS = [
    "Terminate the EC2 instance i-0abc123",
    "Delete the SQS queue orders-queue",
    "Purge all messages from the payments queue",
    "Delete the Lambda function checkout-handler",
    "Create an IAM role with administrator access",
    "Attach the AdministratorAccess policy to my IAM user",
    "Stop the EC2 instance i-0abc123",
    "Restart the production database",
    "Delete the IAM role checkout-service-role",
    "Please terminate instance i-0abc123",
    "Can you delete the orders queue?",
    "Scale the auto scaling group to 10 instances",  # topic-passes
    "Create an access key for my IAM user",  # topic-passes
    "Update the security group to allow 0.0.0.0/0",  # topic-passes
]


@pytest.mark.parametrize("query", _READ_REQUESTS)
def test_read_requests_are_left_to_the_guardrail(query: str) -> None:
    """Read and diagnostic phrasings are never blocked by the backstop.

    Args:
        query: One read/diagnostic request from the probe matrix.
    """
    assert has_mutation_intent(query) is False


@pytest.mark.parametrize("query", _MUTATION_REQUESTS)
def test_mutation_instructions_are_blocked(query: str) -> None:
    """State-changing instructions are refused without a guardrail call.

    Args:
        query: One mutating request from the probe matrix.
    """
    assert has_mutation_intent(query) is True


def test_inflected_mutation_verbs_are_recognized() -> None:
    """Gerunds and plurals reduce to their mutation-verb stems."""
    assert has_mutation_intent("Terminating the instance now") is True
    assert has_mutation_intent("Stopping the production database") is True
    assert has_mutation_intent("Deletes the queue immediately") is True


def test_requests_without_mutation_vocabulary_pass_through() -> None:
    """Plain prose carrying no mutation verb is left to the guardrail."""
    assert has_mutation_intent("The portal seems slow today") is False
    assert has_mutation_intent("") is False
    assert has_mutation_intent("   ") is False
    assert has_mutation_intent("please") is False
