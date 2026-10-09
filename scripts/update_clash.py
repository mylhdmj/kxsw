
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


# ============================================================
# 配置
# ============================================================

TOKEN = os.environ["GITHUB_TOKEN"]
TARGET_REPO = os.environ["TARGET_REPO"]

VERGE_REPO = "clash-verge-rev/clash-verge-rev"
ANDROID_REPO = "MetaCubeX/ClashMetaForAndroid"

CONFIG_PATH = "link/clash.txt"

# 注意：这里使用正常 URL，不添加多余反斜杠
CDN_PREFIX = "https://pd.zwc365.com/cfworker/https://"

API = "https://api.github.com"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "Clash-Release-Updater",
}


# ============================================================
# GitHub API
# ============================================================

class GitHubAPIError(Exception):
    def __init__(self, status, url, detail):
        self.status = status
        self.url = url
        self.detail = detail
        super().__init__(
            f"HTTP {status}: {url}\n{detail[:1500]}"
        )


def request(url, method="GET", data=None, headers=None):
    req_headers = {**HEADERS, **(headers or {})}

    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers=req_headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read()

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise GitHubAPIError(
            exc.code, url, detail
        ) from exc


def api_json(url, method="GET", data=None):
    _, raw = request(url, method, data)
    return json.loads(raw.decode("utf-8")) if raw else {}


def latest_release(repo):
    release = api_json(
        f"{API}/repos/{repo}/releases/latest"
    )

    if release.get("draft") or release.get("prerelease"):
        raise RuntimeError(
            f"{repo} 返回的不是正式 Release"
        )

    if not release.get("tag_name"):
        raise RuntimeError(
            f"{repo} 未返回有效版本标签"
        )

    print(f"上游最新版本：{repo} -> {release['tag_name']}")
    return release


# ============================================================
# 更新说明
# 只保留“下载地址”之前的内容
# ============================================================

def build_release_notes(release):
    body = (release.get("body") or "").strip()

    if not body:
        return "上游未提供更新说明。"

    patterns = [
        r"(?im)^\s*#{1,6}\s*下载地址\b.*$",
        r"(?im)^\s*\*{2}\s*下载地址\s*\*{2}\s*:?\s*$",
        r"(?im)^\s*下载地址\s*:?\s*$",
    ]

    positions = []

    for pattern in patterns:
        match = re.search(pattern, body)
        if match:
            positions.append(match.start())

    if positions:
        body = body[:min(positions)].rstrip()

    return body or "上游未提供有效更新说明。"


# ============================================================
# 匹配及下载指定安装包
# ============================================================

def find_asset(release, pattern):
    matches = [
        asset
        for asset in release.get("assets", [])
        if re.fullmatch(pattern, asset["name"], re.I)
    ]

    if len(matches) != 1:
        names = [
            asset["name"]
            for asset in release.get("assets", [])
        ]
        raise RuntimeError(
            f"安装包匹配失败：{pattern}\n"
            f"匹配数量：{len(matches)}\n"
            f"上游文件：{names}"
        )

    return matches[0]


