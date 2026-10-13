"""GitHub Activity collector — repo signals for tech event predictions."""

import logging
from datetime import datetime, timedelta

import httpx

from src.collectors.base import BaseCollector
from src.config import settings
from src.models.evidence import EvidenceItem
from src.models.question import Question

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

REPO_MAP: dict[str, list[tuple[str, str]]] = {
    "gpt": [("openai/openai-python", "OpenAI Python SDK")],
    "openai": [("openai/openai-python", "OpenAI Python SDK")],
    "claude": [("anthropics/anthropic-sdk-python", "Anthropic Python SDK")],
    "anthropic": [("anthropics/anthropic-sdk-python", "Anthropic Python SDK")],
    "gemini": [("google-gemini/generative-ai-python", "Google GenAI SDK")],
    "llama": [("meta-llama/llama", "Meta LLaMA")],
    "mistral": [("mistralai/mistral-inference", "Mistral Inference")],
    "huggingface": [("huggingface/transformers", "HuggingFace Transformers")],
    "transformers": [("huggingface/transformers", "HuggingFace Transformers")],
    "langchain": [("langchain-ai/langchain", "LangChain")],
    "pytorch": [("pytorch/pytorch", "PyTorch")],
    "tensorflow": [("tensorflow/tensorflow", "TensorFlow")],
    "apple": [("apple/ml-stable-diffusion", "Apple ML")],
}


class GitHubActivityCollector(BaseCollector):
    """Collects evidence from GitHub repository activity."""

    source_type = "github"

    def is_available(self) -> bool:
        # Works without token (60 req/hr), better with token (5000 req/hr)
        return True

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        return headers

    def _find_repos(self, question: Question) -> list[tuple[str, str]]:
        """Match question keywords to known GitHub repos."""
        repos: list[tuple[str, str]] = []
        seen = set()

        for keyword in question.search_keywords:
            kw_lower = keyword.lower()
            for fragment, repo_list in REPO_MAP.items():
                if fragment in kw_lower:
                    for repo in repo_list:
                        if repo[0] not in seen:
                            repos.append(repo)
                            seen.add(repo[0])

        return repos

    async def collect(self, question: Question, max_results: int = 20) -> list[EvidenceItem]:
        repos = self._find_repos(question)
        if not repos:
            logger.info("No matching GitHub repos for this question.")
            return []

        items: list[EvidenceItem] = []
        headers = self._headers()

        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            for repo_full, repo_desc in repos[:3]:  # Limit repos to avoid rate limits
                try:
                    # Fetch recent releases
                    releases = await self._get_releases(client, repo_full)
                    items.extend(releases)

                    # Fetch recent commit activity
                    commits = await self._get_recent_commits(client, repo_full)
                    items.extend(commits)
                except Exception as e:
                    logger.warning(f"GitHub collector failed for {repo_full}: {e}")

        items.sort(key=lambda x: (x.published_at or datetime.min).timestamp(), reverse=True)
        return items[:max_results]

    async def _get_releases(
        self, client: httpx.AsyncClient, repo: str
    ) -> list[EvidenceItem]:
        """Fetch recent releases from a repo."""
        resp = await client.get(f"{GITHUB_API}/repos/{repo}/releases", params={"per_page": 5})

        if resp.status_code != 200:
            return []

        items: list[EvidenceItem] = []
        for release in resp.json():
            published_at = None
            if release.get("published_at"):
                try:
                    published_at = datetime.fromisoformat(
                        release["published_at"].replace("Z", "+00:00")
                    )
                except Exception:
                    pass

            tag = release.get("tag_name", "")
            name = release.get("name", "") or tag
            body = release.get("body", "") or ""

            items.append(
                EvidenceItem(
                    source_type=self.source_type,
                    source_name=f"GitHub:{repo}",
                    title=f"Release {name} ({tag})",
                    url=release.get("html_url", ""),
                    content_snippet=body[:500],
                    published_at=published_at,
                )
            )

        return items

    async def _get_recent_commits(
        self, client: httpx.AsyncClient, repo: str
    ) -> list[EvidenceItem]:
        """Fetch recent commit activity as a signal."""
        since = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")

        resp = await client.get(
            f"{GITHUB_API}/repos/{repo}/commits",
            params={"since": since, "per_page": 10},
        )

        if resp.status_code != 200:
            return []

        commits = resp.json()
        if not commits:
            return []

        # Summarize commit activity rather than listing every commit
        commit_count = len(commits)
        latest = commits[0]
        latest_msg = latest.get("commit", {}).get("message", "").split("\n")[0]
        latest_date_str = latest.get("commit", {}).get("committer", {}).get("date", "")

        published_at = None
        if latest_date_str:
            try:
                published_at = datetime.fromisoformat(latest_date_str.replace("Z", "+00:00"))
            except Exception:
                pass

        return [
            EvidenceItem(
                source_type=self.source_type,
                source_name=f"GitHub:{repo}",
                title=f"{commit_count} commits in last 14d (latest: {latest_msg[:80]})",
                url=latest.get("html_url", ""),
                content_snippet=f"Active development: {commit_count} commits in the last 2 weeks. "
                f"Latest commit: {latest_msg[:200]}",
                published_at=published_at,
            )
        ]
