"""Registry API endpoints — search, pull, publish with version support."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from aqm.core.project import (
    get_agents_yaml_path,
    get_pipeline_path,
    save_pipeline,
)

logger = logging.getLogger(__name__)


class PullRequest(BaseModel):
    pipeline_name: str
    version: Optional[str] = None
    repo: Optional[str] = None
    offline: bool = False


class PublishRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    version: Optional[str] = None
    local_only: bool = False
    pipeline: Optional[str] = None


# Chinese keyword -> English registry terms.  The GitHub index only carries
# English pipeline names/tags (and mostly empty descriptions), so a Chinese
# query needs expanding before matching.
_QUERY_ALIASES: dict[str, tuple[str, ...]] = {
    "代码": ("code", "software", "feature", "review"),
    "开发": ("code", "software", "feature"),
    "编程": ("code", "software"),
    "审查": ("review",),
    "评审": ("review", "design"),
    "检查": ("review",),
    "内容": ("content", "blog", "seo"),
    "写作": ("content", "blog"),
    "文案": ("content", "blog"),
    "博客": ("blog", "seo"),
    "数据": ("data", "analysis"),
    "分析": ("analysis", "data"),
    "报表": ("data", "analysis"),
    "客服": ("customer", "support"),
    "支持": ("support", "customer"),
    "事故": ("incident", "response"),
    "故障": ("incident", "response"),
    "应急": ("incident", "response"),
    "法务": ("legal", "review"),
    "法律": ("legal",),
    "合规": ("legal", "review"),
    "入职": ("onboarding",),
    "发布": ("release", "publish"),
    "上线": ("release",),
    "设计": ("design", "api"),
    "接口": ("api",),
    "文档": ("content", "blog"),
    "运维": ("incident", "release", "devops"),
    "测试": ("review", "release"),
}


def _query_terms(query: str | None) -> list[str]:
    """Expand a (possibly Chinese) query into lowercase match terms."""
    q = (query or "").strip().lower()
    if not q:
        return []
    terms: list[str] = [q]
    for zh, words in _QUERY_ALIASES.items():
        if zh in q:
            terms.extend(words)
    seen: set[str] = set()
    unique: list[str] = []
    for t in terms:
        if t and t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


def _matches(name: str, description: str, tags: list[str], terms: list[str]) -> bool:
    if not terms:
        return True
    n = (name or "").lower()
    d = (description or "").lower()
    t = [str(x).lower() for x in (tags or [])]
    return any(term in n or term in d or any(term in tag for tag in t) for term in terms)


def create_registry_router(project_root: Path) -> APIRouter:
    router = APIRouter()
    agents_yaml_path = get_agents_yaml_path(project_root)

    @router.get("/api/registry/search")
    async def api_search(
        query: Optional[str] = Query(None),
        offline: bool = Query(False),
    ):
        from aqm.registry import search_github, DEFAULT_REGISTRY_REPO, list_local_versions

        results = []
        terms = _query_terms(query)

        # GitHub search (fetch the index, then match locally so Chinese aliases work)
        if not offline:
            github_results = search_github(query=None, repo=DEFAULT_REGISTRY_REPO)
            for m in github_results:
                if not _matches(m.name, m.description, m.tags, terms):
                    continue
                results.append({
                    "name": m.name,
                    "description": m.description,
                    "author": m.author,
                    "version": m.version,
                    "versions": m.versions,
                    "latest": m.latest,
                    "tags": m.tags,
                    "agents_count": m.agents_count,
                    "source": "github",
                })

        # Local registry (versioned)
        seen = {r["name"] for r in results}
        local_dir = Path.home() / ".aqm" / "registry"
        if local_dir.is_dir():
            for d in sorted(local_dir.iterdir()):
                if not d.is_dir():
                    continue
                local_versions = list_local_versions(d.name)
                # Check if there's any version or legacy agents.yaml
                has_content = local_versions or (d / "agents.yaml").exists()
                if has_content and d.name not in seen:
                    desc = ""
                    tags: list[str] = []
                    # Try to read meta from latest version
                    if local_versions:
                        latest_v = local_versions[-1]
                        meta_path = d / latest_v / "meta.json"
                        if meta_path.exists():
                            try:
                                meta = json.loads(meta_path.read_text("utf-8"))
                                desc = meta.get("description", "")
                                tags = meta.get("tags", []) or []
                            except Exception:
                                pass
                    if not _matches(d.name, desc, tags, terms):
                        continue
                    results.append({
                        "name": d.name,
                        "description": desc,
                        "author": "",
                        "version": local_versions[-1] if local_versions else "",
                        "versions": local_versions,
                        "latest": local_versions[-1] if local_versions else "",
                        "tags": tags,
                        "agents_count": 0,
                        "source": "local",
                    })

        return results

    @router.post("/api/registry/pull")
    async def api_pull(req: PullRequest):
        import yaml as _yaml
        from aqm.registry import (
            DEFAULT_REGISTRY_REPO,
            parse_name_version,
            pull_from_github,
            pull_from_local,
            save_to_local_registry,
        )

        name, inline_version = parse_name_version(req.pipeline_name)
        version = req.version or inline_version

        content = None
        source = ""
        pulled_version = ""

        # GitHub
        if not req.offline:
            result = pull_from_github(name, version=version, repo=req.repo or DEFAULT_REGISTRY_REPO)
            if result:
                content, meta = result
                pulled_version = meta.version or version or ""
                source = "github"

        # Local registry
        if content is None:
            result = pull_from_local(name, version=version)
            if result:
                content, meta = result
                pulled_version = meta.version or version or ""
                source = "local"

        if content is None:
            raise HTTPException(404, f"Pipeline '{name}' not found")

        # Save to project pipelines
        save_pipeline(project_root, name, content)

        # Cache in local registry
        if pulled_version:
            save_to_local_registry(name, pulled_version, content)

        data = _yaml.safe_load(content)
        agents_count = len(data.get("agents", []))

        return {
            "success": True,
            "pipeline_name": name,
            "version": pulled_version,
            "source": source,
            "agents_count": agents_count,
        }

    @router.post("/api/registry/publish")
    async def api_publish(req: PublishRequest):
        import yaml as _yaml
        from aqm.registry import (
            DEFAULT_REGISTRY_REPO,
            increment_version,
            list_versions,
            publish_to_github,
            save_to_local_registry,
            version_sort_key,
        )

        if req.pipeline:
            try:
                source_path = get_pipeline_path(project_root, req.pipeline)
            except FileNotFoundError:
                raise HTTPException(404, f"Pipeline '{req.pipeline}' not found")
        else:
            source_path = agents_yaml_path

        if not source_path.exists():
            raise HTTPException(400, "No pipeline YAML found to publish")

        content = source_path.read_text(encoding="utf-8")
        data = _yaml.safe_load(content)

        if not isinstance(data, dict) or "agents" not in data:
            raise HTTPException(400, "Invalid agents.yaml")

        pipeline_name = req.name or req.pipeline or project_root.name
        agents_count = len(data.get("agents", []))

        # Determine version
        version = req.version
        if not version:
            existing = list_versions(pipeline_name, repo=DEFAULT_REGISTRY_REPO)
            all_v = sorted(
                set(existing.get("github", []) + existing.get("local", [])),
                key=version_sort_key,
            )
            version = increment_version(all_v[-1]) if all_v else "1.0.0"

        # Save to local registry (versioned)
        meta_dict = {
            "name": pipeline_name,
            "description": req.description or "",
            "version": version,
            "agents_count": agents_count,
        }
        ver_dir = save_to_local_registry(pipeline_name, version, content, meta_dict)

        result = {
            "success": True,
            "name": pipeline_name,
            "version": version,
            "agents_count": agents_count,
            "location": str(ver_dir),
        }

        # GitHub publish
        if not req.local_only:
            pub_result = publish_to_github(
                agents_yaml_path=source_path,
                pipeline_name=pipeline_name,
                description=req.description or "",
                version=version,
                repo=DEFAULT_REGISTRY_REPO,
            )
            if pub_result.success:
                result["pr_url"] = pub_result.pr_url
            else:
                result["github_error"] = pub_result.error

        return result

    @router.get("/api/registry/{name}/versions")
    async def api_list_versions(
        name: str,
        offline: bool = Query(False),
    ):
        from aqm.registry import (
            DEFAULT_REGISTRY_REPO,
            list_versions as _list_versions,
            version_sort_key,
        )

        if offline:
            from aqm.registry import list_local_versions
            return {"name": name, "versions": list_local_versions(name), "github": [], "local": list_local_versions(name)}

        result = _list_versions(name, repo=DEFAULT_REGISTRY_REPO)
        return {
            "name": name,
            "github": result.get("github", []),
            "local": result.get("local", []),
            "versions": sorted(
                set(result.get("github", []) + result.get("local", [])),
                key=version_sort_key,
            ),
        }

    return router
