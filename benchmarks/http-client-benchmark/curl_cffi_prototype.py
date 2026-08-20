import asyncio
import time

from common import API_BASE, emit_result, github_headers, parse_args, request_specs
from curl_cffi import CurlHttpVersion
from curl_cffi.requests import AsyncSession


async def main():
    repo, since = parse_args("curl_cffi_prototype.py")
    started = time.perf_counter()
    async with AsyncSession(
        headers=github_headers("curl-cffi-prototype"),
        http_version=CurlHttpVersion.V2TLS,
        timeout=15,
    ) as client:
        responses = await asyncio.gather(
            *(
                client.get(f"{API_BASE}/{path}", params=params)
                for path, params in request_specs(repo, since)
            )
        )
        for response in responses:
            response.raise_for_status()
    emit_result([response.json() for response in responses], since, started)


if __name__ == "__main__":
    asyncio.run(main())
