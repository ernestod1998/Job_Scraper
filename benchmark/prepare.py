"""Prepare private reviewable inputs, without model calls or truncated descriptions."""
import hashlib
import json
import re
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .core import RESUMES, VERSION, digest, validate_result
from .providers import APIError, NoRedirect, http_json
from .storage import private_dir, write_json

PATTERNS = {
    "BioScience_ML": r"bioinform|computational|cheminform|(?:machine learning|AI).*scientist|scientist.*(?:ML|AI)",
    "ML": r"machine learning|\bML\b|AI engineer|research engineer",
    "DS": r"data scien",
    "SWE": r"software engineer|full.stack|backend engineer",
    "FDE": r"forward.deployed|deployment strategist|solutions engineer|customer engineer",
}


def evidence(text, prefix):
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    return {f"{prefix}:{i:04d}": line for i, line in enumerate(lines, 1)}


def text_from_html(markup):
    soup = BeautifulSoup(markup, "html.parser")
    for el in soup(["script", "style", "noscript", "nav", "footer"]):
        el.decompose()
    return soup.get_text("\n", strip=True)


def public_html(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            return response.read(2_000_000).decode("utf-8")
    except (urllib.error.URLError, OSError, ValueError):
        raise ValueError("public_description_unavailable") from None


def jobposting_description(markup):
    """JSON-LD fallback for an ATS page; a JS shell is never a description."""
    def walk(value):
        if isinstance(value, list):
            for child in value:
                yield from walk(child)
        elif isinstance(value, dict):
            kind = value.get("@type")
            if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
                if isinstance(value.get("description"), str):
                    yield text_from_html(value["description"])
            if "@graph" in value:
                yield from walk(value["@graph"])
    for script in BeautifulSoup(markup, "html.parser").find_all("script", type="application/ld+json"):
        try:
            values = list(walk(json.loads(script.string or script.get_text())))
            if values:
                return values[0]
        except ValueError:
            continue
    raise ValueError("structured_job_description_missing")


def prepare_resumes(directory, output):
    directory, output = Path(directory), private_dir(output)
    result = {}
    for name in RESUMES:
        # BioScience_ML also ends in _ML; assign each filename to its longest
        # matching variant so the general ML resume remains unambiguous.
        matches = sorted(p for p in directory.glob(f"*_{name}.pdf") if max(
            (r for r in RESUMES if p.stem.endswith("_" + r)), key=len) == name)
        if len(matches) != 1:
            raise ValueError("expected_one_pdf_per_resume_variant")
        path = matches[0]
        proc = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, timeout=30)
        if proc.returncode:
            raise ValueError("pdf_text_extraction_failed")
        text = proc.stdout.decode("utf-8")
        if len(text.strip()) < 500:
            raise ValueError("pdf_has_insufficient_text")
        pages = private_dir(output / "pages")
        rendered = subprocess.run(["pdftoppm", "-scale-to", "1500", "-png", str(path), str(pages / name)],
                                  capture_output=True, timeout=60)
        if rendered.returncode:
            raise ValueError("pdf_render_failed")
        for image in pages.glob(f"{name}-*.png"):
            image.chmod(0o600)
        result[name] = {"source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "evidence": evidence(text, name), "visually_verified": False}
    write_json(output / "resumes.json", result)
    return result


def job_key(job):
    # Cross-source repost suppression is conservative; location-specific duplicates
    # are reviewed manually before the frozen selection is accepted.
    return tuple(re.sub(r"\W+", " ", str(job.get(k, "")).lower()).strip()
                 for k in ("company", "title"))


def infer_tracks(job):
    return [track for track, pattern in PATTERNS.items() if re.search(pattern, job["title"], re.I)]


def fetch_description(job):
    """Fetch full ATS JSON. No generic HTML shell is treated as a complete JD."""
    url = urlparse(job["url"])
    parts = url.path.strip("/").split("/")
    host = (url.hostname or "").lower()
    if host in {"www.linkedin.com", "linkedin.com"}:
        match = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)(?:/|$)", url.path)
        if not match:
            raise ValueError("unsupported_posting_url")
        request = urllib.request.Request(
            "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/" + match.group(1),
            headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
                markup = response.read(2_000_000).decode("utf-8")
        except (urllib.error.URLError, OSError, ValueError):
            raise ValueError("linkedin_description_unavailable") from None
        node = BeautifulSoup(markup, "html.parser").select_one(".show-more-less-html__markup")
        if node is None:
            raise ValueError("linkedin_description_missing")
        return text_from_html(str(node)), "linkedin_guest"
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"} and len(parts) >= 3:
        board, jobid = parts[0], parts[-1]
        if not re.fullmatch(r"[\w-]+", board) or not jobid.isdigit():
            raise ValueError("unsupported_posting_url")
        data = http_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jobid}", {}, timeout=20)
        # Greenhouse content may be entity-escaped HTML twice.
        import html
        return text_from_html(html.unescape(data.get("content", ""))), "greenhouse_api"
    if host == "jobs.ashbyhq.com" and len(parts) >= 2:
        board, jobid = parts[:2]
        if not re.fullmatch(r"[\w.-]+", board):
            raise ValueError("unsupported_posting_url")
        try:
            data = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}", {}, timeout=20)
        except APIError:
            return jobposting_description(public_html(job["url"])), "ashby_jobposting_jsonld"
        for posting in data.get("jobs", []):
            if posting.get("id") == jobid or posting.get("jobUrl", "").rstrip("/") == job["url"].rstrip("/"):
                return (posting.get("descriptionPlain") or text_from_html(posting.get("descriptionHtml", ""))), "ashby_api"
        return jobposting_description(public_html(job["url"])), "ashby_jobposting_jsonld"
    if host == "jobs.lever.co" and len(parts) >= 2:
        board, jobid = parts[:2]
        if not all(re.fullmatch(r"[\w-]+", x) for x in (board, jobid)):
            raise ValueError("unsupported_posting_url")
        data = http_json(f"https://api.lever.co/v0/postings/{board}/{jobid}", {}, timeout=20)
        sections = [data.get("descriptionPlain", "")]
        sections += [item.get("text", "") + "\n" + text_from_html(item.get("content", ""))
                     for item in data.get("lists", [])]
        sections += [data.get("additionalPlain", "")]
        return "\n".join(sections), "lever_api"
    raise ValueError("saved_description_or_supported_ats_required")


