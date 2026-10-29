#!/usr/bin/env python3
"""Merge Shadowrocket ad-block lists and rebuild fork configs.

Sources:
  - blackmatrix7 Advertising_Domain.list + Advertising.list
  - anti-AD anti-ad-surge2.txt (DOMAIN-SET)
  - custom/ad-allow.list and custom/ad-block.list

The upstream johnshall ad list is not merged. sr_top500_banlist_ad.conf
and sr_top500_whitelist_ad.conf on johnshall's release branch are only
skeletons: their proxy/direct rules are kept, and their inline Reject
section is replaced with DOMAIN-SET / RULE-SET references.

Outputs:
  - sr_top500_banlist_ad.conf    blacklist proxy rules + DOMAIN-SET/RULE-SET ads
  - sr_top500_whitelist_ad.conf  whitelist direct/proxy rules + DOMAIN-SET/RULE-SET ads
  - dist/ad-domain.list          DOMAIN-SET referenced by the configs
  - dist/ad-rules.list           RULE-SET (IP-CIDR / URL-REGEX / user keywords)
  - dist/ad-allow.list           DIRECT exceptions, referenced only when needed
  - dist/stats.json
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache"
DIST = ROOT / "dist"

OWNER_REPO = "hi-vic-vic/Shadowrocket-ADBlock-Rules-Forever"
BRANCH = "release"
RAW_BASE = os.environ.get(
    "RULE_RAW_BASE",
    f"https://raw.githubusercontent.com/{OWNER_REPO}/{BRANCH}",
)

UPSTREAM_REPO = "johnshall/Shadowrocket-ADBlock-Rules-Forever"
UPSTREAM_BRANCH = "release"
BM7_REPO = "blackmatrix7/ios_rule_script"
BM7_BRANCH = "master"
ANTI_REPO = "privacy-protection-tools/anti-AD"
ANTI_BRANCH = "master"

AD_CONFIGS = (
    "sr_top500_banlist_ad.conf",
    "sr_top500_whitelist_ad.conf",
)

SRC_LOCAL = "local"
SRC_BM7 = "blackmatrix7"
SRC_ANTI = "anti_ad"
SRC_EXTRA = "extra_block"

# Suffixes that would block an entire public registry. Never emit these.
PUBLIC_SUFFIXES = {
    "com.cn",
    "net.cn",
    "org.cn",
    "gov.cn",
    "edu.cn",
    "ac.cn",
    "co.uk",
    "org.uk",
    "me.uk",
    "ac.uk",
    "com.hk",
    "com.tw",
    "com.sg",
    "com.au",
    "co.jp",
    "co.kr",
    "com.br",
    "com.mx",
    "com.tr",
    "com.ua",
    "com.vn",
    "com.my",
    "co.id",
    "co.th",
    "com.ar",
    "co.za",
    "com.co",
    "net.au",
    "org.au",
    "co.nz",
    "com.ph",
    "ne.jp",
    "or.jp",
    "go.jp",
}

LABEL_RE = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")
REJECT_RE = re.compile(
    r"^(?:DOMAIN-SUFFIX|DOMAIN|DOMAIN-KEYWORD|IP-CIDR6?|IP6-CIDR|USER-AGENT|URL-REGEX|PROCESS-NAME)\s*,.+\s*,\s*(?:Reject|REJECT)(?:\s*,\s*no-resolve)?\s*$",
    re.IGNORECASE,
)
BEGIN_MARK = "# BEGIN merged adblock"
END_MARK = "# END merged adblock"
POLICY_TAIL_RE = re.compile(
    r",(?:Reject|REJECT|Direct|DIRECT|Proxy|PROXY|no-resolve)\s*$",
    re.IGNORECASE,
)
HOST_ANCHOR_RE = re.compile(r"[a-z0-9][a-z0-9-]*\.[a-z]{2,}", re.IGNORECASE)


class Node:
    __slots__ = ("ch", "end", "src")

    def __init__(self) -> None:
        self.ch: dict[str, Node] = {}
        self.end = False
        self.src = ""


def http_get(url: str) -> str:
    last_error: Exception | None = None
    for _ in range(3):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "shadowrocket-adblock-merge"},
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = resp.read()
            if len(data) < 64:
                raise RuntimeError(f"response too small ({len(data)} bytes)")
            return data.decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, RuntimeError, OSError) as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def run_git(args: list[str], cwd: Path | None = None) -> None:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    subprocess.run(args, cwd=cwd, check=True, env=env)


def clone_full(url: str, branch: str, dest: Path) -> None:
    if (dest / ".git").exists():
        run_git(["git", "fetch", "--depth", "1", "origin", branch], cwd=dest)
        run_git(["git", "-c", "advice.detachedHead=false", "checkout", "-q", "-f", "FETCH_HEAD"], cwd=dest)
        return
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_git(
        ["git", "clone", "--depth", "1", "--branch", branch, url, str(dest)]
    )


def clone_sparse(url: str, branch: str, dest: Path, directory: str) -> None:
    if not (dest / ".git").exists():
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        run_git(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--filter=blob:none",
                "--sparse",
                "--branch",
                branch,
                url,
                str(dest),
            ]
        )
    else:
        run_git(["git", "fetch", "--depth", "1", "origin", branch], cwd=dest)
        run_git(["git", "-c", "advice.detachedHead=false", "checkout", "-q", "-f", "FETCH_HEAD"], cwd=dest)
    run_git(["git", "sparse-checkout", "set", directory], cwd=dest)


def raw_url(repo: str, branch: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"


def load_remote(cache_name: str, url: str, git_loader) -> tuple[str, str]:
    """Return (text, how). how is download, git, or cache."""
    cache_path = CACHE / "sources" / cache_name
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = http_get(url)
        cache_path.write_text(text, encoding="utf-8")
        return text, "download"
    except Exception as exc:  # noqa: BLE001 - fallback is the point
        print(f"  raw 下载失败 {cache_name}: {exc}", file=sys.stderr)
    try:
        text = git_loader()
        cache_path.write_text(text, encoding="utf-8")
        return text, "git"
    except Exception as exc:  # noqa: BLE001
        print(f"  git 下载失败 {cache_name}: {exc}", file=sys.stderr)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path.read_text(encoding="utf-8", errors="replace"), "cache"
    local = ROOT / cache_name
    if local.exists():
        text = local.read_text(encoding="utf-8", errors="replace")
        # Only an upstream ad config still full of inline Reject rules is a valid skeleton.
        if text.count(",Reject") > 1000:
            return text, "workspace"
    raise SystemExit(f"无法获取 {cache_name} ({url})")


def fetch_inputs() -> dict[str, tuple[str, str]]:
    repos = CACHE / "repos"
    bm7_dir = repos / "ios_rule_script"
    anti_dir = repos / "anti-AD"
    up_dir = repos / "upstream"

    def bm7_file(name: str) -> str:
        clone_sparse(
            f"https://github.com/{BM7_REPO}.git",
            BM7_BRANCH,
            bm7_dir,
            "rule/Shadowrocket/Advertising",
        )
        path = bm7_dir / "rule/Shadowrocket/Advertising" / name
        return path.read_text(encoding="utf-8", errors="replace")

    def anti_file() -> str:
        clone_full(f"https://github.com/{ANTI_REPO}.git", ANTI_BRANCH, anti_dir)
        return (anti_dir / "anti-ad-surge2.txt").read_text(encoding="utf-8", errors="replace")

    def up_file(name: str) -> str:
        clone_full(
            f"https://github.com/{UPSTREAM_REPO}.git",
            UPSTREAM_BRANCH,
            up_dir,
        )
        return (up_dir / name).read_text(encoding="utf-8", errors="replace")

    wanted: list[tuple[str, str, object]] = [
        (
            "Advertising_Domain.list",
            raw_url(BM7_REPO, BM7_BRANCH, "rule/Shadowrocket/Advertising/Advertising_Domain.list"),
            lambda: bm7_file("Advertising_Domain.list"),
        ),
        (
            "Advertising.list",
            raw_url(BM7_REPO, BM7_BRANCH, "rule/Shadowrocket/Advertising/Advertising.list"),
            lambda: bm7_file("Advertising.list"),
        ),
        (
            "anti-ad-surge2.txt",
            raw_url(ANTI_REPO, ANTI_BRANCH, "anti-ad-surge2.txt"),
            anti_file,
        ),
    ]
    for name in AD_CONFIGS:
        wanted.append((name, raw_url(UPSTREAM_REPO, UPSTREAM_BRANCH, name), lambda n=name: up_file(n)))

    loaded: dict[str, tuple[str, str]] = {}
    for name, url, loader in wanted:
        print(f"获取 {name}")
        loaded[name] = load_remote(name, url, loader)
        print(f"  来源: {loaded[name][1]}  字符: {len(loaded[name][0])}")
    return loaded


def normalize_domain(raw: str) -> str | None:
    domain = raw.strip().lower().rstrip(".")
    if domain.startswith("*."):
        domain = domain[2:]
    if domain.startswith("."):
        domain = domain[1:]
    if ":" in domain:
        host, _, port = domain.rpartition(":")
        if port.isdigit():
            domain = host
    if not domain or any(ch in domain for ch in " /@*?#[]()\\"):
        return None
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    labels = domain.split(".")
    if len(labels) < 2 or len(domain) > 253:
        return None
    for label in labels:
        if not label or len(label) > 63 or LABEL_RE.fullmatch(label) is None:
            return None
    return domain


def is_unsafe_suffix(domain: str) -> bool:
    return domain in PUBLIC_SUFFIXES or "." not in domain


def strip_policy(payload: str) -> str:
    current = payload.strip()
    while True:
        updated = POLICY_TAIL_RE.sub("", current).strip()
        if updated == current:
            return current
        current = updated


def parse_line(line: str) -> tuple | None:
    text = line.strip()
    if not text or text.startswith("#") or text.startswith("!") or text.startswith("//"):
        return None
    if " #" in text:
        text = text.split(" #", 1)[0].strip()
    if text.startswith("||") and text.endswith("^"):
        return ("suffix", text[2:-1])
    if text.startswith("."):
        return ("suffix", text[1:])
    if "," not in text:
        if " " in text or "/" in text:
            return None
        return ("exact", text)

    kind, payload = text.split(",", 1)
    kind = kind.strip().upper()
    if kind == "DOMAIN-SUFFIX":
        return ("suffix", strip_policy(payload).split(",", 1)[0].strip())
    if kind == "DOMAIN":
        return ("exact", strip_policy(payload).split(",", 1)[0].strip())
    if kind == "DOMAIN-KEYWORD":
        return ("keyword", strip_policy(payload).split(",", 1)[0].strip())
    if kind in {"IP-CIDR", "IP-CIDR6", "IP6-CIDR"}:
        body = strip_policy(payload)
        cidr = body.split(",", 1)[0].strip()
        no_resolve = "no-resolve" in payload.lower()
        return ("ip", cidr, no_resolve)
    if kind == "URL-REGEX":
        return ("regex", strip_policy(payload))
    return None


def empty_bucket() -> dict:
    return {
        "suffix": [],
        "exact": [],
        "ip": [],  # (cidr, no_resolve)
        "regex": [],
        "keyword": [],
        "invalid": 0,
        "invalid_samples": [],
        "unsafe_suffix": 0,
        "unsafe_samples": [],
    }


def add_invalid(bucket: dict, sample: str) -> None:
    bucket["invalid"] += 1
    if len(bucket["invalid_samples"]) < 8:
        bucket["invalid_samples"].append(sample[:160])


def ingest(text: str, mode: str) -> dict:
    """mode: domainset, rules, or adconf (Reject lines only)."""
    bucket = empty_bucket()
    for line in text.splitlines():
        if mode == "adconf":
            stripped = line.strip()
            if not REJECT_RE.match(stripped):
                continue
        parsed = parse_line(line)
        if parsed is None:
            continue
        kind = parsed[0]
        if kind in {"suffix", "exact"}:
            domain = normalize_domain(parsed[1])
            if domain is None:
                add_invalid(bucket, parsed[1])
                continue
            if kind == "suffix" and is_unsafe_suffix(domain):
                bucket["unsafe_suffix"] += 1
                if len(bucket["unsafe_samples"]) < 8:
                    bucket["unsafe_samples"].append(domain)
                continue
            bucket[kind].append(domain)
        elif kind == "ip":
            bucket["ip"].append((parsed[1], parsed[2]))
        elif kind == "regex":
            bucket["regex"].append(parsed[1])
        elif kind == "keyword":
            bucket["keyword"].append(parsed[1])
    return bucket


class SuffixTrie:
    def __init__(self) -> None:
        self.root = Node()
        self.duplicate = 0
        self.covered = 0

    def add(self, domain: str, src: str) -> str:
        labels = domain.split(".")
        labels.reverse()
        node = self.root
        for label in labels:
            if node.end:
                self.covered += 1
                return "covered"
            node = node.ch.setdefault(label, Node())
        if node.end:
            self.duplicate += 1
            return "dup"
        pruned = count_terminals(node)
        if node.ch:
            node.ch.clear()
        node.end = True
        node.src = src
        if pruned:
            self.covered += pruned
            return "pruned"
        return "added"

    def covers(self, domain: str) -> bool:
        labels = domain.split(".")
        labels.reverse()
        node = self.root
        for label in labels:
            if node.end:
                return True
            node = node.ch.get(label)
            if node is None:
                return False
        return node.end

    def remove_subtree(self, domain: str) -> int:
        labels = domain.split(".")
        labels.reverse()
        node = self.root
        for label in labels:
            node = node.ch.get(label)
            if node is None:
                return 0
        removed = count_terminals(node)
        node.end = False
        node.src = ""
        node.ch.clear()
        return removed

    def collect(self) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []

        def walk(node: Node, labels: list[str]) -> None:
            if node.end:
                found.append((".".join(reversed(labels)), node.src))
            for label, child in node.ch.items():
                labels.append(label)
                walk(child, labels)
                labels.pop()

        walk(self.root, [])
        return found


def count_terminals(node: Node) -> int:
    total = 1 if node.end else 0
    for child in node.ch.values():
        total += count_terminals(child)
    return total


def regex_worth_keeping(pattern: str) -> bool:
    """Keep host-anchored ad URL patterns. Drop bare path fragments."""
    flattened = pattern.replace("\\.", ".")
    return HOST_ANCHOR_RE.search(flattened) is not None


def merge_ips(entries: list[tuple[str, bool, str]]) -> tuple[list[dict], dict]:
    parsed: list[dict] = []
    dropped: list[str] = []
    invalid = 0
    for cidr, no_resolve, src in entries:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            invalid += 1
            continue
        if not net.network_address.is_global:
            dropped.append(str(net))
            continue
        if net.version == 4 and net.prefixlen < 16:
            dropped.append(str(net))
            continue
        if net.version == 6 and net.prefixlen < 32:
            dropped.append(str(net))
            continue
        parsed.append({"net": net, "no_resolve": no_resolve, "src": src})

    parsed.sort(key=lambda item: (item["net"].version, item["net"].prefixlen, int(item["net"].network_address)))
    kept: list[dict] = []
    duplicate_or_covered = 0
    for item in parsed:
        match = None
        for existing in kept:
            if item["net"].version == existing["net"].version and item["net"].subnet_of(existing["net"]):
                match = existing
                break
        if match is not None:
            duplicate_or_covered += 1
            if match["net"] == item["net"] and not item["no_resolve"]:
                match["no_resolve"] = False
            continue
        kept.append(item)
    kept.sort(key=lambda item: (item["net"].version, int(item["net"].network_address), item["net"].prefixlen))
    return kept, {
        "invalid": invalid,
        "dropped_non_global_or_too_broad": sorted(set(dropped)),
        "duplicate_or_covered": duplicate_or_covered,
        "kept": len(kept),
    }


def is_ad_preamble(line: str) -> bool:
    text = line.strip()
    if text == "":
        return True
    if not text.startswith("#"):
        return False
    body = text.lstrip("#").strip()
    if body == "":
        return False
    protected = ("黑名单", "白名单", "直连", "代理", "国内外", "GFW", "局域网", "包含广告", "不包含广告")
    return not any(word in body for word in protected)


def is_reject_line(line: str) -> bool:
    return REJECT_RE.match(line.strip()) is not None


def split_ad_region(lines: list[str]) -> tuple[list[str], list[str]]:
    begin = next((i for i, line in enumerate(lines) if line.strip() == BEGIN_MARK), None)
    if begin is not None:
        end = next((i for i, line in enumerate(lines) if i > begin and line.strip() == END_MARK), None)
        if end is None:
            raise SystemExit("找到合并广告段开头，但没有结束标记")
        return lines[:begin], lines[end + 1 :]

    indexes = [i for i, line in enumerate(lines) if is_reject_line(line)]
    if not indexes:
        raise SystemExit("广告配置里没有 Reject 规则，拒绝用已经瘦身过的文件当上游骨架")
    start, end = indexes[0], indexes[-1]
    for index in range(start, end + 1):
        text = lines[index].strip()
        if not text or text.startswith("#") or is_reject_line(text):
            continue
        raise SystemExit(f"广告段里夹着非拒绝规则，停止生成: {text[:180]}")
    region_start = start
    while region_start > 0 and is_ad_preamble(lines[region_start - 1]):
        region_start -= 1
    region_end = end + 1
    while region_end < len(lines) and lines[region_end].strip() == "":
        region_end += 1
    return lines[:region_start], lines[region_end:]


def render_reference_block(include_allow: bool) -> list[str]:
    lines = [
        "",
        BEGIN_MARK,
        "# 合并去广告：blackmatrix7 Advertising + anti-AD",
        "# 域名和 IP 放在 dist/ 下，用 DOMAIN-SET / RULE-SET 引用，避免把几十万行载入配置。",
    ]
    if include_allow:
        lines.append(f"RULE-SET,{RAW_BASE}/dist/ad-allow.list,DIRECT")
    lines.append(f"DOMAIN-SET,{RAW_BASE}/dist/ad-domain.list,REJECT")
    lines.append(f"RULE-SET,{RAW_BASE}/dist/ad-rules.list,REJECT")
    lines.append(END_MARK)
    lines.append("")
    return lines


def rebuild_config(text: str, ad_lines: list[str]) -> str:
    head, tail = split_ad_region(text.splitlines())
    merged = head + ad_lines + tail
    return "\n".join(merged).rstrip("\n") + "\n"


def significant_rules(lines: list[str]) -> list[str]:
    kept: list[str] = []
    skipping = False
    for line in lines:
        text = line.strip()
        if text == BEGIN_MARK:
            skipping = True
            continue
        if text == END_MARK:
            skipping = False
            continue
        if skipping or not text or text.startswith("#") or text.startswith("["):
            continue
        if is_reject_line(text):
            continue
        if text.startswith("DOMAIN-SET,") or (
            text.startswith("RULE-SET,") and "/dist/ad-" in text
        ):
            continue
        kept.append(text)
    return kept


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def domain_under(domain: str, suffix: str) -> bool:
    return domain == suffix or domain.endswith("." + suffix)


def apply_allow(trie: SuffixTrie, exact: dict[str, str], allow: dict) -> list[tuple[str, str]]:
    exceptions: list[tuple[str, str]] = []
    removed_suffix = 0
    removed_exact = 0
    for suffix in dict.fromkeys(allow["suffix"]):
        removed_suffix += trie.remove_subtree(suffix)
        for domain in list(exact):
            if domain_under(domain, suffix):
                del exact[domain]
                removed_exact += 1
        if trie.covers(suffix):
            exceptions.append(("DOMAIN-SUFFIX", suffix))
    allow_suffixes = set(allow["suffix"])
    for domain in dict.fromkeys(allow["exact"]):
        if any(domain_under(domain, suffix) for suffix in allow_suffixes):
            continue
        if domain in exact:
            del exact[domain]
            removed_exact += 1
        if trie.covers(domain):
            exceptions.append(("DOMAIN", domain))
    # Drop exact exceptions already covered by a suffix exception.
    suffix_exceptions = {domain for kind, domain in exceptions if kind == "DOMAIN-SUFFIX"}
    filtered = []
    for kind, domain in exceptions:
        if kind == "DOMAIN" and any(domain_under(domain, suffix) for suffix in suffix_exceptions):
            continue
        filtered.append((kind, domain))
    filtered.sort()
    allow["removed_suffix_terminals"] = removed_suffix
    allow["removed_exact"] = removed_exact
    allow["exceptions"] = len(filtered)
    return filtered


def build(loaded: dict[str, tuple[str, str]]) -> dict:
    bm7_domains = ingest(loaded["Advertising_Domain.list"][0], "domainset")
    bm7_rules = ingest(loaded["Advertising.list"][0], "rules")
    anti = ingest(loaded["anti-ad-surge2.txt"][0], "domainset")
    extra = ingest((ROOT / "custom" / "ad-block.list").read_text(encoding="utf-8"), "rules")
    allow = ingest((ROOT / "custom" / "ad-allow.list").read_text(encoding="utf-8"), "rules")

    trie = SuffixTrie()
    exact: dict[str, str] = {}

    def feed_suffixes(domains: list[str], src: str) -> None:
        for domain in domains:
            trie.add(domain, src)

    feed_suffixes(bm7_domains["suffix"], SRC_BM7)
    feed_suffixes(bm7_rules["suffix"], SRC_BM7)
    feed_suffixes(anti["suffix"], SRC_ANTI)
    feed_suffixes(extra["suffix"], SRC_EXTRA)

    exact_covered = 0
    exact_dup = 0

    def feed_exact_counted(domains: list[str], src: str) -> None:
        nonlocal exact_covered, exact_dup
        for domain in domains:
            if trie.covers(domain):
                exact_covered += 1
                continue
            if domain in exact:
                exact_dup += 1
                continue
            exact[domain] = src

    feed_exact_counted(bm7_domains["exact"], SRC_BM7)
    feed_exact_counted(bm7_rules["exact"], SRC_BM7)
    feed_exact_counted(anti["exact"], SRC_ANTI)
    feed_exact_counted(extra["exact"], SRC_EXTRA)

    exceptions = apply_allow(trie, exact, allow)

    suffixes = trie.collect()
    suffix_set = {domain for domain, _src in suffixes}
    for domain in list(exact):
        labels = domain.split(".")
        if any(".".join(labels[i:]) in suffix_set for i in range(len(labels))):
            del exact[domain]
            exact_covered += 1

    # Final parent check. Fail the build instead of shipping a redundant list.
    for domain in suffix_set:
        labels = domain.split(".")
        for index in range(1, len(labels)):
            parent = ".".join(labels[index:])
            if parent in suffix_set:
                raise SystemExit(f"父后缀去重失败: {parent} 仍覆盖 {domain}")

    ip_entries: list[tuple[str, bool, str]] = []
    ip_entries.extend((cidr, flag, SRC_BM7) for cidr, flag in bm7_rules["ip"])
    ip_entries.extend((cidr, flag, SRC_EXTRA) for cidr, flag in extra["ip"])
    kept_ips, ip_stats = merge_ips(ip_entries)

    allow_ips = []
    for cidr, _flag in allow["ip"]:
        try:
            allow_ips.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            ip_stats["invalid"] += 1
    if allow_ips:
        before = len(kept_ips)
        kept_ips = [
            item
            for item in kept_ips
            if not any(item["net"].version == allow_net.version and item["net"].subnet_of(allow_net) for allow_net in allow_ips)
        ]
        ip_stats["removed_by_allow"] = before - len(kept_ips)
        ip_stats["kept"] = len(kept_ips)
    else:
        ip_stats["removed_by_allow"] = 0

    regexes = []
    regex_dropped = []
    for pattern in dict.fromkeys(bm7_rules["regex"] + extra["regex"]):
        if regex_worth_keeping(pattern):
            regexes.append(pattern)
        else:
            regex_dropped.append(pattern)
    regexes.sort()

    user_keywords = sorted(set(extra["keyword"]))
    excluded_keywords = sorted(set(bm7_rules["keyword"]))

    by_source = {SRC_BM7: 0, SRC_ANTI: 0, SRC_EXTRA: 0}
    for _domain, src in suffixes:
        by_source[src] = by_source.get(src, 0) + 1
    for src in exact.values():
        by_source[src] = by_source.get(src, 0) + 1

    stats = {
        "sources": {
            "blackmatrix7_domain": {
                "domain_suffix": len(bm7_domains["suffix"]),
                "domain_exact": len(bm7_domains["exact"]),
                "invalid": bm7_domains["invalid"],
                "unsafe_suffix": bm7_domains["unsafe_suffix"],
                "unsafe_samples": bm7_domains["unsafe_samples"],
            },
            "blackmatrix7_rules": {
                "domain_suffix": len(bm7_rules["suffix"]),
                "domain_exact": len(bm7_rules["exact"]),
                "ip": len(bm7_rules["ip"]),
                "url_regex": len(bm7_rules["regex"]),
                "url_regex_kept": len([p for p in bm7_rules["regex"] if regex_worth_keeping(p)]),
                "url_regex_dropped": sorted(p for p in set(bm7_rules["regex"]) if not regex_worth_keeping(p)),
                "domain_keyword_excluded": len(set(bm7_rules["keyword"])),
            },
            "anti_ad_surge2": {
                "domain_suffix": len(anti["suffix"]),
                "domain_exact": len(anti["exact"]),
                "invalid": anti["invalid"],
                "unsafe_suffix": anti["unsafe_suffix"],
                "unsafe_samples": anti["unsafe_samples"],
                "format": "DOMAIN-SET（行首点号表示后缀）。anti-ad-surge.txt 是同一批后缀的 DOMAIN-SUFFIX 行，更占内存；anti-ad-domains.txt 没有做后缀合并。",
            },
            "extra_block": {
                "domain_suffix": len(extra["suffix"]),
                "domain_exact": len(extra["exact"]),
                "ip": len(extra["ip"]),
                "url_regex": len(extra["regex"]),
                "domain_keyword": len(extra["keyword"]),
            },
            "allow": {
                "domain_suffix": len(allow["suffix"]),
                "domain_exact": len(allow["exact"]),
                "ip": len(allow["ip"]),
                "removed_suffix_terminals": allow.get("removed_suffix_terminals", 0),
                "removed_exact": allow.get("removed_exact", 0),
                "exceptions": allow.get("exceptions", 0),
            },
        },
        "dedupe": {
            "duplicate_suffix": trie.duplicate,
            "covered_by_parent_suffix": trie.covered,
            "duplicate_exact": exact_dup,
            "exact_covered_by_suffix": exact_covered,
            "ip": ip_stats,
        },
        "final": {
            "domain_suffix": len(suffixes),
            "domain_exact": len(exact),
            "domain_total": len(suffixes) + len(exact),
            "ip": len(kept_ips),
            "url_regex": len(regexes),
            "user_keyword": len(user_keywords),
            "allow_exceptions": len(exceptions),
            "keyword_excluded": len(excluded_keywords),
            "domains_first_added_by": by_source,
        },
        "decisions": {
            "domain_keyword": "blackmatrix7 的 DOMAIN-KEYWORD 不进入生效规则。advert、crashlytics、fenxi、umeng 这类关键字误杀高，而且 crashlytics 已在上游 skip-proxy 里被保留。",
            "url_regex": "只保留能锚定到具体广告域名的 URL-REGEX。纯路径片段（例如 /\\d+/resolve）丢弃。HTTPS 上的 URL-REGEX 需要 Shadowrocket 的 MITM 才会看得到路径。",
            "ip": "只使用 blackmatrix7 Advertising.list 和自定义额外名单里的 IP。保留全球单播地址，去掉私网、链路本地和 0.0.0.0/8，并删掉被更大网段盖住的小网段。blackmatrix7 的 IP 带 no-resolve。",
            "excluded_local_ads": "不并入 johnshall 的 sr_ad_only.conf，也不并入上游配置里原来的内联广告段。",
            "config_order": "黑名单与国内外划分：DOMAIN-SET/RULE-SET 放在上游原来的广告段，手工 Proxy 仍在广告之前。白名单：引用放在直连/代理规则之前。",
        },
    }

    if stats["final"]["domain_total"] < 100_000:
        raise SystemExit(f"合并后的域名少得不正常: {stats['final']['domain_total']}")

    block = render_reference_block(bool(exceptions))
    write_outputs(suffixes, exact, kept_ips, regexes, user_keywords, excluded_keywords, exceptions, stats)
    write_configs(loaded, block)
    return stats


def write_outputs(suffixes, exact, kept_ips, regexes, user_keywords, excluded_keywords, exceptions, stats) -> None:
    suffix_lines = [f".{domain}" for domain, _src in sorted(suffixes)]
    exact_lines = sorted(exact)
    domain_header = [
        "# Shadowrocket DOMAIN-SET",
        "# 行首有点：匹配该域名及其子域名。行首无点：只匹配这一个主机名。",
        f"# DOMAIN-SET,{RAW_BASE}/dist/ad-domain.list,REJECT",
        f"# suffix: {len(suffix_lines)}",
        f"# exact: {len(exact_lines)}",
        "# sources: blackmatrix7 Advertising_Domain + anti-AD anti-ad-surge2",
    ]
    domain_text = "\n".join(domain_header + suffix_lines + exact_lines) + "\n"
    write_text(DIST / "ad-domain.list", domain_text)

    rule_lines = [
        "# Shadowrocket RULE-SET",
        "# 本文件不含策略名。配置里写成 RULE-SET,<URL>,REJECT",
        f"# ip: {len(kept_ips)}",
        f"# url-regex: {len(regexes)}",
        f"# user-keyword: {len(user_keywords)}",
    ]
    for item in kept_ips:
        line = f"IP-CIDR,{item['net']}"
        if item["no_resolve"]:
            line += ",no-resolve"
        rule_lines.append(line)
    rule_lines.extend(f"URL-REGEX,{pattern}" for pattern in regexes)
    rule_lines.extend(f"DOMAIN-KEYWORD,{keyword}" for keyword in user_keywords)
    write_text(DIST / "ad-rules.list", "\n".join(rule_lines) + "\n")

    keyword_path = DIST / "ad-keyword.list"
    if keyword_path.exists():
        keyword_path.unlink()

    if exceptions:
        allow_lines = [
            "# Shadowrocket RULE-SET，在配置里以 DIRECT 引用，并且放在广告 DOMAIN-SET 之前。",
            "# 这些域名被一条更短的广告后缀盖住。两份 .conf 会在 DOMAIN-SET 之前用 DIRECT 引用本文件。",
        ]
        allow_lines.extend(f"{kind},{domain}" for kind, domain in exceptions)
        write_text(DIST / "ad-allow.list", "\n".join(allow_lines) + "\n")
    else:
        allow_path = DIST / "ad-allow.list"
        if allow_path.exists():
            allow_path.unlink()

    write_text(DIST / "stats.json", json.dumps(stats, ensure_ascii=False, indent=2) + "\n")


def write_configs(loaded: dict[str, tuple[str, str]], block: list[str]) -> None:
    for name in AD_CONFIGS:
        original = loaded[name][0]
        rebuilt = rebuild_config(original, block)
        before = significant_rules(original.splitlines())
        after = significant_rules(rebuilt.splitlines())
        if before != after:
            raise SystemExit(f"{name} 的非广告规则被改动了，停止写入")
        reject_count = sum(1 for line in rebuilt.splitlines() if is_reject_line(line))
        domain_set = f"DOMAIN-SET,{RAW_BASE}/dist/ad-domain.list,REJECT"
        rule_set = f"RULE-SET,{RAW_BASE}/dist/ad-rules.list,REJECT"
        if reject_count != 0 or domain_set not in rebuilt or rule_set not in rebuilt:
            raise SystemExit(f"{name} 广告引用异常: reject={reject_count}")
        write_text(ROOT / name, rebuilt)
        print(f"写出 {name}: 保留非广告规则 {len(after)} 条")


def self_test() -> None:
    assert normalize_domain(".Example.COM.") == "example.com"
    assert normalize_domain("*.Ads.Example.com") == "ads.example.com"
    assert normalize_domain("com") is None
    assert is_unsafe_suffix("com.cn")
    assert not regex_worth_keeping(r"/\d+/resolve")
    assert regex_worth_keeping(r"^https?:\/\/ad\d\.sina\.com\.cn")

    trie = SuffixTrie()
    assert trie.add("ads.example.com", SRC_LOCAL) == "added"
    assert trie.add("example.com", SRC_BM7) == "pruned"
    assert trie.add("a.example.com", SRC_ANTI) == "covered"
    found = {domain for domain, _src in trie.collect()}
    assert found == {"example.com"}

    trie = SuffixTrie()
    trie.add("tracker.test", SRC_LOCAL)
    exact = {"keep.tracker.test": SRC_LOCAL, "other.test": SRC_BM7}
    allow = empty_bucket()
    allow["suffix"] = ["safe.tracker.test"]
    allow["exact"] = ["keep.tracker.test"]
    exceptions = apply_allow(trie, exact, allow)
    assert ("DOMAIN-SUFFIX", "safe.tracker.test") in exceptions
    assert trie.covers("safe.tracker.test")
    assert "keep.tracker.test" not in exact

    skeleton = "\n".join(
        [
            "[General]",
            "ipv6 = false",
            "[Rule]",
            "#",
            "# 白名单中包含了境外网站中可以访问的那些，对不确定的网站则尽可能地代理。",
            "# 包含广告过滤",
            "#",
            "",
            "# adblock rules refresh time: 2020",
            "DOMAIN-SUFFIX,ad.example,Reject",
            "IP-CIDR,203.0.113.5/32,Reject",
            "",
            "# 手工定义的 Direct 列表",
            "DOMAIN-SUFFIX,icloud-content.com,Direct",
            "DOMAIN-SUFFIX,tedcdn.com,Proxy",
            "FINAL,PROXY",
            "",
        ]
    )
    allow_block = render_reference_block(True)
    rebuilt = rebuild_config(skeleton, allow_block)
    assert "DOMAIN-SUFFIX,ad.example,Reject" not in rebuilt
    assert "DOMAIN-SUFFIX,icloud-content.com,Direct" in rebuilt
    assert "DOMAIN-SUFFIX,tedcdn.com,Proxy" in rebuilt
    assert rebuilt.index("ad-allow.list,DIRECT") < rebuilt.index("DOMAIN-SET,")
    assert rebuilt.index("DOMAIN-SET,") < rebuilt.index("DOMAIN-SUFFIX,icloud-content.com,Direct")
    assert significant_rules(skeleton.splitlines()) == significant_rules(rebuilt.splitlines())
    assert is_reject_line("IP-CIDR,1.2.3.4/32,Reject,no-resolve")

    ban = "\n".join(
        [
            "[Rule]",
            "# 黑名单中包含了 GFWList",
            "# 包含广告过滤",
            "DOMAIN-SUFFIX,tedcdn.com,Proxy",
            "",
            "# 手工定义的 Reject 列表",
            "#待反馈是否误杀",
            "DOMAIN-SUFFIX,ad.example,Reject",
            "DOMAIN-SUFFIX,goosevpn.com,Proxy",
            "FINAL,direct",
            "",
        ]
    )
    ban_block = render_reference_block(False)
    rebuilt_ban = rebuild_config(ban, ban_block)
    assert rebuilt_ban.index("DOMAIN-SUFFIX,tedcdn.com,Proxy") < rebuilt_ban.index("DOMAIN-SET,")
    assert rebuilt_ban.index("DOMAIN-SET,") < rebuilt_ban.index("DOMAIN-SUFFIX,goosevpn.com,Proxy")
    assert "手工定义的 Reject" not in rebuilt_ban
    assert significant_rules(ban.splitlines()) == significant_rules(rebuilt_ban.splitlines())
    rebuilt_twice = rebuild_config(rebuilt_ban, ban_block)
    assert rebuilt_twice.count(f"DOMAIN-SET,{RAW_BASE}/dist/ad-domain.list,REJECT") == 1

    kept, info = merge_ips(
        [
            ("1.2.3.0/24", True, SRC_BM7),
            ("1.2.3.8/32", True, SRC_BM7),
            ("8.8.8.8/32", False, SRC_LOCAL),
            ("10.1.2.0/24", True, SRC_BM7),
            ("0.0.0.0/24", True, SRC_BM7),
        ]
    )
    assert info["kept"] == 2, info
    assert any(str(item["net"]) == "8.8.8.8/32" and item["no_resolve"] is False for item in kept)
    assert any(str(item["net"]) == "1.2.3.0/24" for item in kept)
    print("self-test ok")


def print_summary(stats: dict) -> None:
    final = stats["final"]
    print("\n===== 合并结果 =====")
    sources = stats["sources"]
    print("已排除本仓库原有广告源（sr_ad_only.conf / 上游内联广告段）")
    print(
        "blackmatrix7 域名后缀 {domain_suffix}  精确域名 {domain_exact}".format(
            **sources["blackmatrix7_domain"]
        )
    )
    print(
        "blackmatrix7 规则 IP {ip}  URL-REGEX {url_regex}（保留 {url_regex_kept}）  关键字排除 {domain_keyword_excluded}".format(
            **sources["blackmatrix7_rules"]
        )
    )
    print(
        "anti-AD surge2 后缀 {domain_suffix}".format(**sources["anti_ad_surge2"])
    )
    dedupe = stats["dedupe"]
    print(
        "父后缀覆盖删掉 {covered_by_parent_suffix}  重复后缀 {duplicate_suffix}  被后缀覆盖的精确域名 {exact_covered_by_suffix}".format(
            **dedupe
        )
    )
    print(
        "最终后缀 {domain_suffix}  精确 {domain_exact}  域名合计 {domain_total}  IP {ip}  URL-REGEX {url_regex}".format(
            **final
        )
    )
    print("各来源最终留下的域名（先写入者计）", final["domains_first_added_by"])
    for path in (
        DIST / "ad-domain.list",
        DIST / "ad-rules.list",
        ROOT / "sr_top500_banlist_ad.conf",
        ROOT / "sr_top500_whitelist_ad.conf",
    ):
        data = path.read_bytes()
        print(f"{path.relative_to(ROOT)}: {data.count(bchr_newline())} 行, {len(data)} 字节")


def bchr_newline() -> int:
    return ord("\n")


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        self_test()
        return 0
    self_test()
    loaded = fetch_inputs()
    stats = build(loaded)
    print_summary(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
