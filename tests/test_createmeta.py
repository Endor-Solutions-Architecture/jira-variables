"""Custom-field createmeta is relaxed for Endor and restored for Jira."""

from __future__ import annotations

import unittest

from jira_variables_proxy.createmeta import relax_createmeta, restore_custom_value
from jira_variables_proxy.interpolate import InterpolationError


def _meta() -> dict:
    return {
        "fields": [
            {
                "name": "Severity",
                "fieldId": "customfield_10020",
                "schema": {"type": "option"},
                "allowedValues": [
                    {"value": "Critical"},
                    {"value": "High"},
                    {"value": "Medium"},
                    {"value": "Low"},
                ],
            },
            {
                "name": "CVSS 3.1",
                "fieldId": "customfield_10021",
                "schema": {"type": "number"},
            },
            {
                "name": "Priority",
                "fieldId": "priority",
                "schema": {"type": "priority"},
                "allowedValues": [{"name": "Medium", "id": "3"}],
            },
        ]
    }


class CreatemetaTest(unittest.TestCase):
    def test_relax_hides_custom_field_constraints_and_priority_options(self) -> None:
        payload = _meta()
        relax_createmeta(payload)
        severity, score, priority = payload["fields"]
        self.assertNotIn("allowedValues", severity)
        self.assertEqual(score["schema"]["type"], "string")
        self.assertNotIn("allowedValues", priority)

    def test_restore_sends_priority_as_a_name(self) -> None:
        priority = _meta()["fields"][2]
        self.assertEqual(restore_custom_value("Medium", priority), {"name": "Medium"})
        with self.assertRaises(InterpolationError) as caught:
            restore_custom_value("Critical", priority)
        self.assertIn("Priority must be one of Medium", caught.exception.message)

    def test_restore_wraps_a_select_and_sends_a_number(self) -> None:
        severity, score, _priority = _meta()["fields"]
        self.assertEqual(restore_custom_value("Medium", severity), {"value": "Medium"})
        self.assertEqual(restore_custom_value("5.3", score), 5.3)
        self.assertEqual(restore_custom_value("5", score), 5)

    def test_textarea_on_v3_is_an_adf_document(self) -> None:
        field = {
            "name": "References",
            "fieldId": "customfield_10048",
            "schema": {
                "type": "string",
                "custom": "com.atlassian.jira.plugin.system.customfieldtypes:textarea",
            },
        }
        document = restore_custom_value("https://app.endorlabs.com/t/david-learn/findings/abc", field)
        self.assertEqual(
            document,
            {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "https://app.endorlabs.com/t/david-learn/findings/abc",
                            }
                        ],
                    }
                ],
            },
        )
        self.assertEqual(
            restore_custom_value("same text", field, api_version="2"),
            "same text",
        )
        self.assertIsNone(restore_custom_value(None, field))

    def test_restore_rejects_an_option_jira_does_not_have(self) -> None:
        severity = _meta()["fields"][0]
        with self.assertRaises(InterpolationError) as caught:
            restore_custom_value("Urgent", severity)
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("Severity must be one of Critical, High, Medium, Low", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
