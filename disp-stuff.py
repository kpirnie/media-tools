#!/usr/bin/env python3

import json, argparse
import urllib.request

#DISP_HOST = "https://disp.kptv.im"
DISP_HOST = "http://192.168.2.200:8000"
DISP_USER = "kp"
DISP_PASS = "kp"


def api(path, method="GET", payload=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(DISP_HOST + path, data=data, headers=headers, method=method)
    return json.load(urllib.request.urlopen(req))

def renumber_channels(token):
    # Fetch channels
    print("Fetching channels...")
    channels = api("/api/channels/channels/", token=token)

    # Sort by name and number sequentially
    channels.sort(key=lambda c: c["name"].lower())
    print(f"Renumbering {len(channels)} channels...")

    # Patch each channel with its new number
    for num, c in enumerate(channels, start=1):
        if c.get("channel_number") != num:
            api(f"/api/channels/channels/{c['id']}/", "PATCH", {"channel_number": num}, token=token)
            print(f"  Renumbered: {c['name']} -> {num}")

    print("Channels renumbered.")

def map_tvgid(token):
    # Grab a Dispatcharr JWT
    print("Authenticating with Dispatcharr...")
    token = api("/api/accounts/token/", "POST", {"username": DISP_USER, "password": DISP_PASS})["access"]

    # Fetch channels + EPG data
    print("Fetching channels and EPG data...")
    channels = api("/api/channels/channels/", token=token)
    epg = {e["id"]: e["tvg_id"] for e in api("/api/epg/epgdata/", token=token)}

    # Find channels whose tvg_id doesn't match their mapped EPG record
    mismatches = [
        (c["id"], epg[c["epg_data_id"]])
        for c in channels
        if c.get("epg_data_id") and c["epg_data_id"] in epg and c.get("tvg_id") != epg[c["epg_data_id"]]
    ]
    print(f"Found {len(mismatches)} channels with mismatched tvg-ids. Syncing...")

    # Patch each mismatched channel
    for cid, tvgid in mismatches:
        api(f"/api/channels/channels/{cid}/", "PATCH", {"tvg_id": tvgid}, token=token)
        print(f"  Synced tvg-id: {cid} -> {tvgid}")

    print("Tvg-ids synced.")

def clear_epgs(token):
    # Fetch channels
    print("Fetching channels...")
    channels = api("/api/channels/channels/", token=token)

    # Find channels with an EPG mapping
    mapped = [c for c in channels if c.get("epg_data_id")]
    print(f"Clearing EPG mappings from {len(mapped)} channels...")

    # Patch each mapped channel
    for c in mapped:
        api(f"/api/channels/channels/{c['id']}/", "PATCH", {"epg_data_id": None}, token=token)
        print(f"  Cleared: {c['name']}")

    print("EPG mappings cleared.")

def main():
    # Parse arguments
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_map = sub.add_parser("map")
    p_map.add_argument("--tvgid", action="store_true", required=True)
    p_renum = sub.add_parser("renumber")
    p_renum.add_argument("--channels", action="store_true", required=True)
    p_clear = sub.add_parser("clearepgs")
    args = parser.parse_args()

    # Grab a Dispatcharr JWT
    print("Authenticating with Dispatcharr...")
    token = api("/api/accounts/token/", "POST", {"username": DISP_USER, "password": DISP_PASS})["access"]

    if args.command == "map":
        map_tvgid(token)
    elif args.command == "renumber":
        renumber_channels(token)
    elif args.command == "clearepgs":
        clear_epgs(token)

if __name__ == "__main__":
    main()