def collect_candidates(repo, output, per_track=12):
    repo, output = Path(repo), private_dir(output)
    master = json.loads((repo / "all_jobs.json").read_text())["jobs"]
    saved = {}
    for filename in ("indeed_jobs.json", "boards_jobs.json"):
        path = repo / filename
        if path.exists():
            for j in json.loads(path.read_text()).get("jobs", []):
                if j.get("description"):
                    saved[j["url"]] = j["description"]
    counts = dict.fromkeys(RESUMES, 0)
    selected, seen = [], set()
    # Prefer sources with structured descriptions; no old ranking scores are read.
    candidates = sorted(master, key=lambda j: (j.get("ats") not in ("Greenhouse", "Ashby", "Lever"),
                                               str(j.get("first_seen", "")), j.get("url", "")))
    for job in candidates:
        tracks = infer_tracks(job)
        if not tracks or job_key(job) in seen:
            continue
        host = urlparse(job["url"]).hostname
        supported = host in ("boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.ashbyhq.com", "jobs.lever.co", "www.linkedin.com", "linkedin.com")
        if not supported and job["url"] not in saved:
            continue
        if not any(counts[t] < per_track for t in tracks):
            continue
        seen.add(job_key(job))
        for t in tracks:
            counts[t] += 1
        selected.append(job)

    def fetch(job):
        safe = {k: job.get(k, "") for k in ("title", "company", "url", "location", "ats", "date_posted")}
        safe.update(id=digest(job["url"])[:16], tracks=infer_tracks(job))
        try:
            text, source = ((text_from_html(saved[job["url"]]), "saved_feed") if job["url"] in saved
                            else fetch_description(job))
            if len(text.strip()) < 500:
                raise ValueError("description_too_short")
            safe.update(evidence=evidence(text, "JD"), description_sha256=digest(text),
                        description_source=source, complete_reviewed=False)
        except APIError as e:
            safe["error"] = str(e)
        except ValueError as e:
            safe["error"] = str(e)
        return safe

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(fetch, selected))
    payload = {"created_at": datetime.now(timezone.utc).isoformat(), "candidates": results,
               "note": "Unscored candidates; source text and track/difficulty selection require review."}
    write_json(output / "candidates.json", payload)
    return payload


def freeze(workspace):
    """Freeze an explicitly reviewed selection; never silently sample or invent labels."""
    workspace = Path(workspace)
    resumes = json.loads((workspace / "resumes.json").read_text())
    selection = json.loads((workspace / "selection.json").read_text())
    references = json.loads((workspace / "references.json").read_text())
    shared = json.loads((workspace / "shared-facts.json").read_text())
    if set(resumes) != set(RESUMES) or not all(r.get("visually_verified") is True for r in resumes.values()):
        raise ValueError("resume_visual_review_required")
    if len(selection) != 25 or len({j["id"] for j in selection}) != 25 or len({job_key(j) for j in selection}) != 25:
        raise ValueError("exactly_25_distinct_jobs_required")
    if set(references) != {j["id"] for j in selection}:
        raise ValueError("reference_coverage_required")
    for track in RESUMES:
        group = [j for j in selection if j["track"] == track]
        if sorted(j["fit_bucket"] for j in group) != ["gap", "partial", "partial", "strong", "strong"]:
            raise ValueError("five_jobs_per_track_with_balanced_fit_required")
    for job in selection:
        if job.get("complete_reviewed") is not True:
            raise ValueError("job_description_review_required")
        ref = references[job["id"]]
        if ref.get("reviewed") is not True:
            raise ValueError("reference_review_required")
        validate_result(ref["result"], job, resumes, shared)
        if not ref["result"]["requirements"]:
            raise ValueError("reference_requires_requirements")
    manifest = {"version": VERSION, "jobs": selection, "resumes": resumes,
                "shared_facts": shared, "references": references,
                "reference_kind": "Codex-reviewed; not human ground truth"}
    manifest["fingerprint"] = digest(manifest)
    path = workspace / "manifest.json"
    if path.exists() and json.loads(path.read_text()) != manifest:
        raise ValueError("frozen_manifest_exists_use_new_workspace")
    write_json(path, manifest)
    return manifest