def download_asset(asset):
    print(f"开始下载：{asset['name']}")

    req = urllib.request.Request(
        asset["browser_download_url"],
        headers={
            "User-Agent": "Clash-Release-Updater",
            "Accept": "application/octet-stream",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            content = resp.read()
    except Exception as exc:
        raise RuntimeError(
            f"下载失败：{asset['name']}：{exc}"
        ) from exc

    if not content:
        raise RuntimeError(f"下载文件为空：{asset['name']}")

    expected_size = asset.get("size", 0)
    if expected_size and len(content) != expected_size:
        raise RuntimeError(
            f"文件大小不一致：{asset['name']}；"
            f"预期 {expected_size}，实际 {len(content)}"
        )

    print(f"下载成功：{asset['name']}，{len(content)} 字节")
    return content


# ============================================================
# 目标仓库 Release
# ============================================================

def get_target_release(tag):
    encoded_tag = urllib.parse.quote(tag, safe="")
    url = (
        f"{API}/repos/{TARGET_REPO}/releases/tags/"
        f"{encoded_tag}"
    )

    try:
        return api_json(url)
    except GitHubAPIError as exc:
        if exc.status == 404:
            return None
        raise


def get_release_assets(release):
    detail = api_json(
        f"{API}/repos/{TARGET_REPO}/releases/"
        f"{release['id']}"
    )
    return detail.get("assets", [])


def create_release(tag, title, body):
    print(f"创建 Release：{title}，标签：{tag}")

    return api_json(
        f"{API}/repos/{TARGET_REPO}/releases",
        "POST",
        {
            "tag_name": tag,
            "name": title,
            "body": body,
            "draft": False,
            "prerelease": False,
            "make_latest": "false",
        },
    )


def update_release_metadata(release, title, body):
    """
    已存在的目标 Release 也同步标题和更新说明。
    不改变已有标签。
    """
    if (
        release.get("name") == title
        and release.get("body", "") == body
    ):
        return release

    print(f"更新 Release 标题和说明：{title}")

    return api_json(
        f"{API}/repos/{TARGET_REPO}/releases/"
        f"{release['id']}",
        "PATCH",
        {
            "name": title,
            "body": body,
        },
    )


def upload_asset(release, filename, content):
    upload_url = (
        f"https://uploads.github.com/repos/"
        f"{TARGET_REPO}/releases/{release['id']}/assets"
        f"?name={urllib.parse.quote(filename, safe='')}"
    )

    req = urllib.request.Request(
        upload_url,
        data=content,
        method="POST",
        headers={
            **HEADERS,
            "Content-Type": "application/octet-stream",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        print(f"上传成功：{result['name']}")

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(
            f"上传失败：{filename}，HTTP {exc.code}\n"
            f"{detail[:1500]}"
        ) from exc


def publish_release(tag, title, body, selected_assets):
    release = get_target_release(tag)

    if release is None:
        release = create_release(tag, title, body)
        existing_names = set()
    else:
        release = update_release_metadata(
            release, title, body
        )

        existing_names = {
            asset["name"]
            for asset in get_release_assets(release)
        }

        missing_names = (
            set(selected_assets) - existing_names
        )

        if not missing_names:
            print(
                f"版本 {tag} 已存在，"
                "4 个指定附件齐全，跳过下载和上传。"
            )
            return release

        print(f"需要补传附件：{sorted(missing_names)}")

    # 只上传缺失文件，不处理其他架构。
    for filename, upstream_asset in selected_assets.items():
        if filename in existing_names:
            print(f"附件已存在，跳过：{filename}")
            continue

        content = download_asset(upstream_asset)
        upload_asset(release, filename, content)

    final_names = {
        asset["name"]
        for asset in get_release_assets(release)
    }

    missing = set(selected_assets) - final_names

    if missing:
        raise RuntimeError(
            "Release 附件不完整：" + ", ".join(sorted(missing))
        )

    print(f"Release 检查完成：{tag}")
    return release


def target_asset_url(tag, filename):
    return (
        f"https://github.com/{TARGET_REPO}/releases/download/"
        f"{urllib.parse.quote(tag, safe='')}/"
        f"{urllib.parse.quote(filename, safe='')}"
    )


def accelerated_url(url):
    if not url.startswith("https://"):
        raise ValueError(f"无效的 HTTPS 地址：{url}")

    # 输出正常的 https://，绝不添加反斜杠。
    return CDN_PREFIX + url[len("https://"):]


# ============================================================
# 配置文件读写
# ============================================================

def read_config():
    path = urllib.parse.quote(CONFIG_PATH, safe="/")
    info = api_json(
        f"{API}/repos/{TARGET_REPO}/contents/{path}"
    )

    if info.get("encoding") != "base64":
        raise RuntimeError(f"无法读取 {CONFIG_PATH}")

    content = base64.b64decode(
        info["content"]
    ).decode("utf-8")

    return content, info["sha"]


def update_config(replacements):
    """
    只更新 Clash 区块。

    从独立的 Clash 标题开始，到独立的 FlClash 标题之前结束。
    所有 FlClash 内容原样保留。
    """

    content, sha = read_config()

    # 保留原始行结束符，避免改动文件其他部分的格式。
    lines = content.splitlines(keepends=True)

    clash_indices = [
        i for i, line in enumerate(lines)
        if line.strip() == "Clash"
    ]

    flclash_indices = [
        i for i, line in enumerate(lines)
        if line.strip() == "FlClash"
    ]

    if len(clash_indices) != 1:
        raise RuntimeError(
            "无法唯一定位 Clash 区块，停止修改。"
        )

    if len(flclash_indices) != 1:
        raise RuntimeError(
            "无法唯一定位 FlClash 区块，停止修改。"
        )

    clash_start = clash_indices[0]
    flclash_start = flclash_indices[0]

    if flclash_start <= clash_start:
        raise RuntimeError(
            "Clash 与 FlClash 区块顺序异常，停止修改。"
        )

    # 只在 Clash 标题和 FlClash 标题之间操作。
    section = lines[clash_start + 1:flclash_start]
    section_text = "".join(section)

    for key, url in replacements.items():
        pattern = rf"(?m)^{re.escape(key)}=.*$"

        if not re.search(pattern, section_text):
            raise RuntimeError(
                f"Clash 区块中缺少字段：{key}。"
                "为保护 FlClash 和其他配置，停止提交。"
            )

        # 兼容旧配置中已有的 https\://。
        # 替换成新的正常 HTTPS URL。
        section_text = re.sub(
            pattern,
            lambda _: f"{key}={url}",
            section_text,
        )

    # 重组文件，FlClash 区块不做任何修改。
    updated = (
        "".join(lines[:clash_start + 1])
        + section_text
        + "".join(lines[flclash_start:])
    )

    if updated == content:
        print("Clash 配置没有变化，跳过提交。")
        return

    payload = {
        "message": "chore: update Clash download links",
        "content": base64.b64encode(
            updated.encode("utf-8")
        ).decode("ascii"),
        "sha": sha,
    }

    path = urllib.parse.quote(CONFIG_PATH, safe="/")

    api_json(
        f"{API}/repos/{TARGET_REPO}/contents/{path}",
        "PUT",
        payload,
    )

    print("Clash 区块更新成功；FlClash 区块未修改。")


# ============================================================
# 主流程
# ============================================================

def main():
    replacements = {}

    # --------------------------------------------------------
    # 一、Clash Verge Rev
    # --------------------------------------------------------

    print("=" * 60)
    print("检查 Clash Verge Rev")
    print("=" * 60)

    verge = latest_release(VERGE_REPO)
    version = verge["tag_name"]

    # 使用上游原始标签，例如 v2.5.8。
    target_tag = version

    # 标题格式：Clash.Verge_2.5.8
    version_number = version.removeprefix("v")
    target_title = f"Clash.Verge_{version_number}"

    # 仅抓取这四个文件。
    verge_specs = {
        "Windows_x64_url":
            r"Clash\.Verge_.*_x64-setup\.exe",

        "Windows_web_url":
            r"Clash\.Verge_.*_x64_fixed_webview2-setup\.exe",

        "Mac_intel_url":
            r"Clash\.Verge_.*_x64\.dmg",

        "Mac_apple_url":
            r"Clash\.Verge_.*_aarch64\.dmg",
    }

    selected_assets = {}

    for key, pattern in verge_specs.items():
        asset = find_asset(verge, pattern)
        selected_assets[asset["name"]] = asset
        print(f"{key}: {asset['name']}")

    release_notes = build_release_notes(verge)

    publish_release(
        tag=target_tag,
        title=target_title,
        body=release_notes,
        selected_assets=selected_assets,
    )

    print(
        f"Release 地址："
        f"https://github.com/{TARGET_REPO}/releases/tag/"
        f"{urllib.parse.quote(target_tag, safe='')}"
    )

    # 更新 Clash 区块中的四个桌面端地址。
    for key, pattern in verge_specs.items():
        asset = find_asset(verge, pattern)

        replacements[key] = accelerated_url(
            target_asset_url(target_tag, asset["name"])
        )

    # --------------------------------------------------------
    # 二、Clash Meta for Android
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("检查 Clash Meta for Android")
    print("=" * 60)

    android = latest_release(ANDROID_REPO)

    android_specs = {
        "Android_arm_url":
            r"cmfa-.*meta-universal-release\.apk",

        "Android_v8a_url":
            r"cmfa-.*meta-arm64-v8a-release\.apk",

        "Android_v7a_url":
            r"cmfa-.*meta-armeabi-v7a-release\.apk",
    }

    for key, pattern in android_specs.items():
        asset = find_asset(android, pattern)

        replacements[key] = accelerated_url(
            asset["browser_download_url"]
        )

        print(f"{key}: {asset['name']}")

    # --------------------------------------------------------
    # 三、仅更新 Clash 区块
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("更新 link/clash.txt")
    print("=" * 60)

    update_config(replacements)

    print()
    print("=" * 60)
    print("全部任务执行完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
