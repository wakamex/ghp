import time

import niquests
from common import API_BASE, emit_result, github_headers, parse_args, request_specs


def main():
    repo, since = parse_args("niquests_prototype.py")
    started = time.perf_counter()
    with niquests.Session(
        headers=github_headers("niquests-prototype"),
        multiplexed=True,
        disable_http3=True,
        timeout=15,
    ) as client:
        responses = [
            client.get(f"{API_BASE}/{path}", params=params)
            for path, params in request_specs(repo, since)
        ]
        client.gather(*responses)
        for response in responses:
            response.raise_for_status()
    emit_result([response.json() for response in responses], since, started)


if __name__ == "__main__":
    main()
