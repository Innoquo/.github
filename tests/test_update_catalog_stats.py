from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "update_catalog_stats.py"
SPEC = importlib.util.spec_from_file_location("catalog_stats", SCRIPT)
catalog_stats = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = catalog_stats
SPEC.loader.exec_module(catalog_stats)


def repository(name: str) -> dict[str, object]:
    return {
        "name": name,
        "full_name": f"Innoquo/{name}",
        "html_url": f"https://github.com/Innoquo/{name}",
        "default_branch": "main",
        "archived": False,
        "disabled": False,
        "fork": False,
    }


def manifest(
    name: str,
    *,
    lab_id: str = "061",
    priority: int = 1,
    status: str = "L3",
    failure_tested: bool = True,
    cloud_verified: bool = False,
) -> str:
    last_verified = "last_verified: 2026-08-08\n" if status in {"L3", "L4", "L5"} else ""
    return (
        "schema_version: 1\n"
        f"id: {lab_id}\n"
        f"priority: {priority}\n"
        f"slug: {name}\n"
        "track: aws-bedrock-agentcore\n"
        f"status: {status}\n"
        "visibility: public\n"
        "languages: [en, fr]\n"
        "reusable_asset: example\n"
        f"failure_tested: {str(failure_tested).lower()}\n"
        f"cloud_verified: {str(cloud_verified).lower()}\n"
        f"{last_verified}"
    )


class FakeClient:
    def __init__(self, manifests: dict[str, str | None]) -> None:
        self.manifests = manifests

    def read_manifest(self, repo: dict[str, object]) -> str | None:
        return self.manifests[str(repo["name"])]


class ParseManifestTests(unittest.TestCase):
    def test_parses_flat_yaml_with_inline_comment(self) -> None:
        parsed = catalog_stats.parse_flat_yaml(
            "schema_version: 1 # contract\nstatus: 'L3'\n"
        )
        self.assertEqual({"schema_version": "1", "status": "L3"}, parsed)

    def test_rejects_nested_yaml(self) -> None:
        with self.assertRaisesRegex(catalog_stats.CatalogueError, "nested YAML"):
            catalog_stats.parse_flat_yaml("root:\n  child: value\n")


class ValidationTests(unittest.TestCase):
    def test_valid_l3_manifest(self) -> None:
        repo = repository("bedrock-fastapi-runtime")
        lab = catalog_stats.validate_manifest(
            catalog_stats.parse_flat_yaml(manifest(str(repo["name"]))), repo
        )
        self.assertEqual("L3", lab.status)
        self.assertTrue(lab.failure_tested)

    def test_l3_requires_failure_test(self) -> None:
        repo = repository("bedrock-fastapi-runtime")
        with self.assertRaisesRegex(catalog_stats.CatalogueError, "failure_tested"):
            catalog_stats.validate_manifest(
                catalog_stats.parse_flat_yaml(
                    manifest(str(repo["name"]), failure_tested=False)
                ),
                repo,
            )

    def test_l4_requires_cloud_verification(self) -> None:
        repo = repository("bedrock-fastapi-runtime")
        with self.assertRaisesRegex(catalog_stats.CatalogueError, "cloud_verified"):
            catalog_stats.validate_manifest(
                catalog_stats.parse_flat_yaml(
                    manifest(str(repo["name"]), status="L4", cloud_verified=False)
                ),
                repo,
            )

    def test_slug_must_match_repository(self) -> None:
        repo = repository("different-name")
        with self.assertRaisesRegex(catalog_stats.CatalogueError, "does not match"):
            catalog_stats.validate_manifest(
                catalog_stats.parse_flat_yaml(manifest("declared-name")), repo
            )


class CollectionTests(unittest.TestCase):
    def test_counts_only_repositories_with_manifests(self) -> None:
        repos = [repository("lab-one"), repository("documentation")]
        labs = catalog_stats.collect_labs(
            repos,
            FakeClient(
                {
                    "lab-one": manifest("lab-one", status="L2"),
                    "documentation": None,
                }
            ),
        )
        catalogue = catalog_stats.build_catalogue(labs, "Innoquo")
        self.assertEqual(1, catalogue["published_labs"])
        self.assertEqual(0, catalogue["featured_l3_plus"])

    def test_rejects_duplicate_ids(self) -> None:
        repos = [repository("lab-one"), repository("lab-two")]
        with self.assertRaisesRegex(catalog_stats.CatalogueError, "already used"):
            catalog_stats.collect_labs(
                repos,
                FakeClient(
                    {
                        "lab-one": manifest("lab-one", priority=1),
                        "lab-two": manifest("lab-two", priority=2),
                    }
                ),
            )


class ReadmeTests(unittest.TestCase):
    def test_updates_each_badge_once(self) -> None:
        readme = (
            "https://img.shields.io/badge/published%20labs-0-green?style=flat-square\n"
            "https://img.shields.io/badge/featured%20L3%2B-0-gold?style=flat-square\n"
        )
        updated = catalog_stats.update_readme(readme, 12, 4)
        self.assertIn("published%20labs-12-", updated)
        self.assertIn("featured%20L3%2B-4-", updated)

    def test_atomic_write_avoids_content_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "generated.json"
            catalog_stats.atomic_write(path, "{}\n")
            self.assertEqual("{}\n", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
