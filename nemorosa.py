import sys
import os
import http.cookies
import time
import traceback
import bencode
import collections
import argparse
import posixpath
import json  # Added JSON module
from typing import Optional
from urllib.parse import urlparse
# imports: third party
from colorama import Fore, init, Style
import requests
import requests.cookies
import transmission_rpc

# imports: custom
import modules.filecompare, modules.api, modules.cookies
import modules.logger

# constants
PROCESSED_DIRS_FILE = "processed-dirs.txt"
CHECK_TRACKERS = ["flacsfor.me", "home.opsfet.ch", "52dic.vip"]

def parse_libtc_url(url):
    # transmission+http://127.0.0.1:9091/?session_path=/session/path/
    # rtorrent+scgi:///path/to/socket.scgi?session_path=/session/path/
    # deluge://username:password@127.0.0.1:58664/?session_path=/session/path/
    # qbittorrent+http://username:password@127.0.0.1:8080/?session_path=/session/path/
    kwargs = {}
    parsed = urlparse(url)
    scheme = parsed.scheme.split("+")
    netloc = parsed.netloc
    if "@" in netloc:
        auth, netloc = netloc.split("@")
        username, password = auth.split(":")
        kwargs["username"] = username
        kwargs["password"] = password

    client = scheme[0]
    if client in ["qbittorrent"]:
        kwargs["url"] = f"{scheme[1]}://{netloc}{parsed.path}"
    else:
        kwargs['scheme'] = scheme[1]
        kwargs["host"], kwargs["port"] = netloc.split(":")
        kwargs["port"] = int(kwargs["port"])

    return kwargs

# ================== JSON Helper Functions ==================
def load_json_with_default(file_path, default=None):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default
    
def append_to_json_set(file_path, record):
    data = set(load_json_with_default(file_path, set()))
    data.add(record)
    with open(file_path, 'w', encoding="utf-8") as f:
        json.dump(list(data), f, indent=4, ensure_ascii=False)

