
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


# ============================================================
# 基础配置
# ============================================================

TOKEN = os.environ["GITHUB_TOKEN"]
TARGET_REPO = os.environ["TARGET_REPO"]

VERGE_REPO = "clash-verge-rev/clash-verge-rev"
ANDROID_REPO = "MetaCubeX/ClashMetaForAndroid"

CONFIG_PATH = "link/clash.txt"

# 下载加速前缀：保留原有格式
CDN_PREFIX = (
    "https\\://pd.zwc365.com/cfworker/https\\://"
)

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
            f"{repo} 没有返回有效版本标签"
        )

    print(f"上游最新版本：{repo} -> {release['tag_name']}")
    return release


# ============================================================
# Release 更新说明
# 只保留“下载地址”之前的内容
# ============================================================

def build_release_notes(release):
    """
    只保留上游 Release 正文中“下载地址”之前的内容。

    从“下载地址”标题开始，后面的全部删除，包括：
    Windows/macOS/Linux 下载列表、FAQ、推荐链接、
    创建时间及其他后续内容。

    支持常见 Markdown 标题、加粗标题和普通独立标题。
    """

    body = (release.get("body") or "").strip()

    if not body:
        return "上游未提供更新说明。"

    stop_patterns = [
        # Markdown 标题，例如：## 下载地址
        r"(?im)^\s*#{1,6}\s*下载地址\b.*$",

        # 加粗标题，例如：**下载地址**
        r"(?im)^\s*\*{2}\s*下载地址\s*\*{2}\s*:?\s*$",

        # 普通独立标题，例如：下载地址
        r"(?im)^\s*下载地址\s*:?\s*$",
    ]

    stop_positions = []

    for pattern in stop_patterns:
        match = re.search(pattern, body)
        if match:
            stop_positions.append(match.start())

    if stop_positions:
        body = body[:min(stop_positions)].rstrip()

    return body or "上游未提供有效更新说明。"


# ============================================================
# 上游安装包匹配与下载
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
            f"上游现有文件：{names}"
        )

    return matches[0]


