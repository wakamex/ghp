import io
import json
import time
import urllib.parse

import pycurl
from common import API_BASE, emit_result, github_headers, parse_args, request_specs


def fetch_all(requests, headers):
    multi = pycurl.CurlMulti()
    multi.setopt(pycurl.M_PIPELINING, pycurl.PIPE_MULTIPLEX)
    multi.setopt(pycurl.M_MAX_HOST_CONNECTIONS, 1)
    handles = []
    for path, params in requests:
        body = io.BytesIO()
        handle = pycurl.Curl()
        handle.setopt(
            pycurl.URL,
            f"{API_BASE}/{path}?{urllib.parse.urlencode(params)}",
        )
        handle.setopt(pycurl.HTTPHEADER, headers)
        handle.setopt(pycurl.HTTP_VERSION, pycurl.CURL_HTTP_VERSION_2TLS)
        handle.setopt(pycurl.TIMEOUT, 15)
        handle.setopt(pycurl.WRITEFUNCTION, body.write)
        handles.append((handle, body))
        multi.add_handle(handle)

    while True:
        while True:
            status, active = multi.perform()
            if status != pycurl.E_CALL_MULTI_PERFORM:
                break
        if not active:
            break
        multi.select(1.0)

    queued, succeeded, failed = multi.info_read()
    while queued:
        next_queued, next_succeeded, next_failed = multi.info_read()
        queued = next_queued
        succeeded.extend(next_succeeded)
        failed.extend(next_failed)
    if failed:
        _handle, error_number, error_message = failed[0]
        raise RuntimeError(f"curl error {error_number}: {error_message}")

    payloads = []
    for handle, body in handles:
        status = handle.getinfo(pycurl.RESPONSE_CODE)
        if not 200 <= status < 300:
            raise RuntimeError(f"GitHub returned HTTP {status}")
        payloads.append(json.loads(body.getvalue()))
        multi.remove_handle(handle)
        handle.close()
    multi.close()
    return payloads


def main():
    repo, since = parse_args("pycurl_prototype.py")
    headers = [
        f"{name}: {value}" for name, value in github_headers("pycurl-prototype").items()
    ]
    started = time.perf_counter()
    payloads = fetch_all(request_specs(repo, since), headers)
    emit_result(payloads, since, started)


if __name__ == "__main__":
    main()
