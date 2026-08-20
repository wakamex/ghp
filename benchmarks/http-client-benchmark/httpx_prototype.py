import asyncio
import time

import httpx
from common import API_BASE, emit_result, github_headers, parse_args, request_specs


async def main():
    repo, since = parse_args("httpx_prototype.py")
    started = time.perf_counter()
    async with httpx.AsyncClient(
        base_url=API_BASE,
        headers=github_headers("httpx-prototype"),
        http2=True,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        timeout=15,
    ) as client:
        responses = await asyncio.gather(
            *(
                client.get(path, params=params)
                for path, params in request_specs(repo, since)
            )
        )
        for response in responses:
            response.raise_for_status()
    emit_result([response.json() for response in responses], since, started)


if __name__ == "__main__":
    asyncio.run(main())