def download_asset(asset):
    url = asset["browser_download_url"]
    print(f"开始下载：{asset['name']}")

    req = urllib.request.Request(
        url,
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
        raise RuntimeError(
            f"下载文件为空：{asset['name']}"
        )

    expected_size = asset.get("size", 0)

    if expected_size and len(content) != expected_size:
        raise RuntimeError(
            f"文件大小校验失败：{asset['name']}；"
            f"预期 {expected_size} 字节，"
            f"实际 {len(content)} 字节"
        )

    print(
        f"下载成功：{asset['name']}，"
        f"{len(content)} 字节"
    )
    return content


# ============================================================
# 目标仓库 Release 管理
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
    print(f"创建目标 Release：{tag}")

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
    """
    规则：
    1. Release 不存在：创建 Release 并上传指定附件。
    2. Release 已存在且附件齐全：跳过下载与上传。
    3. Release 已存在但附件缺失：只补传缺失附件。
    4. 不上传任何未指定的上游安装包。
    """

    release = get_target_release(tag)

    if release is None:
        release = create_release(tag, title, body)
        existing_names = set()
        print("目标 Release 不存在，执行首次发布。")

    else:
        existing_assets = get_release_assets(release)
        existing_names = {
            asset["name"] for asset in existing_assets
        }

        expected_names = set(selected_assets.keys())
        missing_names = expected_names - existing_names

        if not missing_names:
            print(
                f"版本 {tag} 已发布，"
                "4 个安装包均已存在。"
            )
            print("跳过下载和上传。")
            return release

        print(
            f"Release {tag} 已存在，"
            f"需要补传 {len(missing_names)} 个附件。"
        )

        for name in sorted(missing_names):
            print(f"缺失附件：{name}")

    for filename, upstream_asset in selected_assets.items():
        if filename in existing_names:
            print(f"附件已存在，跳过：{filename}")
            continue

        content = download_asset(upstream_asset)
        upload_asset(release, filename, content)

    # 再次确认目标 Release 附件齐全
    final_assets = get_release_assets(release)
    final_names = {
        asset["name"] for asset in final_assets
    }

    missing_after_upload = (
        set(selected_assets.keys()) - final_names
    )

    if missing_after_upload:
        raise RuntimeError(
            "Release 附件检查失败，缺少："
            + ", ".join(sorted(missing_after_upload))
        )

    print(f"Release 附件检查通过：{tag}")
    return release


def target_asset_url(tag, filename):
    encoded_tag = urllib.parse.quote(tag, safe="")
    encoded_filename = urllib.parse.quote(filename, safe="")

    return (
        f"https://github.com/{TARGET_REPO}/releases/download/"
        f"{encoded_tag}/{encoded_filename}"
    )


def accelerated_url(url):
    if not url.startswith("https://"):
        raise ValueError(f"不是有效的 HTTPS 地址：{url}")

    return CDN_PREFIX + url[len("https://"):]


# ============================================================
# link/clash.txt 更新
# ============================================================

def read_config():
    encoded_path = urllib.parse.quote(
        CONFIG_PATH,
        safe="/",
    )

    info = api_json(
        f"{API}/repos/{TARGET_REPO}/contents/{encoded_path}"
    )

    if info.get("encoding") != "base64":
        raise RuntimeError(
            f"无法读取配置文件：{CONFIG_PATH}"
        )

    content = base64.b64decode(
        info["content"]
    ).decode("utf-8")

    return content, info["sha"]


def update_config(replacements):
    content, sha = read_config()
    updated = content

    for key, url in replacements.items():
        pattern = rf"(?m)^{re.escape(key)}=.*$"
        replacement = f"{key}={url}"

        if not re.search(pattern, updated):
            raise RuntimeError(
                f"配置文件缺少字段：{key}。"
                "为避免误改，停止提交。"
            )

        updated = re.sub(
            pattern,
            lambda _: replacement,
            updated,
        )

    if updated == content:
        print(
            "link/clash.txt 已是最新内容，跳过提交。"
        )
        return

    payload = {
        "message": "chore: update Clash download links",
        "content": base64.b64encode(
            updated.encode("utf-8")
        ).decode("ascii"),
        "sha": sha,
    }

    encoded_path = urllib.parse.quote(
        CONFIG_PATH,
        safe="/",
    )

    api_json(
        f"{API}/repos/{TARGET_REPO}/contents/{encoded_path}",
        "PUT",
        payload,
    )

    print("link/clash.txt 更新并提交成功。")


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

    # 使用独立标签，避免与其他软件 Release 冲突。
    target_tag = f"clash-verge-{version}"

    # 只处理指定的 4 个文件。
    # 不下载 Windows ARM64、Linux DEB/RPM 等文件。
    verge_specs = {
        "Windows_x64_url": (
            r"Clash\.Verge_.*_x64-setup\.exe"
        ),
        "Windows_web_url": (
            r"Clash\.Verge_.*_x64_fixed_webview2-setup\.exe"
        ),
        "Mac_intel_url": (
            r"Clash\.Verge_.*_x64\.dmg"
        ),
        "Mac_apple_url": (
            r"Clash\.Verge_.*_aarch64\.dmg"
        ),
    }

    selected_assets = {}

    for key, pattern in verge_specs.items():
        asset = find_asset(verge, pattern)
        selected_assets[asset["name"]] = asset
        print(f"{key}: {asset['name']}")

    # 只保留“下载地址”之前的上游更新说明。
    release_notes = build_release_notes(verge)

    publish_release(
        tag=target_tag,
        title=f"Clash Verge Rev {version}",
        body=release_notes,
        selected_assets=selected_assets,
    )

    print(
        "目标 Release："
        f"https://github.com/{TARGET_REPO}/releases/tag/"
        f"{urllib.parse.quote(target_tag, safe='')}"
    )

    # 更新目标仓库 4 个安装包的下载地址。
    for key, pattern in verge_specs.items():
        asset = find_asset(verge, pattern)

        url = target_asset_url(
            target_tag,
            asset["name"],
        )

        replacements[key] = accelerated_url(url)

    # --------------------------------------------------------
    # 二、Clash Meta for Android
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("检查 Clash Meta for Android")
    print("=" * 60)

    android = latest_release(ANDROID_REPO)

    android_specs = {
        "Android_arm_url": (
            r"cmfa-.*meta-universal-release\.apk"
        ),
        "Android_v8a_url": (
            r"cmfa-.*meta-arm64-v8a-release\.apk"
        ),
        "Android_v7a_url": (
            r"cmfa-.*meta-armeabi-v7a-release\.apk"
        ),
    }

    for key, pattern in android_specs.items():
        asset = find_asset(android, pattern)

        replacements[key] = accelerated_url(
            asset["browser_download_url"]
        )

        print(f"{key}: {asset['name']}")

    # --------------------------------------------------------
    # 三、更新 link/clash.txt
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("检查并更新 link/clash.txt")
    print("=" * 60)

    update_config(replacements)

    print()
    print("=" * 60)
    print("全部任务执行完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