def append_to_json_dict(file_path, key, value):
    data = load_json_with_default(file_path, {})
    data[key] = value
    with open(file_path, 'w', encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# ================== Torrent Directory Processing Functions ==================

def check_path(path: str, is_file: bool = False, auto_create: bool = False):
    if path is not None:
        abspath = os.path.abspath(path)
        if os.path.exists(abspath):
            if is_file and not os.path.isfile(abspath):
                logger.error("Path must be a file: {}".format(path))
                exit(0)
            if not is_file and not os.path.isdir(abspath):
                logger.error("Path must be a folder: {}".format(path))
                exit(0)
        else:
            if not auto_create:
                logger.error("Path doesn't exist: {} ".format(path))
                exit(0)
            else:
                if is_file:
                    logger.success("File automatically created: {}".format(path))
                    folder = os.path.split(abspath)[0]
                    if not os.path.exists(folder):
                        os.makedirs(folder)
                    with open(abspath, "w") as _:
                        pass
                else:
                    logger.success("Directory automatically created: {}".format(path))
                    os.makedirs(path)

def scan(
    *,
    fdict: dict,
    tsize: int,
    scan_source: str,
    original_name: str,
    api: modules.api.WhatAPI,
    download_dir: str,
    transmission_client: transmission_rpc.Client,
    result_url_path,
    result_map_path,
    scan_history_path,
    result_url_undownloaded_path,
    no_download,
    GLOBAL,
):
    GLOBAL["scanned"] += 1

    tid = -1
    # search for the files with top 5 longest name
    scan_querys = []
    max_fnames = sorted(fdict.keys(), key=lambda fname: len(fname), reverse=True)
    for index, fname in enumerate(max_fnames):
        if index == 0 or posixpath.splitext(fname)[1] in [".flac", ".mp3", ".dsf", ".dff", ".m4a"]:
            scan_querys.append(fname)
        if len(scan_querys) >= 5:
            break

    for fname in scan_querys:
        logger.debug(f"Searching for file: {fname}")
        search_resp = api.search_torrent_by_filename(fname)
        
        # 记录API响应状态
        if search_resp.get("status") != "success":
            logger.warning(f"API failure for file '{fname}': {json.dumps(search_resp)}")
            continue
        else:
            logger.debug(f"API search successful for file '{fname}'")
        
        # 处理搜索结果
        torrents = []
        for group in search_resp["response"]["results"]:
            if "torrents" in group:
                torrents.extend(group["torrents"])
        
        # 记录找到的结果数量
        logger.debug(f"Found {len(torrents)} potential matches for file '{fname}'")
        
        # 按大小匹配
        size_match_found = False
        for t in torrents:
            if t["size"] == tsize:
                tid = t["torrentId"]
                size_match_found = True
                logger.success(f"Size match found! Torrent ID: {tid} (Size: {tsize})")
                break
        
        if size_match_found:
            break
        
        # 处理结果过多的情况
        if len(torrents) > 20:
            logger.warning(f"Too many results found for file '{fname}' ({len(torrents)}). Skipping.")
            continue
        
        # 按文件内容匹配
        if tid == -1:
            logger.debug(f"No size match found. Checking file contents for '{fname}'")
            for t_index, t in enumerate(torrents, 1):
                logger.debug(f"Checking torrent #{t_index}/{len(torrents)}: ID {t['torrentId']}")
                
                resp = api.torrent(t["torrentId"])
                resp_files = resp.get("fileList", {})
                
                # 检查当前文件是否匹配
                if resp_files.get(fname, 0) == fdict[fname]:
                    # 如果是音乐文件，直接匹配
                    matched = False
                    if posixpath.splitext(fname)[1] in [".flac", ".mp3", ".dsf", ".dff", ".m4a"]:
                        tid = t["torrentId"]
                        matched = True
                    else:
                        check_music_file = scan_querys[-1]
                        if resp_files.get(check_music_file, 0) == fdict[check_music_file]:
                            tid = t["torrentId"]
                            matched = True

                    if matched:
                        logger.success(f"Music file match found! Torrent ID: {tid} (File: {fname})")
                        # 检查文件冲突
                        if modules.filecompare.check_conflicts(fdict, resp_files):
                            logger.debug("Conflict detected. Skipping this torrent.")
                            tid = -1  # 重置tid
                        
                        break
        
        # 如果找到匹配，提前退出
        if tid != -1:
            logger.debug(f"Match found with file '{fname}'. Stopping search.")
            break
        
        # 如果没有更多结果，停止搜索
        if len(torrents) == 0 or search_resp["response"]["pages"] <= 1:
            logger.debug(f"No more results for file '{fname}'")
            if posixpath.splitext(fname)[1] in [".flac", ".mp3", ".dsf", ".dff", ".m4a"]:
                logger.debug("Stopping search as music file match is not found")
                break

    if tid == -1:
        logger.header("No matching torrent found")
        append_to_json_dict(result_map_path, os.path.split(scan_source)[1], tid)
        append_to_json_set(scan_history_path, scan_source)
        downloaded = False
    else:
        GLOBAL["found"] += 1
        logger.success("Found match! Torrent ID: {}".format(tid))
        torrent_data = api.download_torrent(tid)

        torrent_object: collections.OrderedDict = bencode.decode(torrent_data)
        fdict_torrent = {}
        for f in torrent_object["info"]["files"]:
            tsize += f["length"]
            fdict_torrent["/".join(f["path"])] = f["length"]
        
        rename_map = modules.filecompare.generate_rename_map(fdict, fdict_torrent)

        downloaded = False
        if not no_download:
            if inject_transmission_client(
                transmission_client, torrent_data, download_dir, original_name, rename_map
            ):
                downloaded = True
                GLOBAL["downloaded"] += 1
                logger.success("Torrent added to Transmission successfully")
            else:
                logger.error("Failed to download .torrent file from {}".format(tid))
                GLOBAL["cnt_dl_fail"] += 1
                if GLOBAL["cnt_dl_fail"] <= 10:
                    logger.error(traceback.format_exc())
                    logger.error(
                        "It might because the torrent id {} has reached the "
                        "limitation of non-browser downloading of {}. "
                        "The URL of failed downloading will be saved to {}. "
                        "You can download it from your own browser.".format(
                            tid, api.server, result_url_undownloaded_path
                        )
                    )
                    if GLOBAL["cnt_dl_fail"] == 10:
                        logger.debug(
                            "Suppressing further hinting for .torrent file downloading failures"
                        )
        append_to_json_set(result_url_path, tid)
        append_to_json_dict(result_map_path, os.path.split(scan_source)[1], tid)
        if not downloaded:
            append_to_json_dict(result_url_undownloaded_path, tid, {"download_dir": download_dir, "original_name": original_name, "rename_map": rename_map})
        append_to_json_set(scan_history_path, scan_source)
    return tid, downloaded

# ================== Transmission Client Functions ==================

def inject_transmission_client(
    transmission_client: transmission_rpc.Client,
    torrent_data,
    download_dir: str,
    original_torrent_name: Optional[str] = None,
    rename_map: Optional[dict] = {}):
    """
    Inject a torrent into the Transmission client from a download URL.
    """

    max_retries = 8
    for attempt in range(max_retries):
        try:
            added_torrent = transmission_client.add_torrent(torrent_data, download_dir=download_dir, paused=True, labels=["nemorosa"])
            if added_torrent.name != original_torrent_name:
                transmission_client.rename_torrent_path(added_torrent.id, location=added_torrent.name, name=original_torrent_name)
                logger.debug(f"Renamed torrent from {added_torrent.name} to {original_torrent_name}")
            if rename_map != {}:
                for torrent_file_name, local_file_name in rename_map.items():
                    transmission_client.rename_torrent_path(added_torrent.id, location=posixpath.join(original_torrent_name, torrent_file_name), name=local_file_name)
                    logger.debug(f"Renamed torrent file {torrent_file_name} to {local_file_name}")

                transmission_client.verify_torrent(added_torrent.id)
            logger.success("Torrent added to Transmission successfully")
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                logger.debug(f"Error injecting torrent into Transmission: {e}, retrying ({attempt + 1}/{max_retries})...")
                time.sleep(2)
            else:
                logger.error(f"Failed to inject torrent into Transmission after {max_retries} attempts: {e}")
                return False

def get_transmission_torrents(transmission_client: transmission_rpc.Client, target_trackers: list) -> list[dict]:
    """
    Retrieve torrents from Transmission client based on filter rules
    """
    try:
        # Get all torrents
        torrents = transmission_client.get_torrents()
        
        # Apply filter rules
        filtered_torrents = {}
        already_cross_seeded_names = set()

        for torrent in torrents:
            
            if any(any(check_str in url for check_str in target_trackers) for url in torrent.tracker_list):
                already_cross_seeded_names.add(torrent.name)

            # Apply name pattern filter
            if any(any(check_str in url for check_str in CHECK_TRACKERS) for url in torrent.tracker_list):
                
                if torrent.name in filtered_torrents:
                    if len(torrent.fields['files']) > len(filtered_torrents[torrent.name]['files']):
                        continue
                    elif len(torrent.fields['files']) == len(filtered_torrents[torrent.name]['files']):
                        if torrent.total_size > filtered_torrents[torrent.name]['totalSize']:
                            continue

                # If passed all filters, include in results
                filtered_torrents[torrent.name] = {
                    'hash': torrent.hash_string,
                    'percentDone': torrent.percent_done,
                    'totalSize': torrent.total_size,
                    'files': torrent.fields['files'],
                    'trackers': torrent.tracker_list,
                    'downloadDir':torrent.download_dir
                }
        
        for name in already_cross_seeded_names:
            if name in filtered_torrents:
                del filtered_torrents[name]

        return filtered_torrents
    
    except Exception as e:
        logger.error("Error retrieving torrents from Transmission: %s", e)
        return []

def process_transmission_torrents(transmission_client, api, result_dir, no_download, target_trackers):
    """
    Process torrents from Transmission client
    """
    logger.section("===== Processing Transmission Torrents =====")
    
    # Set up paths for results with JSON extensions
    scan_history_path = os.path.join(result_dir, "transmission_scan_history.json")
    result_url_path = os.path.join(result_dir, "transmission_result_url.json")
    result_map_path = os.path.join(result_dir, "transmission_result_mapping.json")
    result_url_undownloaded_path = os.path.join(result_dir, "transmission_result_url_undownloaded.json")

    # Create necessary directories/files
    check_path(result_dir, auto_create=True)
    check_path(scan_history_path, is_file=True, auto_create=True)
    check_path(result_url_path, is_file=True, auto_create=True)
    check_path(result_map_path, is_file=True, auto_create=True)
    check_path(result_url_undownloaded_path, is_file=True, auto_create=True)

    GLOBAL = {
        "found": 0,
        "downloaded": 0,
        "scanned": 0,
        "cnt_dl_fail": 0
    }

    try:
        # Get already scanned torrents from JSON
        scanned_set = set(load_json_with_default(scan_history_path, set()))
        
        # Get torrents from Transmission
        torrents = get_transmission_torrents(transmission_client, target_trackers)
        logger.debug("Found %d torrents in Transmission matching the criteria", len(torrents))
        
        for i, (torrent_name, torrent_details) in enumerate(torrents.items()):

            # Skip already scanned torrents
            if torrent_details['hash'] in scanned_set:
                logger.debug("Skipping already scanned torrent: %s (%s)", torrent_name, torrent_details['hash'])
                continue
                
            logger.header("Processing %d/%d: %s (%s)", i+1, len(torrents), torrent_name, torrent_details['hash'])
            
            # Prepare file list and size
            tsize = torrent_details['totalSize']
            fdict = {posixpath.relpath(f['name'], torrent_name): f['length'] for f in torrent_details['files']}
            # Scan for matches
            scan(
                fdict=fdict, 
                tsize=tsize, 
                scan_source=torrent_details['hash'],
                original_name=torrent_name,
                api=api,
                download_dir=torrent_details['downloadDir'],
                transmission_client=transmission_client,
                result_url_path=result_url_path,
                result_map_path=result_map_path,
                scan_history_path=scan_history_path,
                result_url_undownloaded_path=result_url_undownloaded_path,
                no_download=no_download,
                GLOBAL=GLOBAL
            )
            
            # Record that we've processed this torrent
            append_to_json_set(scan_history_path, torrent_details['hash'])
    
    except Exception as e:
        logger.error("Error processing Transmission torrents: %s", e)
        logger.error(traceback.format_exc())
    finally:
        logger.success("Transmission processing summary:")
        logger.success("Torrents scanned: %d", GLOBAL["scanned"])
        logger.success("Matches found: %d", GLOBAL["found"])
        logger.success(".torrent files downloaded: %d", GLOBAL["downloaded"])
        logger.section("===== Transmission Processing Complete =====")


# ================== Main Function ==================


def main():
    # custom help subclass
    class CustomHelpFormatter(argparse.HelpFormatter):
        def __init__(self, prog):
            super().__init__(prog, max_help_position=40, width=80)

        def _format_action_invocation(self, action):
            if not action.option_strings or action.nargs == 0:
                return super()._format_action_invocation(action)
            default = self._get_default_metavar_for_optional(action)
            args_string = self._format_args(action, default)
            return ", ".join(action.option_strings) + " " + args_string

    # argparse custom help format
    fmt = lambda prog: CustomHelpFormatter(prog)
    # argparse parser
    parser = argparse.ArgumentParser(
        description="Scan Transmission client torrents to find matches on WhatAPI",
        formatter_class=fmt,
    )
    # transmission client option
    transmission_group = parser.add_argument_group("Transmission client options")
    transmission_group.add_argument(
        "--transmission",
        required=True,
        help="Transmission RPC URL (e.g. transmission+http://user:pass@localhost:9091)",
    )
    # result directory
    parser.add_argument(
        "--result-dir",
        metavar="PATH",
        default="./results",
        help="output path for scan results (default: %(default)s)",
    )
    # no download option
    parser.add_argument(
        "--no-download",
        action="store_true",
        default=False,
        help="if set, don't download .torrent files, only save URLs",
    )
    # log level
    parser.add_argument(
        "-l",
        "--loglevel",
        metavar="LOGLEVEL",
        default="info",
        choices=["debug", "info", "warning", "error", "critical"],
        help="loglevel for log file (default: %(default)s)",
    )
    # server URL
    parser.add_argument(
        "-s",
        "--server",
        metavar="URL",
        default="https://dicmusic.com",
        help="server URL (default: %(default)s)",
    )
    parser.add_argument(
        "-t",
        "--tracker",
        default="52dic.vip",
        help="tracker URL (default: %(default)s)",
    )
    # api interval
    parser.add_argument(
        "-i",
        "--api-interval",
        type=float,
        default=2,
        metavar="SECONDS",
        help="interval between API calls in seconds (default: %(default)s)",
    )
    # login details
    parser.add_argument(
        "-a",
        "--api-key",
        default=None,
        help="API key for authentication",
    )
    parser.add_argument(
        "-c",
        "--cookie",
        default=None,
        help="Cookie string for authentication",
    )
    # parse arguments
    args = parser.parse_args()
    # set variables from args
    server = args.server
    tracker = args.tracker
    apikey = args.api_key
    cookie_str = args.cookie
    interval = args.api_interval
    result_dir = args.result_dir
    no_download = args.no_download
    transmission_url = args.transmission

    # Set up logging
    modules.logger.global_loglevel = args.loglevel
    global logger
    logger = modules.logger.generate_logger(modules.logger.global_loglevel)
    logger.section("===== Nemorosa Starting =====")

    # load cookies
    cookies = modules.cookies.get_cookies()

    # convert raw cookie line to RequestsCookieJar
    if cookie_str:
        simple_cookie = http.cookies.SimpleCookie(cookie_str)
        cookies = requests.cookies.RequestsCookieJar()
        cookies.update(simple_cookie)

    target_trackers = [tracker]

    # connect to API
    logger.section("===== Establishing Connections =====")
    logger.debug("Getting connection to API...")
    try:
        api = modules.api.WhatAPI(api_key=apikey, interval=interval, cookies=cookies, server=server)
        logger.success("API connection established")
    except Exception as e:
        logger.critical("API connection failed: {}".format(str(e)))
        sys.exit(1)

    try:
        logger.section("===== Connecting to Transmission =====")
        logger.debug("Connecting to Transmission client at %s...", transmission_url)
        transmission_info = parse_libtc_url(transmission_url)
        transmission_client = transmission_rpc.Client(
            host=transmission_info.get("host", "localhost"),
            port=transmission_info.get("port", 9091),
            username=transmission_info.get("username"),
            password=transmission_info.get("password"),
        )
        logger.success("Successfully connected to Transmission client")

        # Process Transmission torrents
        process_transmission_torrents(
            transmission_client, api, result_dir, no_download, target_trackers
        )
    except Exception as e:
        logger.critical("Error connecting to Transmission client: %s", e)
        sys.exit(1)
        
    logger.section("===== Nemorosa Finished =====")

if __name__ == "__main__":
    # initialise colorama
    init(autoreset=True)
    main()