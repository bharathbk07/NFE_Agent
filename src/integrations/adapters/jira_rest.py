"""Jira REST integration adapter (product source of truth)."""
from __future__ import annotations

from typing import Any, Dict, List

from src.integrations.adapters import IntegrationAdapter


class JiraRestAdapter(IntegrationAdapter):
    id = "jira_rest"
    capabilities = ["jira", "alm"]

    def list_items(self, **kwargs: Any) -> List[Dict[str, Any]]:
        from src.nodes.jira_story import _list_eligible_issues

        return [
            {
                "key": i.key,
                "summary": i.summary,
                "status": i.status,
                "labels": list(i.labels or []),
            }
            for i in _list_eligible_issues()
        ]

    def get_item(self, key: str, **kwargs: Any) -> Dict[str, Any]:
        from src.integrations.jira.client import JiraClient

        issue = JiraClient().get_issue(key)
        return {
            "key": issue.key,
            "summary": issue.summary,
            "status": issue.status,
            "labels": list(issue.labels or []),
            "description_preview": (issue.description or "")[:500],
        }
