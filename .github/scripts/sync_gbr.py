"""Import GatherBuddyReborn releases into the shared Dalamud plugin index."""

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import zipfile

REPO = "KindSheepOvo/GatherBuddyReborn"
WEB_REPO = f"https://github.com/{REPO}"
API = f"https://api.github.com/repos/{REPO}"
NAME = "GatherBuddyReborn"
MANIFEST = f"{NAME}.json"
VERSION = re.compile(r"(?:0|[1-9][0-9]{0,4})(?:\.(?:0|[1-9][0-9]{0,4})){3}")
LINK_KEYS = ("DownloadLinkInstall", "DownloadLinkUpdate", "DownloadLinkTesting")
MANIFEST_MAX = 64 * 1024
ZIP_MAX = 64 * 1024 * 1024
ZIP_EXPANDED_MAX = 256 * 1024 * 1024
REQUIRED = {MANIFEST, f"{NAME}.dll", "raphael-cli.exe"}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json(data):
    def invalid_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    return json.loads(
        data.decode("utf-8-sig"),
        object_pairs_hook=unique,
        parse_constant=invalid_constant,
    )


def get(url, maximum, *, api=False, optional=False):
    headers = {"User-Agent": "DalamudPlugins-GatherBuddyReborn-sync"}
    if api:
        headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        token = os.environ.get("GH_TOKEN")
        if token:
            # Unredirected headers are deliberately excluded by urllib on redirects.
            request = Request(url, headers=headers)
            request.add_unredirected_header("Authorization", f"Bearer {token}")
        else:
            request = Request(url, headers=headers)
    else:
        request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=45) as response:
            final = urlsplit(response.geturl())
            hosts = (
                {"api.github.com"} if api
                else {"github.com", "release-assets.githubusercontent.com",
                      "objects.githubusercontent.com"}
            )
            require(final.scheme == "https" and final.hostname in hosts,
                    "Unexpected release response destination")
            size = response.headers.get("Content-Length")
            if size is not None:
                require(int(size) <= maximum, "Response too large")
            data = response.read(maximum + 1)
            require(len(data) <= maximum, "Response too large")
            return data
    except HTTPError as err:
        if optional and err.code == 404:
            return None
        raise


def version_parts(value):
    require(isinstance(value, str) and VERSION.fullmatch(value), "Bad plugin version")
    parts = tuple(map(int, value.split(".")))
    require(all(p <= 65534 for p in parts), "Version component out of range")
    return parts


