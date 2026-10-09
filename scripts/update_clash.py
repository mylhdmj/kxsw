
import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ["GITHUB_TOKEN"]
TARGET_REPO = os.environ["TARGET_REPO"]

VERGE_REPO = "clash-verge-rev/clash-verge-rev"
ANDROID_REPO = "MetaCubeX/ClashMetaForAndroid"
CONFIG_PATH = "link/clash.txt"
CDN_PREFIX = "https://pd.zwc365.com/cfworker/"

API = "https://api.github.com"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "Clash-Release-Updater",
}


def request(url, method="GET", data=None, headers=None):
    h = {**HEADERS, **(headers or {})}
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(
            f"HTTP {e.code}: {url}\n{detail[:1500]}"
        ) from e


def api_json(url, method="GET", data=None):
    _, raw = request(url, method, data)
    return json.loads(raw) if raw else {}


def latest_release(repo):
    release = api_json(f"{API}/repos/{repo}/releases/latest")
    if release.get("draft") or release.get("prerelease"):
        raise RuntimeError(f"{repo} 最新 Release 不是正式版本")
    return release


def find_asset(release, pattern):
    matches = [
        a for a in release["assets"]
        if re.search(pattern, a["name"], re.I)
    ]
    if len(matches) != 1:
        names = [a["name"] for a in release["assets"]]
        raise RuntimeError(
            f"无法唯一匹配资源 {pattern}；当前资源：{names}"
        )
    return matches[0]


def download_asset(asset):
    req = urllib.request.Request(
        asset["browser_download_url"],
        headers={
            "User-Agent": "Clash-Release-Updater",
            "Accept": "application/octet-stream",
        },
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        content = resp.read()

    if not content:
        raise RuntimeError(f"下载文件为空：{asset['name']}")

    expected = asset.get("size", 0)
    if expected and len(content) != expected:
        raise RuntimeError(
            f"文件大小不一致：{asset['name']}，"
            f"预期 {expected}，实际 {len(content)}"
        )
    return content


def get_target_release(tag):
    url = f"{API}/repos/{TARGET_REPO}/releases/tags/{urllib.parse.quote(tag, safe='')}"
    try:
        return api_json(url)
    except RuntimeError as e:
        if "HTTP 404:" in str(e):
            return None
        raise


def publish_release(tag, title, body, files):
    release = get_target_release(tag)

    if release is None:
        release = api_json(
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
    else:
        release = api_json(
            f"{API}/repos/{TARGET_REPO}/releases/{release['id']}",
            "PATCH",
            {"name": title, "body": body},
        )

    # 同一版本重复运行时，先删除同名旧附件，再重新上传。
    existing = api_json(
        f"{API}/repos/{TARGET_REPO}/releases/{release['id']}"
    )
    by_name = {a["name"]: a for a in existing["assets"]}

    for filename, content in files.items():
        if filename in by_name:
            api_json(
                f"{API}/repos/{TARGET_REPO}/releases/assets/"
                f"{by_name[filename]['id']}",
                "DELETE",
            )

        upload_url = (
            f"https://uploads.github.com/repos/{TARGET_REPO}/releases/"
            f"{release['id']}/assets?name={urllib.parse.quote(filename)}"
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
                result = json.loads(resp.read())
                print(f"上传完成：{result['name']}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"上传失败：{filename}，HTTP {e.code}，"
                f"{e.read().decode('utf-8', 'replace')[:1000]}"
            ) from e

    return f"https://github.com/{TARGET_REPO}/releases/tag/{tag}"


def read_config():
    url = (
        f"{API}/repos/{TARGET_REPO}/contents/"
        f"{urllib.parse.quote(CONFIG_PATH, safe='/')}"
    )
    info = api_json(url)
    if info.get("encoding") != "base64":
        raise RuntimeError(f"无法读取配置文件：{CONFIG_PATH}")
    content = base64.b64decode(info["content"]).decode("utf-8")
    return content, info["sha"]


def update_config(replacements):
    content, sha = read_config()
    updated = content

    for key, value in replacements.items():
        pattern = rf"(?m)^{re.escape(key)}=.*$"
        replacement = f"{key}={value}"

        if re.search(pattern, updated):
            updated = re.sub(pattern, lambda _: replacement, updated)
        else:
            raise RuntimeError(f"配置文件中缺少字段：{key}")

    if updated == content:
        print("link/clash.txt 无需修改")
        return

    payload = {
        "message": "chore: update Clash download links",
        "content": base64.b64encode(updated.encode("utf-8")).decode(),
        "sha": sha,
    }
    api_json(
        f"{API}/repos/{TARGET_REPO}/contents/"
        f"{urllib.parse.quote(CONFIG_PATH, safe='/')}",
        "PUT",
        payload,
    )
    print("已提交 link/clash.txt 更新")


def main():
    print("检查 Clash Verge Rev 最新版本……")
    verge = latest_release(VERGE_REPO)
    version = verge["tag_name"]
    version_number = version.removeprefix("v")
    print(f"Clash Verge Rev：{version}")

    # 通过文件名匹配上游四个正式安装包。
    specs = {
        "Windows_x64": r"^Clash\.Verge_.*_x64-setup\.exe$",
        "Windows_web": r"^Clash\.Verge_.*_x64_fixed_webview2-setup\.exe$",
        "Mac_intel": r"^Clash\.Verge_.*_x64\.dmg$",
        "Mac_apple": r"^Clash\.Verge_.*_aarch64\.dmg$",
    }

    selected = {
        key: find_asset(verge, pattern)
        for key, pattern in specs.items()
    }

    # 先下载并验证全部文件，避免只上传部分安装包。
    files = {
        asset["name"]: download_asset(asset)
        for asset in selected.values()
    }

    target_tag = f"clash-verge-{version}"
    release_url = publish_release(
        target_tag,
        f"Clash Verge Rev {version}",
        verge.get("body") or "上游未提供更新说明。",
        files,
    )
    print(f"Release：{release_url}")

    replacements = {}
    for key, asset in selected.items():
        # 配置文件使用反斜杠转义冒号，并保留原加速前缀。
        upstream_url = (
            f"https://github.com/{TARGET_REPO}/releases/download/"
            f"{urllib.parse.quote(target_tag, safe='')}/"
            f"{urllib.parse.quote(asset['name'])}"
        )
        replacements[f"{key}_url"] = (
            "https\\://pd.zwc365.com/cfworker/https\\://"
            + upstream_url.removeprefix("https://")
        )

    print("检查 Clash Meta for Android 最新版本……")
    android = latest_release(ANDROID_REPO)
    print(f"Clash Meta for Android：{android['tag_name']}")

    android_specs = {
        "Android_arm_url": r"(?i)^cmfa-.*meta-universal-release\.apk$",
        "Android_v8a_url": r"(?i)^cmfa-.*meta-arm64-v8a-release\.apk$",
        "Android_v7a_url": r"(?i)^cmfa-.*meta-armeabi-v7a-release\.apk$",
    }

    for key, pattern in android_specs.items():
        asset = find_asset(android, pattern)
        replacements[key] = (
            "https\\://pd.zwc365.com/cfworker/https\\://"
            + asset["browser_download_url"].removeprefix("https://")
        )

    update_config(replacements)
    print("所有检查及更新完成。")


if __name__ == "__main__":
    main()
