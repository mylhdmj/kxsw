
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
CDN_PREFIX = "https\\://pd.zwc365.com/cfworker/https\\://"

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

    if not raw:
        return {}

    return json.loads(raw.decode("utf-8"))


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

    print(
        f"上游版本：{repo} -> "
        f"{release['tag_name']}"
    )

    return release


# ============================================================
# 资产匹配和下载
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
            f"资产匹配失败：{pattern}\n"
            f"匹配数量：{len(matches)}\n"
            f"当前上游资产：{names}"
        )

    return matches[0]


def download_asset(asset):
    url = asset["browser_download_url"]

    print(f"下载：{asset['name']}")

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
        f"下载成功：{asset['name']} "
        f"({len(content)} 字节)"
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
    return api_json(
        f"{API}/repos/{TARGET_REPO}/releases/"
        f"{release['id']}"
    ).get("assets", [])


def create_release(tag, title, body):
    print(f"创建 Release：{tag}")

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
            result = json.loads(
                resp.read().decode("utf-8")
            )

        print(f"上传成功：{result['name']}")

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(
            f"上传失败：{filename}，HTTP {exc.code}\n"
            f"{detail[:1500]}"
        ) from exc


def publish_release(tag, title, body, selected_assets):
    """
    selected_assets:
        {
            "文件名.exe": 上游 asset 对象,
            ...
        }

    规则：
    1. Release 不存在：创建 Release。
    2. Release 已存在且附件齐全：不下载、不上传。
    3. Release 已存在但附件缺失：只下载并上传缺失附件。
    """

    release = get_target_release(tag)

    if release is None:
        release = create_release(tag, title, body)
        existing_names = set()
        print("目标 Release 不存在，准备首次发布。")

    else:
        existing_assets = get_release_assets(release)
        existing_names = {
            asset["name"]
            for asset in existing_assets
        }

        expected_names = set(selected_assets)
        missing_names = expected_names - existing_names

        if not missing_names:
            print(
                f"版本 {tag} 已发布，4 个安装包均存在。"
            )
            print("跳过下载、上传和 Release 重复创建。")

            return release

        print(
            f"版本 {tag} 已存在，"
            f"需要补传 {len(missing_names)} 个文件："
        )

        for name in sorted(missing_names):
            print(f"  - {name}")

    # 只处理目标 Release 中不存在的文件。
    for filename, upstream_asset in selected_assets.items():
        if filename in existing_names:
            print(f"附件已存在，跳过：{filename}")
            continue

        content = download_asset(upstream_asset)
        upload_asset(release, filename, content)

    # 确认所有附件均已上传成功。
    final_assets = get_release_assets(release)
    final_names = {
        asset["name"]
        for asset in final_assets
    }

    missing_after_upload = set(selected_assets) - final_names

    if missing_after_upload:
        raise RuntimeError(
            "Release 附件检查失败，缺少："
            + ", ".join(sorted(missing_after_upload))
        )

    print(f"Release 发布完成：{tag}")

    return release


def target_asset_url(tag, filename):
    return (
        f"https://github.com/{TARGET_REPO}/releases/download/"
        f"{urllib.parse.quote(tag, safe='')}/"
        f"{urllib.parse.quote(filename, safe='')}"
    )


def accelerated_url(url):
    if not url.startswith("https://"):
        raise ValueError(f"无效下载地址：{url}")

    # 保留指定加速前缀。
    return CDN_PREFIX + url[len("https://"):]


# ============================================================
# link/clash.txt 更新
# ============================================================

def read_config():
    path = urllib.parse.quote(CONFIG_PATH, safe="/")

    info = api_json(
        f"{API}/repos/{TARGET_REPO}/contents/{path}"
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
                f"配置文件中不存在字段：{key}；"
                "为避免误改其他内容，已停止提交。"
            )

        updated = re.sub(
            pattern,
            lambda _: replacement,
            updated,
        )

    if updated == content:
        print("link/clash.txt 已是最新内容，无需提交。")
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

    print("link/clash.txt 更新并提交成功。")


# ============================================================
# 主流程
# ============================================================

def main():
    replacements = {}

    # --------------------------------------------------------
    # 1. 检查 Clash Verge Rev
    # --------------------------------------------------------

    print("=" * 60)
    print("检查 Clash Verge Rev")
    print("=" * 60)

    verge = latest_release(VERGE_REPO)
    version = verge["tag_name"]

    # 上游通常使用 v2.5.8 这样的 tag。
    target_tag = f"clash-verge-{version}"

    specs = {
        "Windows_x64": (
            r"Clash\.Verge_.*_x64-setup\.exe"
        ),
        "Windows_web": (
            r"Clash\.Verge_.*_x64_fixed_webview2-setup\.exe"
        ),
        "Mac_intel": (
            r"Clash\.Verge_.*_x64\.dmg"
        ),
        "Mac_apple": (
            r"Clash\.Verge_.*_aarch64\.dmg"
        ),
    }

    selected_assets = {}

    for key, pattern in specs.items():
        asset = find_asset(verge, pattern)
        selected_assets[asset["name"]] = asset

        print(f"{key}: {asset['name']}")

    # 版本相同且四个附件都齐全时，不会下载或上传。
    release = publish_release(
        tag=target_tag,
        title=f"Clash Verge Rev {version}",
        body=verge.get("body") or "上游未提供更新说明。",
        selected_assets=selected_assets,
    )

    release_url = (
        f"https://github.com/{TARGET_REPO}/releases/tag/"
        f"{urllib.parse.quote(target_tag, safe='')}"
    )

    print(f"Clash Verge Rev Release：{release_url}")

    for key, pattern in specs.items():
        asset = find_asset(verge, pattern)

        url = target_asset_url(
            target_tag,
            asset["name"],
        )

        replacements[f"{key}_url"] = accelerated_url(url)

    # --------------------------------------------------------
    # 2. 检查 Clash Meta for Android
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
    # 3. 更新配置文件
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
