import re
import sys
import unicodedata
from urllib.request import Request, urlopen

PRIMARY_URL = (
    "https://raw.githubusercontent.com/"
    "islercn/BeiJing-Unicom-IPTV-List/master/iptv.m3u"
)

META_URL = (
    "https://raw.githubusercontent.com/"
    "dhfjl/iptv/master/iptv.m3u"
)

OLD_ADDRESS = "192.168.11.1:8888"
NEW_ADDRESS = "192.168.1.1:4022"
OUTPUT_FILE = "iptv.m3u"


def fetch(url):
    request = Request(
        url,
        headers={
            "User-Agent": "iptv-github-actions-updater/1.0"
        },
    )
    with urlopen(request, timeout=30) as response:
        data = response.read()

    return data.decode("utf-8-sig", errors="replace")


def normalize_name(value):
    value = unicodedata.normalize("NFKC", value or "")
    value = value.lower().strip()

    # 统一中英文括号、连接符和空白，方便匹配 CCTV-1 与 CCTV1 等写法
    value = value.replace("（", "(").replace("）", ")")
    value = re.sub(r"[\s_\-—–·|/]+", "", value)
    value = re.sub(r"[()（）]", "", value)

    return value


def parse_attributes(extinf_line):
    return dict(
        re.findall(r'([\w-]+)="([^"]*)"', extinf_line)
    )


def parse_m3u(text):
    lines = [
        line.strip()
        for line in text.replace("\r\n", "\n").split("\n")
        if line.strip()
    ]

    entries = []

    for index, line in enumerate(lines):
        if not line.startswith("#EXTINF"):
            continue

        name = line.split(",", 1)[1].strip() if "," in line else ""
        attributes = parse_attributes(line)

        url = None
        for next_line in lines[index + 1:]:
            if next_line.startswith("#"):
                continue
            url = next_line
            break

        if not url:
            continue

        entries.append(
            {
                "extinf": line,
                "name": name,
                "attributes": attributes,
                "url": url,
            }
        )

    return entries


def metadata_keys(entry):
    attributes = entry["attributes"]

    values = [
        entry["name"],
        attributes.get("tvg-name", ""),
        attributes.get("tvg-id", ""),
    ]

    return {
        normalize_name(value)
        for value in values
        if normalize_name(value)
    }


def main():
    primary_entries = parse_m3u(fetch(PRIMARY_URL))
    metadata_entries = parse_m3u(fetch(META_URL))

    if len(primary_entries) < 5:
        raise RuntimeError(
            "主播放列表条目数量异常，拒绝覆盖现有文件"
        )

    metadata_map = {}

    for entry in metadata_entries:
        for key in metadata_keys(entry):
            metadata_map.setdefault(key, entry)

    output = ["#EXTM3U"]
    matched = 0
    unmatched = []

    for primary in primary_entries:
        keys = metadata_keys(primary)
        metadata = None

        for key in keys:
            if key in metadata_map:
                metadata = metadata_map[key]
                break

        # 保留北京联通列表的频道顺序和播放地址
        url = primary["url"].replace(OLD_ADDRESS, NEW_ADDRESS)

        # 匹配成功时，使用 dhfjl/iptv 的台名、logo、group 等信息
        if metadata:
            extinf = metadata["extinf"]
            matched += 1
        else:
            # 匹配不到时保留主列表原有信息，不丢失频道
            extinf = primary["extinf"]
            unmatched.append(primary["name"])

        output.append(extinf)
        output.append(url)

    result = "\n".join(output) + "\n"

    if OLD_ADDRESS in result:
        raise RuntimeError(
            "输出文件中仍然存在旧地址"
        )

    if result.count("#EXTINF") != len(primary_entries):
        raise RuntimeError(
            "输出条目数量校验失败"
        )

    with open(OUTPUT_FILE, "w", encoding="utf-8", newline="\n") as file:
        file.write(result)

    print(f"主列表频道数: {len(primary_entries)}")
    print(f"匹配到元数据: {matched}")
    print(f"未匹配元数据: {len(unmatched)}")

    if unmatched:
        print("未匹配频道:")
        for name in unmatched:
            print(f"- {name}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"更新失败: {error}", file=sys.stderr)
        sys.exit(1)
