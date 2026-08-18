import http.cookiejar
import json
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import quote, urljoin
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen


SOURCE_URL = (
    "https://raw.githubusercontent.com/"
    "islercn/BeiJing-Unicom-IPTV-List/master/iptv.m3u"
)
EPG_SITE = "https://epg.51zmt.top:8001/"
EPG_XML = "http://epg.51zmt.top:8000/e.xml.gz"
OLD_ADDRESS = "192.168.11.1:8888"
NEW_ADDRESS = "192.168.1.1:4022"
OUTPUT_FILE = Path("iptv.m3u")
MAX_UPLOAD_BYTES = 500 * 1024
MIN_MATCH_RATE = 0.40


def request(url, *, opener=None, data=None, headers=None, timeout=60):
    client = opener or urlopen
    req = Request(
        url,
        data=data,
        headers={
            "User-Agent": "dhfjl-iptv-github-actions/3.0",
            **(headers or {}),
        },
    )
    response = client.open(req, timeout=timeout) if opener else client(req, timeout=timeout)
    with response:
        return response.read(), response.headers


def channel_urls(text):
    return [
        line.strip()
        for line in text.replace("\r\n", "\n").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def build_multipart(fields, filename, file_data):
    boundary = f"----iptv-{uuid.uuid4().hex}"
    body = bytearray()

    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        f'Content-Disposition: form-data; name="m3u_file"; '
        f'filename="{filename}"\r\n'.encode()
    )
    body.extend(b"Content-Type: application/x-mpegURL\r\n\r\n")
    body.extend(file_data)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    return bytes(body), boundary


def apply_epg_header(text):
    lines = text.replace("\r\n", "\n").splitlines()
    header = f'#EXTM3U x-tvg-url="{EPG_XML}"'

    if lines and lines[0].lstrip("\ufeff").startswith("#EXTM3U"):
        lines[0] = header
    else:
        lines.insert(0, header)

    return "\n".join(lines).rstrip() + "\n"


def fix_known_bad_matches(text):
    corrections = {
        "BRTV北京卫视": {
            "id": "30",
            "name": "北京卫视",
            "logo": "http://epg.51zmt.top:8000/tb1/ws/beijing.png",
            "group": "卫视",
        },
        "BRTV纪实科教": {
            "id": "1872",
            "name": "BRTV纪实科教",
            "logo": "http://epg.51zmt.top:8000/tb1/sheng/BTV科教.png",
            "group": "地方",
        },
        "BRTV卡酷少儿": {
            "id": "67",
            "name": "卡酷动画",
            "logo": "http://epg.51zmt.top:8000/tb1/qt/kaku.png",
            "group": "地方",
        },
    }
    output = []

    for line in text.splitlines():
        if not line.startswith("#EXTINF") or "," not in line:
            output.append(line)
            continue

        display_name = line.split(",", 1)[1].strip()
        correction = next(
            (
                values
                for prefix, values in corrections.items()
                if display_name.startswith(prefix)
            ),
            None,
        )

        if not correction:
            output.append(line)
            continue

        output.append(
            '#EXTINF:-1 '
            f'tvg-id="{correction["id"]}" '
            f'tvg-name="{correction["name"]}" '
            f'tvg-logo="{correction["logo"]}" '
            f'group-title="{correction["group"]}", {display_name}'
        )

    return "\n".join(output).rstrip() + "\n"


def main():
    source_bytes, _ = request(SOURCE_URL, timeout=30)

    if len(source_bytes) > MAX_UPLOAD_BYTES:
        raise RuntimeError("源播放列表超过网站允许的 500KB")

    source_text = source_bytes.decode("utf-8-sig", errors="strict")
    source_urls = channel_urls(source_text)

    if len(source_urls) < 5:
        raise RuntimeError("源播放列表频道数量异常")

    cookie_jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    homepage_bytes, _ = request(EPG_SITE, opener=opener, timeout=30)
    homepage = homepage_bytes.decode("utf-8", errors="replace")
    csrf_match = re.search(
        r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', homepage
    )

    if not csrf_match:
        raise RuntimeError("无法从 EPG 网站取得 CSRF Token")

    fields = {
        "csrfmiddlewaretoken": csrf_match.group(1),
        "use_source_name": "on",
        # 不传 use_source_category，让网站生成央视/卫视/地方等分组。
        # 不传 use_source_logo，让网站匹配并写入台标。
        "unmatched_tvg_name": "noepg",
    }
    body, boundary = build_multipart(fields, "iptv.m3u", source_bytes)
    response_bytes, _ = request(
        urljoin(EPG_SITE, "upload/"),
        opener=opener,
        data=body,
        timeout=120,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Referer": EPG_SITE,
            "Origin": EPG_SITE.rstrip("/"),
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRFToken": csrf_match.group(1),
        },
    )
    result = json.loads(response_bytes.decode("utf-8"))

    if not result.get("success"):
        raise RuntimeError(f"EPG 网站处理失败: {result.get('msg', '未知错误')}")

    output_filename = result.get("output_filename", "")

    if not re.fullmatch(r"[A-Za-z0-9._-]+\.m3u", output_filename):
        raise RuntimeError("EPG 网站返回了异常的输出文件名")

    reported_total = int(result.get("channel_num", 0))
    matched = int(result.get("channel_num_check", 0))

    if reported_total != len(source_urls):
        raise RuntimeError(
            f"EPG 网站报告 {reported_total} 个频道，源文件有 {len(source_urls)} 个"
        )

    if matched / reported_total < MIN_MATCH_RATE:
        raise RuntimeError(f"EPG 匹配率过低: {matched}/{reported_total}")

    download_url = urljoin(
        EPG_SITE,
        f"download/{quote(output_filename)}/",
    )
    generated_bytes, _ = request(download_url, opener=opener, timeout=60)
    generated = generated_bytes.decode("utf-8-sig", errors="strict")
    generated_urls = channel_urls(generated)

    # 网站应当只补全元数据，不能改变频道数量、顺序或播放地址。
    if generated_urls != source_urls:
        raise RuntimeError("EPG 网站返回的频道顺序或播放地址发生变化")

    generated = fix_known_bad_matches(generated)
    generated = generated.replace(OLD_ADDRESS, NEW_ADDRESS)
    generated = apply_epg_header(generated)

    if OLD_ADDRESS in generated:
        raise RuntimeError("输出文件仍然包含旧地址")

    if generated.count("#EXTINF") != len(source_urls):
        raise RuntimeError("输出文件频道数量校验失败")

    with OUTPUT_FILE.open("w", encoding="utf-8", newline="\n") as output:
        output.write(generated)

    logo_count = sum(
        1
        for line in generated.splitlines()
        if line.startswith("#EXTINF")
        and re.search(r'tvg-logo="[^"]+"', line)
    )

    print(f"源频道数: {len(source_urls)}")
    print(f"EPG 匹配数: {matched}")
    print(f"有效台标数: {logo_count}")
    print(f"地址替换: {OLD_ADDRESS} -> {NEW_ADDRESS}")
    print(f"EPG 地址: {EPG_XML}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"更新失败: {error}", file=sys.stderr)
        sys.exit(1)
