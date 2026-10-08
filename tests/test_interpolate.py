"""Path resolution against the sample Endor Finding and Project documents."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from jira_variables_proxy.description import is_project_aggregation
from jira_variables_proxy.endor import choose_finding
from jira_variables_proxy.interpolate import InterpolationError, build_context, resolve_path, rewrite_issue


_FIXTURES = Path(__file__).resolve().parent / "fixtures"
FINDING = json.loads((_FIXTURES / "finding.json").read_text())
PROJECT = json.loads((_FIXTURES / "project.json").read_text())
CONTEXT = {"finding": FINDING, "project": PROJECT}

_PROJECT_LINK = (
    "https://app.endorlabs.com/t/david-learn/projects/69f5b8c23f06b1175a2023c0"
)


class ResolvePathTest(unittest.TestCase):
    def test_sample_finding_and_project_fields(self) -> None:
        self.assertEqual(resolve_path("finding.spec.level", CONTEXT), "FINDING_LEVEL_MEDIUM")
        self.assertEqual(
            resolve_path("project.spec.git.web_url", CONTEXT),
            "https://api.github.com/TreetopTechie/saleor",
        )
        self.assertEqual(
            resolve_path("project.spec.internal_reference_key", CONTEXT),
            "https://github.com/TreetopTechie/saleor.git",
        )
        self.assertEqual(
            resolve_path("finding.spec.finding_tags", CONTEXT),
            "FINDING_TAGS_DIRECT,FINDING_TAGS_NORMAL,FINDING_TAGS_REACHABLE_DEPENDENCY,FINDING_TAGS_NOTIFICATION",
        )
        self.assertEqual(
            resolve_path("finding.spec.location_urls.poetry.lock", CONTEXT),
            "https://github.com/TreetopTechie/saleor/blob/main/poetry.lock",
        )

    def test_absent_project_tags_are_an_error(self) -> None:
        with self.assertRaises(InterpolationError) as caught:
            resolve_path("project.meta.tags", CONTEXT)
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("absent", caught.exception.message)

    def test_tag_name_returns_its_value(self) -> None:
        project = {
            "meta": {
                "tags": [
                    "chapter=ios",
                    "squad=payments-squad",
                    "tribe=digital-channels",
                ]
            }
        }
        context = {"project": project}
        self.assertEqual(resolve_path("project.meta.tags.tribe", context), "digital-channels")
        self.assertEqual(resolve_path("project.meta.tags.squad", context), "payments-squad")
        self.assertEqual(
            resolve_path("project.meta.tags", context),
            "chapter=ios,squad=payments-squad,tribe=digital-channels",
        )

    def test_null_value_names_the_path(self) -> None:
        context = {"finding": {"spec": {"finding_metadata": {"vulnerability": None}}}}
        path = "finding.spec.finding_metadata.vulnerability.spec.cvss_v3_severity.score"
        with self.assertRaises(InterpolationError) as caught:
            resolve_path(path, context)
        self.assertEqual(caught.exception.status, 400)
        self.assertIn(path, caught.exception.message)
        self.assertIn("empty", caught.exception.message)

    def test_missing_tag_name_is_an_error(self) -> None:
        context = {"project": {"meta": {"tags": ["tribe=digital-channels"]}}}
        with self.assertRaises(InterpolationError) as caught:
            resolve_path("project.meta.tags.missing", context)
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("absent", caught.exception.message)

    def test_label_filter_title_cases_an_enum(self) -> None:
        self.assertEqual(resolve_path("finding.spec.level", CONTEXT, label=True), "Medium")
        payload = {"fields": {"customfield_10002": "{{ finding.spec.level | label }}"}}
        rewritten = rewrite_issue(payload, CONTEXT, finding_present=True)
        self.assertEqual(rewritten["fields"]["customfield_10002"], "Medium")
        for level, label in (
            ("FINDING_LEVEL_CRITICAL", "Critical"),
            ("FINDING_LEVEL_HIGH", "High"),
            ("FINDING_LEVEL_LOW", "Low"),
            ("LEVEL_MEDIUM", "Medium"),
            ("VALIDATION_STATUS_SECRET_IS_INVALID", "Secret Is Invalid"),
        ):
            context = {"finding": {"spec": {"level": level}}}
            self.assertEqual(resolve_path("finding.spec.level", context, label=True), label)

    def test_label_filter_rejects_an_unknown_value(self) -> None:
        context = {"finding": {"spec": {"level": "not-an-enum"}}}
        with self.assertRaises(InterpolationError) as caught:
            resolve_path("finding.spec.level", context, label=True)
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("no label", caught.exception.message)
        payload = {"fields": {"customfield_10002": "{{finding.spec.level | lower}}"}}
        with self.assertRaises(InterpolationError) as caught:
            rewrite_issue(payload, CONTEXT, finding_present=True)
        self.assertIn("unsupported template filter", caught.exception.message)

    def test_package_version_is_the_finding_parent(self) -> None:
        description = (
            f"*Project*: [saleor|{_PROJECT_LINK}]\n"
            "*More details*: [Finding: 6a44ee0cfc31b281d73bb9f5|"
            "https://app.endorlabs.com/t/david-learn/findings/6a44ee0cfc31b281d73bb9f5]\n"
        )

        class Endor:
            def __init__(self) -> None:
                self.package_version: tuple[str, str] | None = None

            def get_project(self, namespace: str, uuid: str) -> dict:
                return PROJECT

            def get_finding(self, namespace: str, uuid: str) -> dict:
                return FINDING

            def get_package_version(self, namespace: str, uuid: str) -> dict:
                self.package_version = (namespace, uuid)
                return {"meta": {"name": "pypi://saleor@3.22.0-a.0"}, "uuid": uuid}

        endor = Endor()
        context, finding_present = build_context(description, endor)
        self.assertTrue(finding_present)
        self.assertEqual(endor.package_version, ("david-learn", "6a44edb5006c73f62271179f"))
        self.assertEqual(resolve_path("packageversion.meta.name", context), "pypi://saleor@3.22.0-a.0")

    def test_branch_template_keeps_the_parent_that_was_loaded(self) -> None:
        branch = (
            "{{packageversion.spec.source_code_reference.version.ref}}"
            "{{repositoryversion.spec.version.ref}}"
        )
        package_context = {
            "finding": FINDING,
            "project": PROJECT,
            "packageversion": {"spec": {"source_code_reference": {"version": {"ref": "main"}}}},
        }
        repository_context = {
            "finding": FINDING,
            "project": PROJECT,
            "repositoryversion": {"spec": {"version": {"ref": "master"}}},
        }
        self.assertEqual(
            rewrite_issue({"fields": {"customfield_10001": branch}}, package_context, True)["fields"][
                "customfield_10001"
            ],
            "main",
        )
        self.assertEqual(
            rewrite_issue({"fields": {"customfield_10001": branch}}, repository_context, True)["fields"][
                "customfield_10001"
            ],
            "master",
        )

    def test_a_missing_path_on_the_loaded_object_still_nulls_the_value(self) -> None:
        payload = {
            "fields": {
                "customfield_10001": (
                    "https://app.endorlabs.com/t/{{finding.tenant_meta.namespace}}"
                    "/findings/{{finding.missing}}"
                )
            }
        }
        fields = rewrite_issue(payload, CONTEXT, finding_present=True)["fields"]
        self.assertIsNone(fields["customfield_10001"])

    def test_repository_version_is_the_finding_parent(self) -> None:
        finding = json.loads(json.dumps(FINDING))
        finding["meta"]["parent_kind"] = "RepositoryVersion"
        finding["meta"]["parent_uuid"] = "6819d2aeb9d0ea2558264469"
        description = (
            f"*Project*: [saleor|{_PROJECT_LINK}]\n"
            "*More details*: [Finding: 6a44ee0cfc31b281d73bb9f5|"
            "https://app.endorlabs.com/t/david-learn/findings/6a44ee0cfc31b281d73bb9f5]\n"
        )

        class Endor:
            def __init__(self) -> None:
                self.repository_version: tuple[str, str] | None = None

            def get_project(self, namespace: str, uuid: str) -> dict:
                return PROJECT

            def get_finding(self, namespace: str, uuid: str) -> dict:
                return finding

            def get_repository_version(self, namespace: str, uuid: str) -> dict:
                self.repository_version = (namespace, uuid)
                return {"spec": {"version": {"ref": "master"}}, "uuid": uuid}

        endor = Endor()
        context, finding_present = build_context(description, endor)
        self.assertTrue(finding_present)
        self.assertEqual(endor.repository_version, ("david-learn", "6819d2aeb9d0ea2558264469"))
        self.assertNotIn("packageversion", context)
        self.assertEqual(resolve_path("repositoryversion.spec.version.ref", context), "master")

    def test_package_version_is_null_when_the_parent_is_not_one(self) -> None:
        finding = json.loads(json.dumps(FINDING))
        finding["meta"]["parent_kind"] = "RepositoryVersion"
        payload = {"fields": {"customfield_10001": "{{packageversion.meta.name}}"}}
        fields = rewrite_issue(payload, {"finding": finding, "project": PROJECT}, finding_present=True)["fields"]
        self.assertIsNone(fields["customfield_10001"])

    def test_credential_field_is_rejected(self) -> None:
        poisoned = {"project": {"spec": {"ingestion_token": "secret"}}}
        with self.assertRaises(InterpolationError) as caught:
            resolve_path("project.spec.ingestion_token", poisoned)
        self.assertIn("credential", caught.exception.message)

    def test_highest_severity_finding_wins_and_ties_keep_order(self) -> None:
        low = {"spec": {"level": "FINDING_LEVEL_LOW"}, "uuid": "low"}
        high = {"spec": {"level": "FINDING_LEVEL_HIGH"}, "uuid": "high"}
        other = {"spec": {"level": "FINDING_LEVEL_HIGH"}, "uuid": "other"}
        self.assertEqual(choose_finding([low, high, other])["uuid"], "high")
        self.assertEqual(choose_finding([other, high])["uuid"], "other")


class RewriteIssueTest(unittest.TestCase):
    def test_parent_issue_skips_finding_bindings(self) -> None:
        payload = {
            "fields": {
                "description": f"*Project*: [saleor|{_PROJECT_LINK}]\n*Project URL*: https://github.com/TreetopTechie/saleor.git",
                "labels": ["endorlabs-scan", "{{project.spec.git.http_clone_url}}", "{{finding.spec.level}}"],
                "customfield_10001": "{{project.spec.internal_reference_key}}",
                "customfield_10002": "{{finding.spec.level}}",
                "customfield_10050": "{{finding.meta.name}}",
                "customfield_10051": "{{packageversion.meta.name}}",
            }
        }
        rewritten = rewrite_issue(payload, {"project": PROJECT}, finding_present=False)
        fields = rewritten["fields"]
        self.assertEqual(fields["description"], payload["fields"]["description"])
        self.assertEqual(
            fields["labels"],
            ["endorlabs-scan", "https://github.com/TreetopTechie/saleor.git"],
        )
        self.assertEqual(fields["customfield_10001"], "https://github.com/TreetopTechie/saleor.git")
        self.assertIsNone(fields["customfield_10002"])
        self.assertIsNone(fields["customfield_10050"])
        self.assertIsNone(fields["customfield_10051"])
        self.assertNotIn("priority", fields)

    def test_child_issue_sets_finding_bindings(self) -> None:
        description = f"*Project*: [saleor|{_PROJECT_LINK}]"
        payload = {
            "fields": {
                "description": description,
                "labels": ["Endor"],
                "customfield_10002": "{{finding.spec.level}}",
                "priority": {"name": "{{finding.spec.level}}"},
            }
        }
        rewritten = rewrite_issue(payload, CONTEXT, finding_present=True)
        self.assertEqual(rewritten["fields"]["description"], description)
        self.assertEqual(rewritten["fields"]["customfield_10002"], "FINDING_LEVEL_MEDIUM")
        self.assertEqual(rewritten["fields"]["priority"], {"name": "FINDING_LEVEL_MEDIUM"})

    def test_project_aggregation_leaves_off_finding_templates(self) -> None:
        finding_link = "https://app.endorlabs.com/t/david-learn/findings/6a44ee0cfc31b281d73bb9f5"
        description = (
            f"*Project*: [saleor|{_PROJECT_LINK}]\n"
            "*Endor-notification-uuid*: 6a44ee18e17f6edd32c349bc\n"
            "*2 findings are identified*\n"
            f"*More details*: [Finding: 6a44ee0cfc31b281d73bb9f5|{finding_link}]\n"
        )
        self.assertTrue(is_project_aggregation(description))

        class Endor:
            def get_project(self, namespace: str, uuid: str) -> dict:
                return PROJECT

            def get_finding(self, namespace: str, uuid: str) -> dict:
                raise AssertionError("project aggregation must not load a finding")

        context, finding_present = build_context(description, Endor())
        self.assertFalse(finding_present)
        self.assertEqual(context["project"]["uuid"], PROJECT["uuid"])
        payload = {
            "fields": {
                "description": description,
                "labels": ["{{project.spec.git.http_clone_url}}", "{{finding.spec.level}}"],
                "customfield_10002": "{{finding.spec.level}}",
            }
        }
        rewritten = rewrite_issue(payload, context, finding_present)
        self.assertEqual(
            rewritten["fields"]["labels"],
            ["https://github.com/TreetopTechie/saleor.git"],
        )
        self.assertIsNone(rewritten["fields"]["customfield_10002"])

    def test_unresolved_template_is_null_and_other_fields_remain(self) -> None:
        payload = {
            "fields": {
                "customfield_10020": "{{finding.spec.level | label}}",
                "customfield_10021": (
                    "{{finding.spec.finding_metadata.vulnerability.spec.cvss_v3_severity.score}}"
                ),
                "customfield_10022": "{{project.meta.tags.squad}}",
            }
        }
        context = {
            "finding": {"spec": {"level": "FINDING_LEVEL_CRITICAL", "finding_metadata": {"vulnerability": None}}},
            "project": {"meta": {"tags": ["Grade:F"]}},
        }
        fields = rewrite_issue(payload, context, finding_present=True)["fields"]
        self.assertEqual(fields["customfield_10020"], "Critical")
        self.assertIsNone(fields["customfield_10021"])
        self.assertIsNone(fields["customfield_10022"])

    def test_dependency_child_is_not_project_aggregation(self) -> None:
        description = (
            f"*Project*: [saleor|{_PROJECT_LINK}]\n"
            "*Dependency*: pypi://google-cloud-storage@2.19.0\n"
            "*Endor-notification-uuid*: 6a44ee18e17f6edd32c349bc\n"
            "*1 finding is identified*\n"
        )
        self.assertFalse(is_project_aggregation(description))
        self.assertFalse(is_project_aggregation(description, "Sub-task"))

    def test_label_with_a_space_is_rejected(self) -> None:
        payload = {"fields": {"labels": ["{{finding.meta.description}}"]}}
        with self.assertRaises(InterpolationError):
            rewrite_issue(payload, CONTEXT, finding_present=True)


if __name__ == "__main__":
    unittest.main()