def validate_archive(data, manifest_bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        seen = set()
        required = set()
        infos = archive.infolist()
        require(len(infos) <= 4096, "Too many ZIP members")
        require(sum(i.file_size for i in infos) <= ZIP_EXPANDED_MAX,
                "Expanded ZIP too large")
        for entry in infos:
            path = PurePosixPath(entry.filename)
            mode = stat.S_IFMT(entry.external_attr >> 16)
            require(
                entry.filename == entry.orig_filename
                and not path.is_absolute()
                and all(v not in {"", ".", ".."} for v in entry.filename.rstrip("/").split("/"))
                and not any(c in entry.filename for c in "\\:\x00")
                and mode in {0, stat.S_IFREG, stat.S_IFDIR}
                and not (entry.flag_bits & 1),
                "Unsafe ZIP member",
            )
            key = str(path).casefold()
            require(key not in seen, "Duplicate ZIP member")
            seen.add(key)
            if entry.filename in REQUIRED:
                require(not entry.is_dir() and entry.file_size > 0, "Empty required ZIP member")
                required.add(entry.filename)
        require(required == REQUIRED, "Required package components are missing")
        info = archive.getinfo(MANIFEST)
        require(info.file_size <= MANIFEST_MAX, "Embedded manifest is too large")
        require(archive.read(info) == manifest_bytes, "Manifest differs from embedded copy")
        require(archive.testzip() is None, "ZIP failed its CRC check")


def parse_manifest(raw, version):
    manifest = parse_json(raw)
    require(isinstance(manifest, dict), "Manifest must be an object")
    require(manifest.get("InternalName") == NAME, "Unexpected InternalName")
    require(manifest.get("AssemblyVersion") == version, "Manifest version differs from tag")
    require(manifest.get("TestingAssemblyVersion") == version, "Testing version differs from tag")
    require(manifest.get("RepoUrl") == WEB_REPO, "Unexpected source repository")
    require(manifest.get("IsTestingExclusive", False) is False, "Not a stable manifest")
    api_level = manifest.get("DalamudApiLevel")
    require(type(api_level) is int and 15 <= api_level <= 100, "Unsupported Dalamud API")
    require(all(isinstance(manifest.get(k), str) and manifest[k].strip()
                for k in ("Name", "Author", "Description", "Punchline")),
            "Missing plugin index display fields")
    return manifest


def get_asset(release, tag, filename, maximum):
    assets = [a for a in release["assets"] if a.get("name") == filename]
    require(len(assets) == 1, f"Expected exactly one {filename} asset")
    asset = assets[0]
    url = f"{WEB_REPO}/releases/download/{tag}/{filename}"
    require(asset["browser_download_url"] == url, "Unexpected asset URL")
    require(asset["state"] == "uploaded", "Release asset has not finished uploading")
    require(type(asset["size"]) is int and 0 < asset["size"] <= maximum,
            "Invalid release asset size")
    data = get(url, maximum)
    require(len(data) == asset["size"], "Release asset size does not match metadata")
    digest = asset.get("digest")
    if digest is not None:
        require(digest == f"sha256:{hashlib.sha256(data).hexdigest()}", "Asset hash mismatch")
    return data


def sync(index_path):
    payload = parse_json(index_path.read_bytes())
    require(isinstance(payload, list) and all(isinstance(e, dict) for e in payload),
            "Plugin index must be a JSON list of objects")
    slots = [i for i, item in enumerate(payload)
             if str(item.get("InternalName", "")).casefold() == NAME.casefold()]
    require(len(slots) <= 1, "Duplicate GBR entries in plugin index")
    old = payload[slots[0]] if slots else None
    if old is not None:
        require(old["InternalName"] == NAME and old.get("RepoUrl") == WEB_REPO,
                "Existing plugin name belongs to a different source")
    raw_release = get(f"{API}/releases/latest", 1024 * 1024, api=True, optional=True)
    if raw_release is None:
        print("No stable GatherBuddyReborn release to publish.")
        return
    release = parse_json(raw_release)
    require(release.get("draft") is False and release.get("prerelease") is False,
            "Only published stable releases are allowed")
    tag = release["tag_name"]
    require(isinstance(tag, str) and tag.startswith("cn-"), "Unexpected release tag")
    version = tag.removeprefix("cn-")
    target_version = version_parts(version)
    require(release["html_url"] == f"{WEB_REPO}/releases/tag/{tag}", "Unexpected release source")
    if old is not None and target_version <= version_parts(old["AssemblyVersion"]):
        print("The plugin index already references this or a newer release.")
        return
    raw_compare = get(f"{API}/compare/{quote(tag)}...main?per_page=1", 2 * 1024 * 1024, api=True)
    compare = parse_json(raw_compare)
    require(compare.get("status") in {"ahead", "identical"} and compare.get("behind_by") == 0,
            "Release tag is not reachable from verified source main")
    raw_manifest = get_asset(release, tag, MANIFEST, MANIFEST_MAX)
    manifest = parse_manifest(raw_manifest, version)
    if old is not None:
        require(manifest["DalamudApiLevel"] >= old["DalamudApiLevel"],
                "Refusing to roll back the published Dalamud API")
    filename = f"{NAME}-{version}.zip"
    archive_data = get_asset(release, tag, filename, ZIP_MAX)
    validate_archive(archive_data, raw_manifest)
    mirror_dir = index_path.parent / NAME
    mirror_dir.mkdir(exist_ok=True)
    require(mirror_dir.is_dir() and not mirror_dir.is_symlink(), "Unsafe mirror directory")
    mirror = mirror_dir / filename
    if mirror.exists() or mirror.is_symlink():
        require(not mirror.is_symlink() and mirror.is_file() and mirror.stat().st_size <= ZIP_MAX
                and mirror.read_bytes() == archive_data, "Published versioned ZIP is immutable")
    else:
        with mirror.open("xb") as output:
            output.write(archive_data)
    archive_url = (
        "https://raw.githubusercontent.com/KindSheepOvo/DalamudPlugins/"
        f"refs/heads/master/{NAME}/{filename}"
    )
    for key in LINK_KEYS:
        manifest[key] = archive_url
    manifest["IsTestingExclusive"] = False
    if slots:
        payload[slots[0]] = manifest
    else:
        payload.append(manifest)
    index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                          encoding="utf-8")
    print(f"Published GatherBuddyReborn {version} in the index.")


if __name__ == "__main__":
    sync(Path(sys.argv[1]))
