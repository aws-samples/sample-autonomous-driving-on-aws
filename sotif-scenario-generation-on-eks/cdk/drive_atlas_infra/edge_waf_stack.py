"""CloudFront-scope WAF (must live in us-east-1).

AWS managed rules (known bad inputs such as Log4j JNDI lookups, IP reputation) block first, for every caller.
Then listed CIDRs are allowed; everyone else is blocked.
"""

from typing import List

from aws_cdk import Stack, aws_wafv2 as wafv2
from constructs import Construct


def _visibility(metric_name: str) -> wafv2.CfnWebACL.VisibilityConfigProperty:
    return wafv2.CfnWebACL.VisibilityConfigProperty(
        cloud_watch_metrics_enabled=True, metric_name=metric_name, sampled_requests_enabled=True
    )


def _managed_rule(name: str, priority: int) -> wafv2.CfnWebACL.RuleProperty:
    return wafv2.CfnWebACL.RuleProperty(
        name=name,
        priority=priority,
        override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
        statement=wafv2.CfnWebACL.StatementProperty(
            managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(vendor_name="AWS", name=name)
        ),
        visibility_config=_visibility(f"drive-atlas-{name}"),
    )


class EdgeWafStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, allowed_cidrs: List[str], **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        ip_set = wafv2.CfnIPSet(
            self,
            "AllowedIps",
            scope="CLOUDFRONT",
            ip_address_version="IPV4",
            addresses=allowed_cidrs,
        )
        rules = [
            _managed_rule("AWSManagedRulesKnownBadInputsRuleSet", 0),
            _managed_rule("AWSManagedRulesAmazonIpReputationList", 1),
            wafv2.CfnWebACL.RuleProperty(
                name="allow-listed-cidrs",
                priority=2,
                action=wafv2.CfnWebACL.RuleActionProperty(allow={}),
                statement=wafv2.CfnWebACL.StatementProperty(
                    ip_set_reference_statement=wafv2.CfnWebACL.IPSetReferenceStatementProperty(arn=ip_set.attr_arn)
                ),
                visibility_config=_visibility("drive-atlas-allowed"),
            ),
        ]
        self.web_acl = wafv2.CfnWebACL(
            self,
            "WebAcl",
            scope="CLOUDFRONT",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(block={}),
            visibility_config=_visibility("drive-atlas-edge"),
            rules=rules,
        )
