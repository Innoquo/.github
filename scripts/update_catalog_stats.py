#!/usr/bin/env python3
"""Update INNOQUO catalogue metrics from public repository lab manifests.

The script uses only the Python standard library. It reads a flat, versioned
``lab.yaml`` contract from the root of each public, non-archived INNOQUO
repository. Repositories without a manifest are ignored. Invalid manifests
fail the run so the organisation profile cannot publish misleading metrics.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable


API_VERSION = "2022-11-28"
VALID_STATUSES = {f"L{level}": level for level in range(6)}
REQUIRED_FIELDS = {
    "schema_version",
    "id",
    "priority",
    "slug",
    "track",
    "status",
    "visibility",
    "failure_tested",
    "cloud_verified",
}

PUBLISHED_BADGE = re.compile(
    r"(https://img\.shields\.io/badge/published%20labs-)\d+(-[^)\s]+)"
)
FEATURED_BADGE = re.compile(
    r"(https://img\.shields\.io/badge/featured%20L3%2B-)\d+(-[^)\s]+)"
)


class CatalogueError(RuntimeError):
    """Raised when the catalogue cannot be updated safely."""


class NotFoundError(CatalogueError):
    """Raised when an optional GitHub resource does not exist."""


@dataclass(frozen=True)
class Lab:
    id: str
    priority: int
    slug: str
    track: str
    status: str
    repository: str
    url: str
    failure_tested: bool
    cloud_verified: bool
    last_verified: str | None


def strip_inline_comment(value: str) -> str:
    """Remove an unquoted YAML comment from a scalar value."""
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote == '"':
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if character == "#" and quote is None and (
            index == 0 or value[index - 1].isspace()
        ):
            return value[:index].rstrip()
    return value.strip()


def parse_scalar(value: str) -> str:
    value = strip_inline_comment(value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def parse_flat_yaml(content: str) -> dict[str, str]:
    """Parse the flat scalar subset used by the versioned lab manifest."""
    result: dict[str, str] = {}
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line[:1].isspace():
            raise CatalogueError(
                f"lab.yaml:{line_number}: nested YAML is not allowed in schema v1"
            )
        if ":" not in raw_line:
            raise CatalogueError(
                f"lab.yaml:{line_number}: expected a top-level key/value pair"
            )
        key, value = raw_line.split(":", 1)
        key = key.strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            raise CatalogueError(f"lab.yaml:{line_number}: invalid key {key!r}")
        if key in result:
            raise CatalogueError(f"lab.yaml:{line_number}: duplicate key {key!r}")
        result[key] = parse_scalar(value)
    return result


def parse_bool(value: str, field: str) -> bool:
    normalized = value.lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise CatalogueError(f"{field} must be true or false")


def parse_positive_int(value: str, field: str) -> int:
    if not re.fullmatch(r"[0-9]+", value):
        raise CatalogueError(f"{field} must be an integer")
    parsed = int(value)
    if parsed < 1:
        raise CatalogueError(f"{field} must be greater than zero")
    return parsed


def validate_manifest(
    manifest: dict[str, str], repository: dict[str, Any]
) -> Lab:
    missing = sorted(REQUIRED_FIELDS - manifest.keys())
    if missing:
        raise CatalogueError(f"missing required fields: {', '.join(missing)}")

    if manifest["schema_version"] != "1":
        raise CatalogueError("schema_version must be 1")

    lab_id = manifest["id"]
    if not re.fullmatch(r"[0-9]{3}", lab_id):
        raise CatalogueError("id must contain exactly three digits")

    priority = parse_positive_int(manifest["priority"], "priority")
    repository_name = str(repository["name"])
    if manifest["slug"] != repository_name:
        raise CatalogueError(
            f"slug {manifest['slug']!r} does not match repository {repository_name!r}"
        )

    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", manifest["track"]):
        raise CatalogueError("track must be a lowercase kebab-case identifier")

    status = manifest["status"].upper()
    if status not in VALID_STATUSES:
        raise CatalogueError("status must be one of L0, L1, L2, L3, L4 or L5")

    if manifest["visibility"].lower() != "public":
        raise CatalogueError("a lab in a public repository must declare visibility: public")

    failure_tested = parse_bool(manifest["failure_tested"], "failure_tested")
    cloud_verified = parse_bool(manifest["cloud_verified"], "cloud_verified")
    evidence_level = VALID_STATUSES[status]

    last_verified = manifest.get("last_verified") or None
    if evidence_level >= 3:
        if not failure_tested:
            raise CatalogueError(f"{status} requires failure_tested: true")
        if not last_verified:
            raise CatalogueError(f"{status} requires last_verified")
        try:
            verified_date = date.fromisoformat(last_verified)
        except ValueError as error:
            raise CatalogueError("last_verified must use YYYY-MM-DD") from error
        if verified_date > date.today():
            raise CatalogueError("last_verified cannot be in the future")

    if evidence_level >= 4 and not cloud_verified:
        raise CatalogueError(f"{status} requires cloud_verified: true")

    return Lab(
        id=lab_id,
        priority=priority,
        slug=manifest["slug"],
        track=manifest["track"],
        status=status,
        repository=str(repository["full_name"]),
        url=str(repository["html_url"]),
        failure_tested=failure_tested,
        cloud_verified=cloud_verified,
        last_verified=last_verified,
    )


class GitHubClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com") -> None:
        if not token:
            raise CatalogueError("GITHUB_TOKEN is required")
        self.token = token
        self.api_url = api_url.rstrip("/")

    def get_json(self, path: str, *, optional: bool = False) -> Any:
        request = urllib.request.Request(
            f"{self.api_url}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "innoquo-catalogue-stats",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404 and optional:
                raise NotFoundError(path) from error
            remaining = error.headers.get("x-ratelimit-remaining", "unknown")
            raise CatalogueError(
                f"GitHub API returned {error.code} for {path}; "
                f"rate-limit remaining: {remaining}"
            ) from error
        except urllib.error.URLError as error:
            raise CatalogueError(f"GitHub API request failed for {path}: {error.reason}") from error

    def list_public_repositories(self, organization: str) -> list[dict[str, Any]]:
        repositories: list[dict[str, Any]] = []
        page = 1
        while True:
            query = urllib.parse.urlencode(
                {"type": "public", "sort": "full_name", "per_page": 100, "page": page}
            )
            batch = self.get_json(f"/orgs/{organization}/repos?{query}")
            if not isinstance(batch, list):
                raise CatalogueError("GitHub repositories response was not a list")
            repositories.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return repositories

    def read_manifest(self, repository: dict[str, Any]) -> str | None:
        full_name = str(repository["full_name"])
        ref = urllib.parse.quote(str(repository["default_branch"]), safe="")
        try:
            payload = self.get_json(
                f"/repos/{full_name}/contents/lab.yaml?ref={ref}", optional=True
            )
        except NotFoundError:
            return None
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            raise CatalogueError(f"{full_name}: lab.yaml was not returned as base64 content")
        try:
            return base64.b64decode(payload["content"], validate=False).decode("utf-8")
        except (KeyError, ValueError, UnicodeDecodeError) as error:
            raise CatalogueError(f"{full_name}: could not decode lab.yaml") from error


def collect_labs(
    repositories: Iterable[dict[str, Any]], client: GitHubClient
) -> list[Lab]:
    labs: list[Lab] = []
    errors: list[str] = []
    seen_ids: dict[str, str] = {}
    seen_priorities: dict[int, str] = {}

    for repository in repositories:
        if repository.get("archived") or repository.get("disabled") or repository.get("fork"):
            continue
        content = client.read_manifest(repository)
        if content is None:
            continue
        full_name = str(repository.get("full_name", repository.get("name", "unknown")))
        try:
            lab = validate_manifest(parse_flat_yaml(content), repository)
            if lab.id in seen_ids:
                raise CatalogueError(
                    f"id {lab.id} is already used by {seen_ids[lab.id]}"
                )
            if lab.priority in seen_priorities:
                raise CatalogueError(
                    f"priority {lab.priority} is already used by "
                    f"{seen_priorities[lab.priority]}"
                )
            seen_ids[lab.id] = full_name
            seen_priorities[lab.priority] = full_name
            labs.append(lab)
        except CatalogueError as error:
            errors.append(f"{full_name}: {error}")

    if errors:
        formatted = "\n".join(f"- {error}" for error in errors)
        raise CatalogueError(f"invalid public lab manifests:\n{formatted}")

    return sorted(labs, key=lambda lab: (lab.priority, lab.slug))


def build_catalogue(labs: list[Lab], organization: str) -> dict[str, Any]:
    evidence_levels = {status: 0 for status in VALID_STATUSES}
    for lab in labs:
        evidence_levels[lab.status] += 1
    return {
        "schema_version": 1,
        "organization": organization,
        "roadmap_labs": 150,
        "engineering_tracks": 15,
        "published_labs": len(labs),
        "featured_l3_plus": sum(
            1 for lab in labs if VALID_STATUSES[lab.status] >= 3
        ),
        "evidence_levels": evidence_levels,
        "labs": [asdict(lab) for lab in labs],
    }


def replace_exactly_once(
    content: str, pattern: re.Pattern[str], value: int, label: str
) -> str:
    updated, replacements = pattern.subn(
        lambda match: f"{match.group(1)}{value}{match.group(2)}", content
    )
    if replacements != 1:
        raise CatalogueError(
            f"expected exactly one {label} badge in profile README; found {replacements}"
        )
    return updated


def update_readme(content: str, published: int, featured: int) -> str:
    content = replace_exactly_once(
        content, PUBLISHED_BADGE, published, "published labs"
    )
    return replace_exactly_once(
        content, FEATURED_BADGE, featured, "featured L3+"
    )


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--org", default=os.environ.get("GITHUB_REPOSITORY_OWNER", "Innoquo")
    )
    parser.add_argument("--readme", type=Path, default=Path("profile/README.md"))
    parser.add_argument(
        "--output", type=Path, default=Path("generated/catalog-stats.json")
    )
    parser.add_argument(
        "--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com")
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        readme = args.readme.read_text(encoding="utf-8")
        client = GitHubClient(os.environ.get("GITHUB_TOKEN", ""), args.api_url)
        repositories = client.list_public_repositories(args.org)
        labs = collect_labs(repositories, client)
        catalogue = build_catalogue(labs, args.org)
        updated_readme = update_readme(
            readme,
            catalogue["published_labs"],
            catalogue["featured_l3_plus"],
        )
        output = json.dumps(catalogue, indent=2, sort_keys=True) + "\n"

        print(
            f"published={catalogue['published_labs']} "
            f"featured_l3_plus={catalogue['featured_l3_plus']}"
        )
        if not args.dry_run:
            atomic_write(args.readme, updated_readme)
            atomic_write(args.output, output)
        return 0
    except (CatalogueError, OSError) as error:
        print(f"catalogue update failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
